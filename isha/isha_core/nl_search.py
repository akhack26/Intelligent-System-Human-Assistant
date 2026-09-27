"""Natural language (Hindi / Hinglish / English) -> structured file search.

    "10 MB se badi videos dhundo"     -> media=video, min_size=10MB
    "kal modify hui file dhundo"      -> modified yesterday
    "Downloads mein python files"     -> ext=.py, location=Downloads
    "meri last wali photo"            -> media=image, newest first, top result
    "Meri 2025 wali PDF dhundo"       -> ext=.pdf, name contains 2025
                                         (fallback: modified during 2025)

``parse_search_request`` returns an ordered list of candidate queries; the
engine runs them in order and stops at the first one with results, so an
ambiguous phrase ("2025 wali") still finds something without guessing wrong
silently. The chosen interpretation is reported back to the user.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .file_index import SearchQuery, FileIndex, scan_search
from . import paths

MB = 1024 * 1024
GB = 1024 * MB

_MEDIA_WORDS = [
    (r"photos?|images?|pics?|pictures?|tasveer\w*|tasvir\w*|foto\w*|wallpapers?|screenshots?", "image"),
    (r"videos?|movies?|films?|clips?|recordings?", "video"),
    (r"songs?|gaan[ae]|gane|audio|music|recordings?\s+audio|voice\s+notes?", "audio"),
    (r"pdfs?", "pdf"),
    (r"documents?|docs?|dastavez\w*", "document"),
    (r"zips?|rars?|archives?|compressed", "archive"),
    (r"source\s+code|code\s+files?|scripts?", "code"),
    (r"apps?|applications?|installers?|setups?|softwares?|programs?|exe", "application"),
]

_EXT_WORDS = [
    (r"python|py", [".py"]), (r"javascript|js", [".js"]), (r"typescript|ts", [".ts", ".tsx"]),
    (r"html", [".html", ".htm"]), (r"css", [".css"]), (r"java", [".java"]),
    (r"c\+\+|cpp", [".cpp", ".hpp", ".h"]), (r"json", [".json"]),
    (r"word", [".doc", ".docx"]), (r"excel|spreadsheets?|sheets?", [".xls", ".xlsx", ".csv"]),
    (r"powerpoint|ppts?|slides?|presentations?", [".ppt", ".pptx"]),
    (r"text|txt|notes?", [".txt", ".md"]), (r"csv", [".csv"]),
    (r"mp4", [".mp4"]), (r"mp3", [".mp3"]), (r"mkv", [".mkv"]), (r"png", [".png"]),
    (r"jpe?g", [".jpg", ".jpeg"]), (r"gif", [".gif"]), (r"docx?", [".doc", ".docx"]),
    (r"xlsx?", [".xls", ".xlsx"]), (r"iso", [".iso"]), (r"apk", [".apk"]), (r"exe", [".exe"]),
    (r"msi", [".msi"]), (r"deb", [".deb"]), (r"appimage", [".appimage"]), (r"wav", [".wav"]),
]

_LOCATION_WORDS = {
    "desktop": r"desktop|डेस्कटॉप",
    "downloads": r"downloads?|डाउनलोड\w*",
    "documents": r"documents|my\s+documents|documents\s+folder",
    "pictures": r"pictures|pictures\s+folder",
    "music": r"music\s+folder",
    "videos": r"videos\s+folder",
}

_STOP = set("""
meri mera mere my mine the a an wali wala wale vali vala file files dhundo dhundho dhoondo dhundh
dhoondh dhund dhoond find search locate khojo khoj dikhao dikha show list mein me main mai par pe
per on in se ki ka ke jo thi tha the save saved kiya ki hui huyi hue hua modify modified change
changed all saari sari sare saare sab sabhi computer pc laptop system kar karo kardo do please plz
isha jarvis mujhe muje hai hain ho kahan kaha where is are of for with folder folders se badi bada
bade badi chhoti choti chota chote larger bigger smaller greater less than more above below over under
size mb gb kb last latest recent recently newest nayi naya purani old oldest week hafte hafta month
mahine mahina year saal kal aaj today yesterday din days day ago pichle pichhle wala waali vaali
that which jisme jiska iska uska koi kuch ek woh wo voh yeh ye jab maine mene humne apni apna apne
saved stored rakhi rakha padi pada hogi hoga sabse bahut
""".split())


@dataclass
class ParsedSearch:
    candidates: list            # [SearchQuery, ...] in priority order
    interpretation: str
    top_only: bool = False      # "meri last wali photo" -> the single newest


def _day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_search_request(text: str, now: datetime | None = None, limit: int = 30) -> ParsedSearch:
    now = now or datetime.now()
    t = " " + (text or "").strip() + " "
    low = t.lower()
    q = SearchQuery(limit=limit)
    notes = []
    consumed = []   # spans of text already understood

    def eat(m):
        consumed.append(m.span())

    # exact filename like calculator.py / report_2024.pdf
    for m in re.finditer(r"([\w\-]+(?:\.[\w\-]+)*\.(?:[a-z0-9]{1,5}))(?=\s|$)", low):
        if not re.fullmatch(r"\d+(?:\.\d+)?", m.group(1)):
            q.name_terms.append(m.group(1))
            eat(m)
            notes.append(f"name '{m.group(1)}'")

    # folders vs files
    if re.search(r"\b(folders?|directory|directories)\b", low) and not re.search(r"\bfolder\s+(?:me|mein|main|par|pe|in)\b", low):
        q.kind = "dir"
        notes.append("folders")

    # size: "10 MB se badi", "larger than 1 GB", "5mb se chhoti"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kb|mb|gb)\s*(?:se\s+)?(?:zyada\s+)?(badi|bada|bade|bigger|larger|greater|above|over|more|upar|zyada|jyada)", low) or \
        re.search(r"(?:bigger|larger|greater|more|above|over)\s+than\s+(\d+(?:\.\d+)?)\s*(kb|mb|gb)()", low)
    if m:
        mult = {"kb": 1024, "mb": MB, "gb": GB}[m.group(2)]
        q.min_size = int(float(m.group(1)) * mult)
        eat(m)
        notes.append(f"> {m.group(1)} {m.group(2).upper()}")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kb|mb|gb)\s*(?:se\s+)?(?:kam\s+)?(chhoti|choti|chota|chhota|smaller|less|below|under|kam|neeche)", low) or \
        re.search(r"(?:smaller|less|below|under)\s+than\s+(\d+(?:\.\d+)?)\s*(kb|mb|gb)()", low)
    if m:
        mult = {"kb": 1024, "mb": MB, "gb": GB}[m.group(2)]
        q.max_size = int(float(m.group(1)) * mult)
        eat(m)
        notes.append(f"< {m.group(1)} {m.group(2).upper()}")
    if q.min_size is None and re.search(r"\b(large|big|badi|bade|heavy|bhaari|bhari)\s+(files?|videos?|folders?)\b|\bsabse\s+badi\b|\blargest\b|\bbiggest\b", low):
        q.min_size = 100 * MB
        q.sort = "size"
        notes.append("large files (>100 MB), biggest first")

    # dates
    today = _day_start(now)
    if re.search(r"\b(kal|yesterday)\b", low) and not re.search(r"\b(aane\s+wale|tomorrow)\b", low):
        q.modified_after = (today - timedelta(days=1)).timestamp()
        q.modified_before = today.timestamp()
        notes.append("modified yesterday")
    elif re.search(r"\b(aaj|today)\b", low):
        q.modified_after = today.timestamp()
        notes.append("modified today")
    m = re.search(r"\b(?:last|pichle|pichhle|past)\s+(\d+)\s+(din|days?|hafte|weeks?|mahine|months?)\b", low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        days = n * (7 if unit.startswith(("hafte", "week")) else 30 if unit.startswith(("mahine", "month")) else 1)
        q.modified_after = (now - timedelta(days=days)).timestamp()
        notes.append(f"last {days} days")
    elif re.search(r"\b(last|pichle|pichhle|previous|is|this)\s+(week|hafte|hafta)\b", low):
        q.modified_after = (now - timedelta(days=7)).timestamp()
        notes.append("last 7 days")
    elif re.search(r"\b(last|pichle|pichhle|previous|is|this)\s+(month|mahine|mahina)\b", low):
        q.modified_after = (now - timedelta(days=30)).timestamp()
        notes.append("last 30 days")
    elif re.search(r"\b(recent|recently|haal\s+hi|abhi\s+abhi)\b", low) and q.modified_after is None:
        q.modified_after = (now - timedelta(days=7)).timestamp()
        notes.append("recent (7 days)")
    top_only = bool(re.search(r"\b(last|latest|newest|sabse\s+nayi|sabse\s+naya|akhri|aakhri)\s+(wali|wala|vali|waali)?\s*"
                              r"(photo|image|pic|video|file|pdf|song|screenshot|document|download)\b", low)
                    or re.search(r"\bmeri\s+last\s+wali\b", low))
    if top_only:
        q.sort = "recent"
        notes.append("newest first")
    if re.search(r"\b(oldest|sabse\s+purani|sabse\s+purana)\b", low):
        q.sort = "oldest"

    year_m = re.search(r"\b(19\d{2}|20\d{2})\b", low)

    # media type / extensions
    for pat, kind in _MEDIA_WORDS:
        m = re.search(rf"\b(?:{pat})\b", low)
        if m:
            eat(m)
            if kind == "pdf":
                q.extensions = sorted(set(q.extensions) | {".pdf"})
                notes.append("PDF")
            elif not q.media_type and not q.extensions:
                q.media_type = kind
                notes.append(kind)
            break
    for pat, exts in _EXT_WORDS:
        m = re.search(rf"\b(?:{pat})\b(?:\s+(?:files?|scripts?|code|documents?|sheets?|videos?|songs?))?", low)
        if m and not any(n.endswith(tuple(exts)) for n in q.name_terms):
            if pat in ("text|txt|notes?",) and not re.search(r"\b(text|txt)\s+files?\b|\bnotes?\s+files?\b", low):
                continue
            q.extensions = sorted(set(q.extensions) | set(exts))
            q.media_type = "" if q.media_type in ("code", "document", "") else q.media_type
            eat(m)
            notes.append("/".join(exts))

    # location
    for key, pat in _LOCATION_WORDS.items():
        m = re.search(rf"\b(?:{pat})\b", low)
        if m:
            q.locations.append(str(paths.known_folder(key)))
            eat(m)
            notes.append(f"in {key}")
    if not q.locations and re.search(r"\b(pictures|music|videos)\s+(?:me|mein|main|folder)\b", low):
        key = re.search(r"\b(pictures|music|videos)\b", low).group(1)
        q.locations.append(str(paths.known_folder(key)))

    # leftover words -> filename terms
    masked = list(low)
    for a, b in consumed:
        for i in range(a, b):
            masked[i] = " "
    leftover = "".join(masked)
    words = re.findall(r"[\w\-]+", leftover)
    terms = []
    for w in words:
        if w in _STOP or len(w) < 2:
            continue
        if year_m and w == year_m.group(1):
            continue
        if re.fullmatch(r"\d+", w):
            continue
        terms.append(w)
    q.name_terms.extend(terms[:4])
    if terms:
        notes.append("name ~ " + " ".join(terms[:4]))

    candidates = []
    if year_m:
        y = int(year_m.group(1))
        by_name = replace(q, name_terms=q.name_terms + [str(y)])
        by_date = replace(q, modified_after=datetime(y, 1, 1).timestamp(),
                          modified_before=datetime(y + 1, 1, 1).timestamp())
        candidates = [by_name, by_date]
        notes.append(f"'{y}' in name (else modified in {y})")
    else:
        candidates = [q]
    if top_only:
        candidates = [replace(c, limit=5) for c in candidates]
    return ParsedSearch(candidates, ", ".join(notes) or "all files", top_only)


class FileSearchEngine:
    """Index-first search with an honest fallback."""

    def __init__(self, index: FileIndex | None, roots: list, excludes: list | None = None):
        self.index = index
        self.roots = [str(r) for r in roots]
        self.excludes = excludes

    def run(self, q: SearchQuery, allow_scan: bool = True, stop_flag=None) -> dict:
        t0 = time.time()
        if self.index is not None and self.index.ready:
            rows = self.index.search(q)
            # the index can be stale for a folder changed seconds ago: if a
            # located search found nothing, double-check that folder directly
            if not rows and q.locations and allow_scan:
                res = scan_search(q, self.roots, self.excludes, max_scanned=50_000,
                                  time_budget_s=8, stop_flag=stop_flag)
                return {"results": res["results"], "method": "index+rescan",
                        "seconds": round(time.time() - t0, 2), "truncated": res["truncated"]}
            return {"results": rows, "method": "index", "seconds": round(time.time() - t0, 3),
                    "truncated": False}
        if not allow_scan:
            return {"results": [], "method": "none", "seconds": 0, "truncated": False}
        res = scan_search(q, self.roots, self.excludes, stop_flag=stop_flag)
        return {"results": res["results"], "method": "scan", "seconds": res["seconds"],
                "truncated": res["truncated"]}

    def search_text(self, text: str, limit: int = 30, stop_flag=None) -> dict:
        parsed = parse_search_request(text, limit=limit)
        last = None
        for i, q in enumerate(parsed.candidates):
            res = self.run(q, stop_flag=stop_flag)
            res["query"] = q.describe()
            res["interpretation"] = parsed.interpretation
            res["candidate"] = i
            last = res
            if res["results"]:
                break
        if parsed.top_only and last and last["results"]:
            last["results"] = last["results"][:1]
        return last or {"results": [], "method": "none", "interpretation": parsed.interpretation}


def format_results(res: dict, max_items: int = 10) -> str:
    rows = res.get("results") or []
    how = res.get("method", "")
    if not rows:
        extra = " (search was cut short; try a narrower folder)" if res.get("truncated") else ""
        return f"Koi file nahi mili [{res.get('interpretation', '')}]{extra}."
    head = f"{len(rows)} result(s) mile [{res.get('interpretation', '')}; via {how}, {res.get('seconds', 0)}s]:"
    lines = []
    for r in rows[:max_items]:
        size = "" if r["is_dir"] else f", {r['size_mb']} MB"
        lines.append(f"- {r['name']} ({r['modified']}{size})\n  {r['path']}")
    more = f"\n...aur {len(rows) - max_items}" if len(rows) > max_items else ""
    return head + "\n" + "\n".join(lines) + more
