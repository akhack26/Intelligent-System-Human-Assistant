"""Memory levels.

* short-term  : the conversation list mani.py already keeps (``_conv_history``)
* working     : ``WorkingMemory`` - what the current task is about (last app,
                browser, files found, project created) so "isko Documents me
                move karo" / "ab GitHub kholo" resolve correctly across turns
* long-term   : ``LongTermMemory`` - facts the user EXPLICITLY asked ISHA to
                remember (preferences, project names, folders). Local JSON,
                never sent anywhere, and never stores secrets.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

_SECRET_RE = re.compile(
    r"(password|passwd|pass\s*word|pin\b|otp\b|cvv|api[\s_-]?key|token|secret|\bpaswrd\b)"
    r"|\b(?:\d[ -]?){13,19}\b"            # card-number-like digit runs
    r"|\b\d{4}\s?\d{4}\s?\d{4}\b",       # aadhaar-like
    re.I)

CATEGORIES = ("preference", "project", "folder", "app", "person", "fact")


def looks_secret(text: str) -> bool:
    return bool(_SECRET_RE.search(text or ""))


def guess_category(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\b(prefer|pasand|favou?rite|like\s+to|hamesha|always|default|language|bhasha)\b", t):
        return "preference"
    if re.search(r"\bproject\b", t):
        return "project"
    if re.search(r"\b(folder|directory|path|drive)\b", t):
        return "folder"
    if re.search(r"\b(app|browser|editor|software|chrome|firefox|vs\s*code)\b", t):
        return "app"
    if re.search(r"\b(birthday|mummy|papa|bhai|didi|friend|dost|wife|husband)\b", t):
        return "person"
    return "fact"


class LongTermMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._items = self._load()

    def _load(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def remember(self, text: str, category: str = "") -> dict:
        text = re.sub(r"\s+", " ", (text or "")).strip(" .")
        if not text:
            return {"success": False, "message": "Kya yaad rakhna hai?"}
        if looks_secret(text):
            return {"success": False, "message": "Passwords, PIN, OTP ya card numbers main yaad nahi rakhti - "
                                                 "unhe kisi password manager me rakho."}
        cat = category if category in CATEGORIES else guess_category(text)
        with self._lock:
            for it in self._items:
                if it["text"].lower() == text.lower():
                    return {"success": True, "message": f"Yeh pehle se yaad hai: {text}", "item": it}
            item = {"id": f"m{int(time.time() * 1000)}", "text": text, "category": cat,
                    "created": datetime.now().isoformat(timespec="seconds")}
            self._items.append(item)
            self._save()
        return {"success": True, "message": f"Yaad rakh liya ({cat}): {text}", "item": item}

    def forget(self, query: str) -> dict:
        q = (query or "").strip().lower()
        with self._lock:
            if q in ("all", "sab", "sab kuch", "everything", "saari memory", "poori memory"):
                n = len(self._items)
                self._items = []
                self._save()
                return {"success": True, "message": f"Saari long-term memory mita di ({n} items).", "removed": n}
            hits = [it for it in self._items if it["id"] == q or (q and q in it["text"].lower())]
            if not hits and q:
                texts = [it["text"].lower() for it in self._items]
                close = difflib.get_close_matches(q, texts, n=1, cutoff=0.6)
                hits = [it for it in self._items if close and it["text"].lower() == close[0]]
            if not hits:
                return {"success": False, "message": f"'{query}' se milti koi memory nahi mili.", "removed": 0}
            self._items = [it for it in self._items if it not in hits]
            self._save()
        return {"success": True, "message": "Bhool gayi: " + "; ".join(h["text"] for h in hits),
                "removed": len(hits)}

    def items(self, category: str = "") -> list:
        with self._lock:
            return [dict(i) for i in self._items if not category or i["category"] == category]

    def search(self, query: str, top_k: int = 5) -> list:
        qt = set(re.findall(r"\w+", (query or "").lower()))
        if not qt:
            return []
        scored = []
        for it in self.items():
            tt = set(re.findall(r"\w+", it["text"].lower()))
            inter = qt & tt
            if inter:
                scored.append((len(inter) / (len(qt | tt) or 1), it))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in scored[:top_k]]

    def format(self) -> str:
        items = self.items()
        if not items:
            return "Long-term memory khaali hai. 'Yaad rakho ki ...' bolo to main yaad rakhungi."
        by_cat = {}
        for it in items:
            by_cat.setdefault(it["category"], []).append(it["text"])
        return "Mujhe yeh yaad hai:\n" + "\n".join(
            f"[{c}] " + "; ".join(v) for c, v in by_cat.items())

    def prompt_block(self, query: str = "", limit: int = 8) -> str:
        """Compact context for the system prompt: preferences always, rest by relevance."""
        prefs = self.items("preference")[:limit]
        rel = [i for i in self.search(query, top_k=limit) if i not in prefs] if query else []
        chosen = (prefs + rel)[:limit]
        if not chosen:
            return ""
        return "[USER MEMORY - things the user asked you to remember]\n" + "\n".join(f"- {i['text']}" for i in chosen)


class WorkingMemory:
    """Task context that survives across turns (not persisted)."""

    def __init__(self):
        self.last_app = ""
        self.last_browser = ""
        self.last_files: list = []
        self.last_folder = ""
        self.last_project = ""
        self.current_task = ""
        self.updated = 0.0

    def note_tool(self, tool: str, args: dict, result: str):
        args = args or {}
        self.updated = time.time()
        if tool in ("open_app", "focus_app") and args.get("app_name"):
            self.last_app = str(args["app_name"])
            b = str(args["app_name"]).lower()
            if b in ("chrome", "firefox", "edge", "brave", "google chrome"):
                self.last_browser = "chrome" if "chrome" in b else b
        if tool in ("open_website", "web_search", "youtube_play") and args.get("browser"):
            self.last_browser = str(args["browser"])
        if tool in ("find_files", "advanced_file_search", "search_files", "list_media_files", "list_directory"):
            paths = re.findall(r"^\s{2}(\S.*)$", result or "", re.M)
            if paths:
                self.last_files = paths[:20]
        if tool in ("create_code_project", "run_project") :
            m = re.search(r"(?:ban gaya|Project):?\s*.*?([A-Za-z]:\\[^\s(]+|/[^\s(]+)", result or "")
            if m:
                self.last_project = m.group(1).rstrip(".,)")
            elif args.get("project_name"):
                self.last_project = str(args["project_name"])
        if tool in ("create_folder", "create_desktop_folder"):
            m = re.search(r"([A-Za-z]:\\[^\n]+|/[^\n]+)$", (result or "").strip())
            if m:
                self.last_folder = m.group(1).strip()

    _PRONOUN = re.compile(r"\b(is|us|ye|yeh|woh|wo|that|this|isi|usi)\s+(file|folder|photo|pdf|image|video|project)\b"
                          r"|\b(isko|usko|ise|use|it)\b", re.I)

    def refers_back(self, text: str) -> bool:
        return bool(self._PRONOUN.search(text or ""))

    def resolve_file(self) -> str:
        return self.last_files[0] if self.last_files else ""

    def summary(self) -> str:
        bits = []
        if self.last_app:
            bits.append(f"last app opened: {self.last_app}")
        if self.last_browser:
            bits.append(f"browser in use: {self.last_browser}")
        if self.last_files:
            bits.append(f"last file found: {self.last_files[0]}")
        if self.last_project:
            bits.append(f"last project: {self.last_project}")
        if self.last_folder:
            bits.append(f"last folder created: {self.last_folder}")
        return ("[WORKING MEMORY]\n" + "\n".join(f"- {b}" for b in bits)) if bits else ""


_REMEMBER_RE = re.compile(
    r"^(?:isha\s*,?\s*)?(?:please\s+)?(?:yaad\s+rakh(?:o|na|iye|\s+lo|\s+lena)?|remember(?:\s+that)?|note\s+kar\s*lo|"
    r"dhyan\s+rakhna)\s*(?:ki|that|:)?\s+(.+)$|^(.+?)\s+(?:yaad\s+rakh(?:o|na|iye|\s+lo|\s+lena))\.?$", re.I)
_FORGET_RE = re.compile(
    r"^(?:isha\s*,?\s*)?(?:please\s+)?(?:bhool\s+ja(?:o|na)?|forget(?:\s+about)?|mita\s+do)\s*(?:ki|that|:)?\s+(.+)$"
    r"|^(.+?)\s+(?:bhool\s+ja(?:o|na)?|forget\s+kar(?:o|\s+do))\.?$", re.I)
_SHOW_RE = re.compile(
    r"\b(show|dikhao|batao)\s+(?:my\s+|meri\s+)?memory\b|\bmemory\s+(dikhao|batao|show|list)\b|"
    r"\bkya\s+(?:kya\s+)?yaad\s+hai\b|\bwhat\s+do\s+you\s+remember\b|\bmere\s+baare\s+me\s+kya\s+(?:pata|yaad)\b", re.I)


def parse_memory_command(text: str):
    """-> ('remember_fact', {'fact': ...}) | ('forget_memory', {...}) | ('show_memory', {}) | None"""
    s = (text or "").strip().rstrip("?.! ")
    if not s:
        return None
    if _SHOW_RE.search(s) and not re.search(r"\b(ram|usage|kitni|kitna)\b", s, re.I):
        return ("show_memory", {})
    m = _FORGET_RE.match(s)
    if m:
        return ("forget_memory", {"query": (m.group(1) or m.group(2) or "").strip()})
    m = _REMEMBER_RE.match(s)
    if m:
        fact = (m.group(1) or m.group(2) or "").strip()
        if fact and not re.fullmatch(r"(hai|ho|na)\??", fact, re.I):
            return ("remember_fact", {"fact": fact})
    return None
