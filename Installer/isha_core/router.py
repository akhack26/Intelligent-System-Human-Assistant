"""Task classification + multi-model registry + model routing.

    user text ──► classify_task() ──► TaskType ──► role ──► ModelRegistry ──► GGUF path
                                                     ▲
                        manual override (GUI "Model: Coding") ──┘

Only *roles* are hard-coded (general / coding / reasoning / study / fast /
vision). Which file backs each role comes from configuration:

    "models": {"general": "models/general/x.gguf", "coding": "...", ...}

plus the legacy keys the old build used (``gguf_model_path`` → general,
``code_model_path`` / ``code_model`` → coding), plus auto-discovery of
``models/<role>/*.gguf`` folders. A role with no model falls back along a
fixed chain, and the decision always says *which* file it picked and *why*,
so the UI never claims a model that is not installed.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import hardware


class TaskType(str, Enum):
    GENERAL_CHAT = "GENERAL_CHAT"
    CODING = "CODING"
    DEBUGGING = "DEBUGGING"
    REASONING = "REASONING"
    STUDY = "STUDY"
    RESEARCH = "RESEARCH"
    FILE_OPERATION = "FILE_OPERATION"
    FILE_SEARCH = "FILE_SEARCH"
    SYSTEM_CONTROL = "SYSTEM_CONTROL"
    VISION = "VISION"
    QUICK_COMMAND = "QUICK_COMMAND"
    PLANNING = "PLANNING"
    CREATIVE = "CREATIVE"
    MESSAGING = "MESSAGING"


ROLES = ("general", "coding", "reasoning", "study", "fast", "vision")
MANUAL_MODES = ("auto",) + ROLES

TASK_ROLE = {
    TaskType.GENERAL_CHAT: "general",
    TaskType.CREATIVE: "general",
    TaskType.CODING: "coding",
    TaskType.DEBUGGING: "coding",
    TaskType.REASONING: "reasoning",
    TaskType.PLANNING: "reasoning",
    TaskType.RESEARCH: "reasoning",
    TaskType.STUDY: "study",
    TaskType.FILE_SEARCH: "fast",
    TaskType.FILE_OPERATION: "fast",
    TaskType.SYSTEM_CONTROL: "fast",
    TaskType.QUICK_COMMAND: "fast",
    TaskType.MESSAGING: "fast",
    TaskType.VISION: "vision",
}

# Where a role goes when its own model is missing. Vision deliberately does
# NOT fall back to a text model - a text model cannot see.
FALLBACK = {
    "general": ["general"],
    "coding": ["coding", "general"],
    "reasoning": ["reasoning", "general"],
    "study": ["study", "reasoning", "general"],
    "fast": ["fast", "general"],
    "vision": ["vision"],
}

# ---------------------------------------------------------------------------
# Intent classification (Hindi / Hinglish / English, rule-scored)
# ---------------------------------------------------------------------------
_W = lambda *ws: re.compile(r"\b(?:" + "|".join(ws) + r")\b", re.I)  # noqa: E731

_P = {
    "code_lang": _W(r"python", r"javascript", r"js", r"typescript", r"java", r"c\+\+", r"cpp",
                    r"rust", r"golang", r"go\s+lang", r"sql", r"html", r"css", r"react", r"node",
                    r"bash", r"php", r"kotlin", r"swift", r"flask", r"django"),
    "code_noun": _W(r"code", r"coding", r"script", r"program\w*", r"function", r"class",
                    r"api", r"regex", r"algorithm", r"website", r"web\s*site", r"app",
                    r"project", r"module", r"calculator", r"game", r"compile\w*", r"refactor\w*",
                    r"repo", r"repository"),
    "debug": _W(r"bug\w*", r"debug\w*", r"error", r"errors", r"exception", r"traceback",
                r"crash\w*", r"fix", r"theek\s+kar\w*", r"thik\s+kar\w*", r"galti",
                r"not\s+working", r"kaam\s+nahi", r"chal\s+nahi"),
    "make": _W(r"bana\w*", r"bnao", r"likh\w*", r"write", r"create", r"generate", r"build",
               r"develop", r"implement"),
    "study": _W(r"samjha\w*", r"samajh\w*", r"explain\w*", r"sikha\w*", r"seekh\w*",
                r"padha\w*", r"teach", r"learn", r"concept", r"meaning", r"matlab",
                r"kya\s+hota", r"kya\s+hai", r"what\s+is", r"how\s+does", r"why\s+does",
                r"notes", r"chapter", r"exam", r"theory"),
    "reason": _W(r"reason\w*", r"logic\w*", r"puzzle", r"riddle", r"prove", r"proof",
                 r"solve", r"calculate", r"math\w*", r"equation", r"derive", r"analy[sz]\w*",
                 r"sochke", r"soch\s+ke", r"step\s+by\s+step", r"hal\s+kar\w*"),
    "research": _W(r"research", r"compare", r"comparison", r"latest", r"news", r"pros\s+and\s+cons",
                   r"difference\s+between", r"fark", r"review"),
    "plan": _W(r"plan\w*", r"schedule", r"roadmap", r"routine", r"todo", r"to-do", r"timetable",
               r"strategy"),
    "creative": _W(r"poem", r"kavita", r"shayari", r"story", r"kahani", r"joke", r"song\s+lyrics",
                   r"lyrics", r"essay", r"caption", r"slogan", r"imagine"),
    "vision": _W(r"screen\s+par\s+kya", r"on\s+my\s+screen", r"what'?s\s+on\s+(?:my\s+)?screen",
                 r"screenshot\s+(?:dekh|padh|read|analy)\w*", r"image\s+(?:dekh|padh|read)\w*",
                 r"read\s+this\s+image", r"is\s+image", r"photo\s+(?:dekh|me\s+kya)\w*",
                 r"camera\s+(?:dekh|me)\w*", r"ocr", r"find\s+the\s+button", r"dekh\s+kar\s+batao"),
    "file_obj": _W(r"file", r"files", r"folder", r"folders", r"pdf", r"pdfs", r"photo", r"photos",
                   r"image", r"images", r"video", r"videos", r"mp4", r"mp3", r"zip", r"document",
                   r"documents", r"docx", r"excel", r"ppt", r"audio", r"song", r"songs"),
    "search": _W(r"dhund\w*", r"dhoond\w*", r"khoj\w*", r"find", r"search", r"locate", r"kaha\s+hai",
                 r"kahan\s+hai", r"list\s+kar\w*", r"dikhao"),
    "file_op": _W(r"move", r"copy", r"rename", r"naam\s+(?:change|badal)\w*", r"delete",
                  r"hata\w*", r"mita\w*", r"shift", r"paste", r"folder\s+bana\w*"),
    "system": _W(r"ram", r"cpu", r"gpu", r"memory\s+usage", r"disk", r"storage", r"space",
                 r"battery", r"wifi", r"wi-fi", r"network", r"ip", r"process\w*", r"volume",
                 r"brightness", r"shutdown", r"restart", r"sleep", r"lock", r"system\s+info\w*",
                 r"recycle\s+bin", r"trash", r"install\w*", r"uninstall\w*", r"update\w*",
                 r"download\s+kar\w*", r"download\s+do"),
    "open": _W(r"khol\w*", r"open", r"band\s+kar\w*", r"close", r"launch", r"start", r"chalu",
               r"mute", r"unmute", r"minimize", r"maximize"),
    "msg": _W(r"whats\s*app", r"watsapp", r"whatsap", r"message\s+bhej\w*", r"msg\s+bhej\w*"),
    "greet": _W(r"hi", r"hello", r"hey", r"namaste", r"kya\s+haal", r"kaise\s+ho", r"kaisi\s+ho",
                r"good\s+morning", r"good\s+night", r"thanks", r"thank\s+you", r"shukriya",
                r"dhanyavad", r"who\s+are\s+you", r"tum\s+kaun"),
}


@dataclass
class Classification:
    task: TaskType
    confidence: float
    scores: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)

    @property
    def role(self) -> str:
        return TASK_ROLE[self.task]


def classify_task(text: str) -> Classification:
    t = (text or "").strip()
    if not t:
        return Classification(TaskType.GENERAL_CHAT, 0.0)
    hit = {k: bool(p.search(t)) for k, p in _P.items()}
    words = len(t.split())
    s = {tt: 0.0 for tt in TaskType}
    why = []

    if hit["msg"]:
        s[TaskType.MESSAGING] += 3; why.append("messaging keyword")
    if hit["vision"]:
        s[TaskType.VISION] += 3; why.append("vision phrase")
    if hit["debug"] and (hit["code_lang"] or hit["code_noun"]):
        s[TaskType.DEBUGGING] += 3.2; why.append("bug/error + code")
    if hit["code_lang"] and (hit["make"] or hit["code_noun"]):
        s[TaskType.CODING] += 3; why.append("language + build/code")
    elif hit["code_noun"] and hit["make"] and not hit["file_obj"]:
        s[TaskType.CODING] += 2.2; why.append("build + code noun")
    elif hit["code_lang"]:
        s[TaskType.CODING] += 1.2
    if hit["search"] and hit["file_obj"]:
        s[TaskType.FILE_SEARCH] += 3; why.append("search + file object")
    if hit["file_op"] and hit["file_obj"]:
        s[TaskType.FILE_OPERATION] += 2.6; why.append("file operation")
    if hit["system"]:
        s[TaskType.SYSTEM_CONTROL] += 2; why.append("system keyword")
    if hit["open"] and words <= 7:
        s[TaskType.QUICK_COMMAND] += 2.4; why.append("short open/close command")
    if hit["study"]:
        s[TaskType.STUDY] += 2.2; why.append("explain/learn")
    if hit["reason"]:
        s[TaskType.REASONING] += 2.4; why.append("reasoning keyword")
    if hit["research"]:
        s[TaskType.RESEARCH] += 2; why.append("research keyword")
    if hit["plan"]:
        s[TaskType.PLANNING] += 2; why.append("planning keyword")
    if hit["creative"]:
        s[TaskType.CREATIVE] += 2.5; why.append("creative keyword")
    if hit["greet"] and words <= 6:
        s[TaskType.GENERAL_CHAT] += 2.5; why.append("greeting")

    # disambiguation
    if s[TaskType.CODING] and s[TaskType.QUICK_COMMAND]:
        s[TaskType.QUICK_COMMAND] -= 1.5         # "calculator app bana do" is not "open calculator"
    if s[TaskType.FILE_SEARCH] and s[TaskType.SYSTEM_CONTROL]:
        s[TaskType.SYSTEM_CONTROL] -= 1
    if s[TaskType.STUDY] and s[TaskType.CODING] and not hit["make"]:
        s[TaskType.CODING] -= 1                   # "python kya hai" -> study
    if s[TaskType.QUICK_COMMAND] and hit["file_obj"] and hit["search"]:
        s[TaskType.QUICK_COMMAND] -= 2

    best = max(s, key=lambda k: s[k])
    if s[best] <= 0:
        return Classification(TaskType.GENERAL_CHAT, 0.3, {k.value: v for k, v in s.items()},
                              ["no strong signal"])
    total = sum(v for v in s.values() if v > 0) or 1.0
    return Classification(best, round(s[best] / total, 2),
                          {k.value: v for k, v in s.items() if v}, why)


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------
@dataclass
class ModelEntry:
    role: str
    path: Path
    source: str          # "config" | "legacy" | "auto"

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def size_gb(self) -> float:
        try:
            return self.path.stat().st_size / (1024 ** 3)
        except Exception:
            return 0.0

    @property
    def name(self) -> str:
        return self.path.name


def _resolve(raw: str, models_dir: Path, project_root: Path):
    raw = str(raw or "").strip().strip('"')
    if not raw:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    cands = [p] if p.is_absolute() else [models_dir / p, project_root / p, p]
    for c in cands:
        if c.is_file():
            return c
        if c.is_dir():
            picks = sorted(c.glob("*.gguf"))
            if picks:
                return picks[0]
    return cands[0]


class ModelRegistry:
    def __init__(self, cfg: dict, models_dir: Path, project_root: Path, discover=None):
        self.models_dir = Path(models_dir)
        self.project_root = Path(project_root)
        self.entries: dict[str, ModelEntry] = {}
        cfg = cfg or {}
        configured = cfg.get("models") or {}
        if isinstance(configured, dict):
            for role, raw in configured.items():
                role = str(role).lower()
                if role in ROLES and str(raw or "").strip():
                    p = _resolve(raw, self.models_dir, self.project_root)
                    if p:
                        self.entries[role] = ModelEntry(role, p, "config")
        # legacy keys the old build shipped with
        if "general" not in self.entries and str(cfg.get("gguf_model_path") or "").strip():
            p = _resolve(cfg["gguf_model_path"], self.models_dir, self.project_root)
            if p:
                self.entries["general"] = ModelEntry("general", p, "legacy")
        for legacy_key in ("code_model_path", "code_model"):
            if "coding" not in self.entries and str(cfg.get(legacy_key) or "").strip():
                p = _resolve(cfg[legacy_key], self.models_dir, self.project_root)
                if p:
                    self.entries["coding"] = ModelEntry("coding", p, "legacy")
        # models/<role>/*.gguf
        for role in ROLES:
            if role in self.entries:
                continue
            sub = self.models_dir / role
            try:
                picks = sorted(sub.glob("*.gguf")) if sub.is_dir() else []
            except Exception:
                picks = []
            picks = [x for x in picks if "mmproj" not in x.name.lower()]
            if picks:
                self.entries[role] = ModelEntry(role, picks[0], "auto")
        # general: last-resort global discovery (models/*.gguf etc.)
        if "general" not in self.entries and discover is not None:
            try:
                found = [x for x in discover() if "mmproj" not in x.name.lower()]
            except Exception:
                found = []
            if found:
                self.entries["general"] = ModelEntry("general", found[0], "auto")

    def get(self, role: str):
        e = self.entries.get(role)
        return e if (e and e.exists) else None

    def available_roles(self) -> list:
        return [r for r in ROLES if self.get(r)]

    def vision_projector(self):
        """llava-style vision models need an mmproj file next to the weights."""
        e = self.get("vision")
        if not e:
            return None
        for cand in sorted(e.path.parent.glob("*mmproj*.gguf")):
            return cand
        return None

    def describe(self) -> list:
        rows = []
        for role in ROLES:
            e = self.entries.get(role)
            if e is None:
                rows.append({"role": role, "status": "not configured", "path": ""})
            else:
                rows.append({"role": role, "status": "installed" if e.exists else "MISSING FILE",
                             "path": str(e.path), "size_gb": round(e.size_gb, 2), "source": e.source})
        return rows


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
@dataclass
class RouteDecision:
    task: TaskType
    requested_role: str
    role: str | None
    path: Path | None
    manual: bool = False
    notes: list = field(default_factory=list)
    classification: Classification | None = None

    @property
    def available(self) -> bool:
        return self.path is not None

    @property
    def model_name(self) -> str:
        return self.path.name if self.path else "none"

    def summary(self) -> str:
        head = f"task={self.task.value} role={self.role or 'none'} model={self.model_name}"
        if self.manual:
            head += " (manual)"
        return head + (" | " + "; ".join(self.notes) if self.notes else "")


class ModelRouter:
    def __init__(self, registry: ModelRegistry, hw: hardware.HardwareInfo | None = None,
                 loaded_paths: list | None = None):
        self.registry = registry
        self.hw = hw
        self.loaded = {str(p).lower() for p in (loaded_paths or [])}

    def route(self, text: str, mode: str = "auto", task: TaskType | None = None) -> RouteDecision:
        cls = classify_task(text) if task is None else Classification(task, 1.0)
        mode = (mode or "auto").strip().lower()
        manual = mode in ROLES
        requested = mode if manual else TASK_ROLE[cls.task]
        notes = []
        chain = FALLBACK.get(requested, [requested, "general"])
        chosen = None
        for role in chain:
            e = self.registry.get(role)
            if e is None:
                if role == requested:
                    notes.append(f"no '{role}' model installed")
                continue
            if self.hw is not None and str(e.path).lower() not in self.loaded:
                if not hardware.model_fits(e.size_gb, self.hw):
                    notes.append(f"'{e.name}' ({e.size_gb:.1f} GB) does not fit free RAM")
                    continue
            chosen = e
            break
        if chosen is None and requested != "vision":
            # memory is tight: take the smallest installed text model that fits
            installed = [self.registry.get(r) for r in ("fast", "general", "coding", "reasoning", "study")]
            installed = sorted({e.path: e for e in installed if e}.values(), key=lambda e: e.size_gb)
            for e in installed:
                if self.hw is None or hardware.model_fits(e.size_gb, self.hw):
                    chosen = e
                    notes.append(f"low memory -> smallest model '{e.name}'")
                    break
        if chosen is not None and chosen.role != requested:
            notes.append(f"using '{chosen.role}' model instead")
        return RouteDecision(cls.task, requested, chosen.role if chosen else None,
                             chosen.path if chosen else None, manual, notes, cls)
