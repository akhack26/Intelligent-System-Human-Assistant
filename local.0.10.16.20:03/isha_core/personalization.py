"""Appearance / personalization settings and how they map onto the app.

Everything here is headless: the settings UI stores plain values in
isha_config.json and mani.py asks this module what they *mean* for the
prompt, the TTS/STT engines and the local inference budget.

Keys (all optional; an old config without them behaves exactly as before):

    response_language   ""|hi|en|ru|ja|es|fr|en-IN   ("" = reply in the user's language)
    user_name           free text, "" allowed
    work_category       one of WORK_OPTIONS ids, "" = prefer not to say
    user_instructions   user-wide instructions for every chat on this install
    speech_speed        slow|normal|fast   (absent = keep the legacy tts_rate)
    response_effort     low|medium|high|extra|max   (medium = the configured values)
    thinking_mode       existing checkbox; see inference_budget() / thinking_instruction()
"""
from __future__ import annotations

import math
import re

# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
# edge: Microsoft neural voices used by edge-tts (an ONLINE service); the first
# one is the default for that language. names: substrings that identify an
# installed offline voice (pyttsx3 / SAPI) for that language.
LANGUAGES = {
    "":      {"label": "Auto (same as the user)", "prompt": "", "stt": "en-IN", "edge": [],
              "prefixes": (), "names": (), "espeak": ""},
    "hi":    {"label": "Hindi", "prompt": "Hindi (Devanagari script)", "stt": "hi-IN",
              "edge": ["hi-IN-SwaraNeural", "hi-IN-MadhurNeural"], "prefixes": ("hi-",),
              "names": ("hindi", "hemant", "kalpana", "hi-in", "hi_in"), "espeak": "hi"},
    "en":    {"label": "English", "prompt": "English", "stt": "en-US",
              "edge": ["en-US-AriaNeural", "en-US-JennyNeural"], "prefixes": ("en-",),
              "names": ("english", "en-us", "en_us", "en-gb", "en_gb", "zira", "david", "hazel"),
              "espeak": "en"},
    "ru":    {"label": "Russian", "prompt": "Russian", "stt": "ru-RU",
              "edge": ["ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"], "prefixes": ("ru-",),
              "names": ("russian", "irina", "pavel", "ru-ru", "ru_ru"), "espeak": "ru"},
    "ja":    {"label": "Japanese", "prompt": "Japanese", "stt": "ja-JP",
              "edge": ["ja-JP-NanamiNeural", "ja-JP-KeitaNeural"], "prefixes": ("ja-",),
              "names": ("japanese", "haruka", "ayumi", "ichiro", "kyoko", "ja-jp", "ja_jp"), "espeak": "ja"},
    "es":    {"label": "Spanish", "prompt": "Spanish", "stt": "es-ES",
              "edge": ["es-ES-ElviraNeural", "es-MX-DaliaNeural"], "prefixes": ("es-",),
              "names": ("spanish", "helena", "laura", "pablo", "sabina", "es-es", "es_es", "es-mx"),
              "espeak": "es"},
    "fr":    {"label": "French", "prompt": "French", "stt": "fr-FR",
              "edge": ["fr-FR-DeniseNeural", "fr-FR-HenriNeural"], "prefixes": ("fr-",),
              "names": ("french", "hortense", "julie", "paul", "amelie", "fr-fr", "fr_fr"), "espeak": "fr"},
    "en-IN": {"label": "English (India)", "prompt": "Indian English", "stt": "en-IN",
              "edge": ["en-IN-NeerjaNeural", "en-IN-PrabhatNeural"], "prefixes": ("en-in-",),
              "names": ("india", "heera", "ravi", "en-in", "en_in"), "espeak": "en"},
}
LANGUAGE_ORDER = ("", "hi", "en", "ru", "ja", "es", "fr", "en-IN")


def language(cfg: dict) -> str:
    v = str((cfg or {}).get("response_language") or "").strip()
    for k in LANGUAGES:                       # case-insensitive, keeps the canonical key
        if k.lower() == v.lower():
            return k
    return ""


def stt_language(cfg: dict) -> str:
    """Language code for speech recognition. Auto keeps the old en-IN default."""
    return LANGUAGES[language(cfg)]["stt"]


def resolve_tts_voice(cfg: dict) -> tuple:
    """(edge_voice, note). The configured ``tts_voice`` is kept whenever it
    already speaks the chosen reply language; otherwise the language's default
    neural voice is used for replies (the setting itself is not rewritten)."""
    cfg = cfg or {}
    voice = str(cfg.get("tts_voice") or "en-US-AriaNeural").strip()
    lang = language(cfg)
    if not lang:
        return voice, ""
    info = LANGUAGES[lang]
    v = voice.lower()
    if lang == "en":
        ok = v.startswith("en-") and not v.startswith("en-in-")
    else:
        ok = any(v.startswith(p) for p in info["prefixes"])
    if ok:
        return voice, ""
    return info["edge"][0], f"voice '{voice}' does not speak {info['label']}; replies use {info['edge'][0]}"


def match_offline_voice(voices, lang: str):
    """Pick an installed offline voice for ``lang`` from pyttsx3-style voice
    objects (id / name / languages). Returns the voice id or None."""
    if not lang:
        return None
    names = LANGUAGES.get(lang, {}).get("names", ())
    if not names:
        return None
    for v in voices or []:
        blob = " ".join([str(getattr(v, "id", "") or ""), str(getattr(v, "name", "") or "")])
        try:
            for l in getattr(v, "languages", []) or []:
                blob += " " + (l.decode("utf-8", "ignore") if isinstance(l, bytes) else str(l))
        except Exception:
            pass
        b = blob.lower()
        if lang == "en" and ("en-in" in b or "india" in b):
            continue
        if any(n in b for n in names):
            return getattr(v, "id", None)
    return None


# ---------------------------------------------------------------------------
# Speech speed
# ---------------------------------------------------------------------------
SPEECH_SPEEDS = {"slow": "-25%", "normal": "+0%", "fast": "+25%"}
SPEECH_SPEED_LABELS = {"slow": "Slow", "normal": "Normal", "fast": "Fast"}


def _rate_pct(rate) -> int:
    m = re.match(r"\s*([+-]?\d+)\s*%", str(rate or ""))
    return int(m.group(1)) if m else 0


def speed_from_rate(rate) -> str:
    pct = _rate_pct(rate)
    return "slow" if pct <= -15 else "fast" if pct >= 15 else "normal"


def speech_speed(cfg: dict) -> str:
    v = str((cfg or {}).get("speech_speed") or "").strip().lower()
    return v if v in SPEECH_SPEEDS else speed_from_rate((cfg or {}).get("tts_rate"))


def speech_rate(cfg: dict) -> str:
    """edge-tts style rate string ("+25%"). Every backend (edge-tts, pyttsx3,
    SAPI, espeak) already derives its own rate from this percentage. Without a
    ``speech_speed`` key the legacy ``tts_rate`` is used unchanged."""
    cfg = cfg or {}
    v = str(cfg.get("speech_speed") or "").strip().lower()
    if v in SPEECH_SPEEDS:
        return SPEECH_SPEEDS[v]
    return str(cfg.get("tts_rate") or "+0%")


# ---------------------------------------------------------------------------
# Work category
# ---------------------------------------------------------------------------
WORK_OPTIONS = (
    ("", "Prefer not to say"),
    ("product_management", "Product management"),
    ("engineering", "Engineering"),
    ("human_resources", "Human resources"),
    ("finance", "Finance"),
    ("marketing", "Marketing"),
    ("sales", "Sales"),
    ("operations", "Operations"),
    ("data_science", "Data science"),
    ("design", "Design"),
    ("legal", "Legal"),
    ("scientist", "Scientist"),
    ("student", "Student"),
    ("founder", "Founder"),
    ("healthcare", "Healthcare"),
    ("writer", "Writer"),
    ("educator", "Educator"),
    ("software_engineer", "Software engineer"),
)
_WORK_LABEL = dict(WORK_OPTIONS)


def work_label(cfg: dict) -> str:
    v = str((cfg or {}).get("work_category") or "").strip()
    if v in _WORK_LABEL:
        return _WORK_LABEL[v]
    # tolerate a label typed/stored directly
    for k, lbl in WORK_OPTIONS:
        if lbl.lower() == v.lower():
            return lbl
    return ""


def clean_name(value) -> str:
    v = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    v = re.sub(r"[\[\]{}<>`]", "", v)
    return v[:60]


def clean_instructions(value, limit: int = 2000) -> str:
    v = str(value or "").replace("\r\n", "\n").strip()
    return v[:limit]


# ---------------------------------------------------------------------------
# Effort + thinking -> local inference budget
# ---------------------------------------------------------------------------
EFFORTS = ("low", "medium", "high", "extra", "max")
EFFORT_LABELS = {"low": "Low", "medium": "Medium (default)", "high": "High", "extra": "Extra", "max": "Max"}
# (reply-token multiplier, agent-step multiplier) relative to max_tokens / agent_max_steps.
_EFFORT_MULT = {"low": (0.5, 0.5), "medium": (1.0, 1.0), "high": (1.5, 1.5),
                "extra": (2.0, 2.0), "max": (3.0, 3.0)}
COMPLEX_TASKS = {"CODING", "DEBUGGING", "REASONING", "PLANNING", "RESEARCH", "STUDY"}


def effort(cfg: dict) -> str:
    v = str((cfg or {}).get("response_effort") or "medium").strip().lower()
    return v if v in EFFORTS else "medium"


def is_complex(task: str, text: str = "") -> bool:
    return str(task or "").upper() in COMPLEX_TASKS or len((text or "").split()) > 40


def inference_budget(cfg: dict, task: str = "", text: str = "") -> dict:
    """Map effort (+ thinking on complex tasks) to concrete local limits.

    Medium reproduces the configured ``max_tokens`` / ``agent_max_steps``
    exactly. Higher levels allow longer replies and more agent steps, so a
    request can take longer and use more local CPU/GPU time - nothing else.
    Reply tokens are capped at 60% of the context window.
    """
    cfg = cfg or {}
    lvl = effort(cfg)
    tm, sm = _EFFORT_MULT[lvl]
    base_tok = max(16, int(cfg.get("max_tokens", 512) or 512))
    base_steps = max(1, int(cfg.get("agent_max_steps", 8) or 8))
    tok = base_tok * tm
    steps = base_steps * sm
    thinking = bool(cfg.get("thinking_mode", False)) and is_complex(task, text)
    if lvl == "medium" and not thinking:
        # exactly the configured values - an old config behaves as before
        return {"effort": lvl, "max_tokens": base_tok, "agent_max_steps": base_steps, "thinking": False}
    if thinking:
        tok *= 1.5
        steps += 2
    ctx = max(512, int(cfg.get("context_length", 4096) or 4096))
    tok = int(max(96, min(tok, ctx * 0.6)))
    steps = int(max(2, min(40, math.ceil(steps))))
    return {"effort": lvl, "max_tokens": tok, "agent_max_steps": steps, "thinking": thinking}


def thinking_instruction(agent: bool = False) -> str:
    """Added to the system prompt when Thinking Mode is on AND the task is
    complex. It asks for careful *private* work; the reply stays clean and
    <think> blocks are stripped from display and speech anyway."""
    step = ("Before your first tool call, decide the steps you need; after each result, check it "
            "against the goal. " if agent else "")
    return ("[THINKING MODE - complex request] Work through this carefully and privately before "
            "answering: pin down the goal, check assumptions and edge cases, and verify numbers, "
            "code or logic. " + step +
            "Do NOT show your reasoning, notes or <think> blocks. Give only the final answer, "
            "clear and as brief as the task allows.")


# ---------------------------------------------------------------------------
# Prompt block
# ---------------------------------------------------------------------------
def prompt_block(cfg: dict) -> str:
    """User-wide preferences for the system prompt ("" when nothing is set)."""
    cfg = cfg or {}
    lines = []
    name = clean_name(cfg.get("user_name"))
    if name:
        lines.append(f"- The user's name is {name}. Use it naturally and sparingly, not in every reply.")
    work = work_label(cfg)
    if work:
        lines.append(f"- The user's work: {work}. Use it to pick relevant examples and vocabulary.")
    lang = language(cfg)
    if lang:
        lines.append(f"- Reply language: {LANGUAGES[lang]['prompt']}. Write every reply in it, even if the "
                     "user writes in another language, unless this message explicitly asks for a "
                     "different language. (This replaces the 'reply in the user's language' rule.)")
    instr = clean_instructions(cfg.get("user_instructions"))
    if instr:
        lines.append("- The user's standing instructions for ISHA:\n" +
                     "\n".join("    " + ln for ln in instr.splitlines() if ln.strip()))
    if not lines:
        return ""
    return ("[USER PREFERENCES - set by the user in ISHA settings. They shape tone and content but never "
            "override ISHA's safety rules, tool permission checks or the current request.]\n" + "\n".join(lines))
