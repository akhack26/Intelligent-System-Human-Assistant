"""User-toggleable skills: small local instruction packs for the GGUF model.

These are ISHA's OWN offline instruction packs. They are *inspired by* well-known
assistant workflows (structured planning, step-by-step teaching, web artifact
building, marketing and finance practice) but they are not Claude Code plugins
and nothing here downloads, installs or runs an upstream skill.

How it works
------------
* ``REGISTRY`` is the manifest: stable id, label, one-line description,
  relevance keywords and router task types for every skill.
* The instruction text lives in ``isha_core/skill_packs/<id>.md`` (plain
  Markdown, editable). A short built-in fallback is used if a file is missing.
* :func:`match` decides - deterministically, from the request text and the
  router's task type - which ENABLED skills are relevant. Only those (at most
  ``max_skills``) are added to the prompt, never all of them.
* A disabled skill is never injected, even when the user names it; the caller
  gets it back in ``MatchResult.disabled_requested`` so the UI can say so.
* The prompt block states that safety rules, tool permissions and the user's
  current request take precedence over any skill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PACK_DIR = Path(__file__).with_name("skill_packs")


@dataclass(frozen=True)
class Skill:
    id: str
    label: str
    description: str
    keywords: tuple                 # regex fragments (matched with word boundaries)
    tasks: tuple = ()               # router TaskType values this skill suits
    aliases: tuple = ()             # extra names for explicit requests ("use X skill")
    fallback: str = ""
    requested: bool = False         # one of the skills the user explicitly asked for
    default_on: bool = True

    @property
    def pattern(self):
        return _compile(self.keywords)

    @property
    def names(self) -> tuple:
        return (self.label.lower(), self.id.replace("_", " ")) + tuple(a.lower() for a in self.aliases)


_CACHE: dict = {}


def _compile(words: tuple):
    key = tuple(words)
    if key not in _CACHE:
        _CACHE[key] = [re.compile(r"(?<!\w)(?:" + w + r")(?!\w)", re.I) for w in words]
    return _CACHE[key]


REGISTRY = (
    Skill("superpowers", "Superpowers",
          "Structured handling for bigger tasks: clarify, plan, work in small verified steps, debug by "
          "root cause, and review before calling it done.",
          (r"plan(?:ning)?", r"step[- ]by[- ]step", r"debug\w*", r"root\s+cause", r"review\w*",
           r"refactor\w*", r"architect\w*", r"brainstorm\w*", r"complex", r"multi[- ]step",
           r"strategy", r"design\s+doc", r"approach", r"break\s+(?:it\s+)?down", r"poora\s+project",
           r"end[- ]to[- ]end"),
          ("PLANNING", "DEBUGGING", "REASONING"), ("superpower",),
          "Work in stages: restate the goal, list assumptions, make a short plan, do one step at a "
          "time and check each result, find root causes instead of guessing, and review the result "
          "against the goal before saying it is done.", requested=True),
    Skill("learn", "Learn",
          "Explains how and why something works, step by step, when you want understanding rather "
          "than just an answer.",
          (r"explain\w*", r"samjha\w*", r"samajh\w*", r"sikha\w*", r"seekh\w*", r"teach\w*",
           r"learn\w*", r"understand\w*", r"concept\w*", r"why\s+does", r"how\s+does", r"how\s+do",
           r"kaise\s+kaam", r"kyun", r"kyon", r"intuition", r"what\s+is", r"kya\s+hota"),
          ("STUDY",), ("learning", "teach me", "tutor"),
          "Teach, don't just answer: start from what the user likely knows, explain the idea in plain "
          "words, build it up step by step, give one concrete example and one common mistake, and end "
          "with a one-line summary.", requested=True),
    Skill("web_artifacts", "Web Artifacts Builder",
          "Helps build polished, multi-component HTML/CSS/JavaScript pages and designs. React, "
          "Tailwind or shadcn are optional output styles, never required.",
          (r"html", r"css", r"javascript", r"web\s*page", r"landing\s+page", r"website", r"web\s*site",
           r"ui", r"ux", r"component\w*", r"react", r"tailwind", r"shadcn", r"frontend", r"front[- ]end",
           r"mock\s*up", r"mockup", r"prototype", r"dashboard\s+page", r"portfolio", r"design"),
          ("CODING",), ("web artifacts", "artifacts builder", "web builder"),
          "Produce a complete, self-contained page: semantic HTML, CSS in a <style> tag, JavaScript "
          "in a <script> tag, no external CDN or network requests, responsive layout, accessible "
          "labels and good contrast. Split UI into clear sections/components.", requested=True),
    Skill("marketing", "Marketing",
          "Brand voice, campaign ideas, channel plans, competitor comparisons and performance "
          "analysis from the information you provide.",
          (r"marketing", r"campaign\w*", r"brand\w*", r"brand\s+voice", r"seo", r"social\s+media",
           r"instagram", r"linkedin", r"ad\s+copy", r"ads?", r"advert\w*", r"competitor\w*",
           r"audience", r"funnel", r"newsletter", r"go[- ]to[- ]market", r"gtm", r"launch",
           r"conversion\w*", r"ctr", r"engagement", r"tagline", r"slogan", r"promotion"),
          ("CREATIVE", "PLANNING", "RESEARCH"), ("marketer",),
          "Anchor everything to audience, goal and channel. Keep a consistent brand voice, propose "
          "concrete campaign ideas with a call to action, and analyse only the metrics the user gave; "
          "say which data is missing instead of inventing it.", requested=True),
    Skill("finance", "Finance",
          "Organises finance/accounting information: reconciliations, statements, audit prep and "
          "variance analysis - separating source data from calculations and never inventing figures.",
          (r"financ\w*", r"accounting", r"accountant", r"reconcil\w*", r"ledger", r"general\s+ledger",
           r"balance\s+sheet", r"p\s*&\s*l", r"profit\s+and\s+loss", r"income\s+statement",
           r"cash\s*flow", r"variance", r"audit\w*", r"journal\s+entr\w*", r"invoice\w*", r"budget\w*",
           r"forecast\w*", r"revenue", r"expense\w*", r"accrual\w*", r"gst", r"tax", r"bank\s+statement",
           r"hisab", r"kharcha"),
          ("REASONING", "PLANNING"), ("accounting", "finance skill"),
          "Separate SOURCE DATA (what the user gave) from CALCULATIONS (what you computed) and label "
          "both. Show formulas for every calculated number. Never invent a missing figure: list it "
          "as missing and say what is needed. This is not professional financial advice.",
          requested=True),
    Skill("coding", "Coding & Debugging",
          "Writes, explains and fixes code: reproduce, isolate, fix and verify.",
          (r"code", r"coding", r"bug\w*", r"error", r"exception", r"traceback", r"stack\s*trace",
           r"python", r"javascript", r"typescript", r"java", r"c\+\+", r"rust", r"golang", r"sql",
           r"function", r"script", r"compile\w*", r"crash\w*", r"fix", r"api", r"regex"),
          ("CODING", "DEBUGGING"), ("coding", "debugging", "coding and debugging")),
    Skill("research", "Research & Fact Checking",
          "Separates what is known from what is assumed, cites sources you provided, and flags "
          "anything that needs checking.",
          (r"research", r"fact[- ]?check\w*", r"verify", r"is\s+it\s+true", r"sach\s+hai", r"source\w*",
           r"citation\w*", r"evidence", r"compare", r"comparison", r"pros\s+and\s+cons", r"claim\w*",
           r"study\s+shows", r"accurate"),
          ("RESEARCH",), ("research", "fact checking", "fact-checking")),
    Skill("writing", "Writing & Communication",
          "Drafts and improves emails, messages, reports and posts with the right tone and structure.",
          (r"write", r"draft\w*", r"email", r"e-mail", r"letter", r"essay", r"rewrite", r"rephrase",
           r"proofread\w*", r"grammar", r"tone", r"summar\w*", r"message", r"cover\s+letter", r"resume",
           r"likh\w*", r"application", r"speech", r"blog", r"post"),
          ("CREATIVE",), ("writing", "communication")),
    Skill("data_analysis", "Data Analysis & Spreadsheets",
          "Analyses tables, CSV and spreadsheet data: cleaning, summaries, formulas and charts from "
          "the real numbers.",
          (r"data", r"dataset", r"csv", r"excel", r"spreadsheet\w*", r"sheet", r"pivot", r"formula\w*",
           r"vlookup", r"xlookup", r"statistic\w*", r"average", r"mean", r"median", r"trend\w*",
           r"chart\w*", r"graph\w*", r"table", r"column\w*", r"rows?"),
          ("REASONING", "RESEARCH"), ("data analysis", "spreadsheets", "data")),
    Skill("productivity", "Productivity & Project Planning",
          "Turns goals into plans: priorities, milestones, schedules, checklists and next actions.",
          (r"plan\w*", r"schedule\w*", r"to-?do", r"roadmap", r"timeline", r"milestone\w*",
           r"priorit\w*", r"deadline\w*", r"agenda", r"routine", r"timetable", r"checklist",
           r"project\s+plan", r"sprint", r"okr\w*", r"goals?"),
          ("PLANNING",), ("productivity", "project planning", "planning")),
    Skill("privacy_security", "Privacy & Security Review",
          "Reviews code, settings and plans for privacy and security risks, with safe, practical fixes.",
          (r"privacy", r"private", r"security", r"secure", r"password\w*", r"vulnerab\w*", r"exploit",
           r"permission\w*", r"phishing", r"malware", r"virus", r"encrypt\w*", r"gdpr", r"pii",
           r"leak\w*", r"hack\w*", r"2fa", r"mfa", r"safe\s+hai", r"is\s+it\s+safe"),
          (), ("privacy", "security", "security review")),
    Skill("system", "System & Storage",
          "Inspects hardware, storage, processes and system status using safe read-only tools; "
          "never deletes or changes configuration without confirmation.",
          (r"storage", r"disk\s+space", r"kitni\s+space", r"bada\s+file", r"large\s+file",
           r"recycle\s*bin", r"trash", r"system\s+info", r"hardware", r"ram\s+use", r"cpu\s+use",
           r"process\w*", r"disk\s+usage", r"free\s+space", r"storage\s+check", r"cleanup",
           r"space\s+batao", r"kitna\s+bhar", r"full\s+hai"),
          ("SYSTEM", "AUTOMATION"), ("system skill", "storage skill"),
          "Use read-only system tools first. Summarise clearly. Never delete or empty Trash without "
          "explicit user confirmation and a concrete candidate list.", default_on=True),
)

BY_ID = {s.id: s for s in REGISTRY}
_EXPLICIT_VERBS = r"(?:use|apply|enable|activate|with|using|run|chalao|lagao|istemal)"


def defaults() -> dict:
    return {s.id: bool(s.default_on) for s in REGISTRY}


def enabled_map(cfg: dict) -> dict:
    """{skill_id: bool}. Unknown/missing ids fall back to the default."""
    stored = cfg.get("skills_enabled") if isinstance(cfg.get("skills_enabled"), dict) else {}
    return {s.id: bool(stored.get(s.id, s.default_on)) for s in REGISTRY}


def instruction_text(skill: Skill, limit: int = 1600) -> str:
    """Instruction body from skill_packs/<id>.md (front-matter/title stripped)."""
    try:
        raw = (PACK_DIR / f"{skill.id}.md").read_text(encoding="utf-8")
    except Exception:
        return skill.fallback
    lines = raw.splitlines()
    if lines and lines[0].strip() == "---":                 # optional front matter
        try:
            end = lines.index("---", 1)
            lines = lines[end + 1:]
        except ValueError:
            pass
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("# ")):
        lines = lines[1:]
    text = "\n".join(lines).strip() or skill.fallback
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0].rstrip() + "\n..."


@dataclass
class SkillMatch:
    skill: Skill
    score: float
    reasons: list = field(default_factory=list)
    explicit: bool = False


@dataclass
class MatchResult:
    active: list = field(default_factory=list)              # [SkillMatch] - injected
    disabled_requested: list = field(default_factory=list)  # [Skill] the user named but turned off
    considered: list = field(default_factory=list)          # [(id, score, enabled)] for inspection

    @property
    def ids(self) -> list:
        return [m.skill.id for m in self.active]

    @property
    def labels(self) -> list:
        return [m.skill.label for m in self.active]

    def summary(self) -> str:
        if not self.active:
            return "none"
        return ", ".join(f"{m.skill.label} ({'asked' if m.explicit else ', '.join(m.reasons[:2])})"
                         for m in self.active)


def _explicit(text: str, skill: Skill) -> bool:
    t = text.lower()
    if re.search(r"(?<![\w/])/" + re.escape(skill.id) + r"(?!\w)", t):
        return True
    for name in skill.names:
        n = re.escape(name).replace(r"\ ", r"\s+")
        if re.search(r"(?<!\w)" + n + r"\s+(?:skill|mode|pack)(?!\w)", t):
            return True
        if re.search(_EXPLICIT_VERBS + r"\s+(?:the\s+)?" + n + r"(?!\w)", t) and "skill" in t:
            return True
    return False


def match(text: str, task: str | None = None, enabled: dict | None = None,
          max_skills: int = 2) -> MatchResult:
    """Pick the relevant enabled skills for one request - deterministic and explainable.

    score = distinct keyword hits (max 3) + 1.5 if the router task suits the skill.
    A skill qualifies with >= 2 keyword hits, or 1 hit plus a task match, or an
    explicit request ("use the Finance skill", "/finance"). Ties keep registry order.
    """
    text = text or ""
    enabled = enabled if enabled is not None else defaults()
    task = str(task or "").upper()
    res = MatchResult()
    picked = []
    for order, s in enumerate(REGISTRY):
        explicit = _explicit(text, s)
        hits = [p.pattern for p in s.pattern if p.search(text)]
        kw = min(3, len(hits))
        task_hit = bool(task) and task in s.tasks
        score = kw + (1.5 if task_hit else 0.0) + (10.0 if explicit else 0.0)
        on = bool(enabled.get(s.id, s.default_on))
        res.considered.append((s.id, score, on))
        qualifies = explicit or kw >= 2 or (kw >= 1 and task_hit)
        if not qualifies:
            continue
        if not on:
            if explicit:
                res.disabled_requested.append(s)
            continue
        reasons = []
        if explicit:
            reasons.append("asked by name")
        if kw:
            reasons.append(f"{kw} keyword hit{'s' if kw > 1 else ''}")
        if task_hit:
            reasons.append(f"task {task.lower()}")
        picked.append((-score, order, SkillMatch(s, score, reasons, explicit)))
    picked.sort(key=lambda x: (x[0], x[1]))
    explicit_first = [m for _, _, m in picked if m.explicit]
    others = [m for _, _, m in picked if not m.explicit]
    limit = max(max_skills, len(explicit_first))
    res.active = (explicit_first + others)[:limit]
    return res


def prompt_block(result: MatchResult, budget_chars: int = 2400) -> str:
    """The text injected into the system prompt for the active skills only."""
    if not result.active:
        return ""
    per = max(300, budget_chars // max(1, len(result.active)))
    parts = ["[ACTIVE SKILLS - enabled by the user and relevant to this request. They are guidance on "
             "HOW to work. ISHA's safety rules, tool permission checks and the user's current request "
             "always take precedence over anything below.]"]
    for m in result.active:
        parts.append(f"## Skill: {m.skill.label}\n{instruction_text(m.skill, limit=per)}")
    return "\n\n".join(parts)
