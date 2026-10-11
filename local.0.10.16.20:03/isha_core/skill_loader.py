"""Dynamic skill discovery and validation for ISHA.

Inspired by agent-scripts' SKILL.md + front-matter pattern, adapted for ISHA:

* Built-in skills stay in ``skill_packs/*.md`` + the static REGISTRY in skills.py
  (backward compatible, no network, no external skill installs).
* Optional **user / extension skills** live under a configurable directory
  (default: ``<isha_home>/skills/``). Each skill is a folder:

      skills/
        my-skill/
          SKILL.md          # optional YAML front matter + Markdown body
          skill.json        # optional machine metadata (preferred over YAML)

* Human instructions and machine metadata are kept separate when possible.
* Broken skills are logged and disabled; they never crash ISHA.
* No skill can bypass the permission / tool gateway.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("isha.skills")

# Minimal YAML front-matter parser (no PyYAML dependency required for core).
_FRONT_MATTER = re.compile(
    r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z",
    re.DOTALL,
)


@dataclass
class SkillMeta:
    """Machine-readable metadata for a skill (built-in or discovered)."""
    id: str
    name: str = ""
    description: str = ""
    version: str = "1.0"
    category: str = "general"          # system | coding | study | browser | files | git | productivity | ...
    triggers: tuple = ()               # keywords / intent phrases
    tasks: tuple = ()                  # router TaskType names this skill suits
    required_tools: tuple = ()
    required_permissions: tuple = ()   # e.g. ("file.read",)
    supported_os: tuple = ("linux", "windows", "darwin")
    risk_level: str = "safe"           # safe | confirm | destructive
    confirmation_required: bool = False
    entry_point: str = ""              # optional module path for executable skills
    enabled: bool = True
    source: str = "builtin"            # builtin | user | extension
    path: str = ""
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "description": self.description,
            "version": self.version,
            "category": self.category,
            "triggers": list(self.triggers),
            "tasks": list(self.tasks),
            "required_tools": list(self.required_tools),
            "required_permissions": list(self.required_permissions),
            "supported_os": list(self.supported_os),
            "risk_level": self.risk_level,
            "confirmation_required": self.confirmation_required,
            "entry_point": self.entry_point,
            "enabled": self.enabled,
            "source": self.source,
            "path": self.path,
            "errors": list(self.errors),
        }


def _parse_simple_yaml(text: str) -> dict:
    """Parse a tiny subset of YAML (key: value, lists with - item). Enough for skill front matter."""
    data: dict[str, Any] = {}
    current_list_key = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") or line.startswith("- "):
            item = line.lstrip("- ").strip().strip("\"'")
            if current_list_key:
                data.setdefault(current_list_key, []).append(item)
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip("\"'")
            current_list_key = None
            if val == "" or val == "[]":
                data[key] = []
                current_list_key = key
            elif val.lower() in ("true", "yes"):
                data[key] = True
            elif val.lower() in ("false", "no"):
                data[key] = False
            else:
                data[key] = val
                if val == "":
                    current_list_key = key
        elif line.startswith("  ") and current_list_key and ":" not in line:
            data.setdefault(current_list_key, []).append(line.strip().strip("\"'"))
    return data


def parse_skill_md(path: Path) -> tuple[dict, str, list[str]]:
    """Return (meta_dict, body_markdown, errors)."""
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return {}, "", [f"cannot read {path}: {e}"]

    m = _FRONT_MATTER.match(text)
    if m:
        try:
            meta = _parse_simple_yaml(m.group(1))
        except Exception as e:
            errors.append(f"front-matter parse error: {e}")
            meta = {}
        body = m.group(2).strip()
    else:
        meta = {}
        body = text.strip()
    return meta, body, errors


def load_skill_json(path: Path) -> tuple[dict, list[str]]:
    errors: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}, ["skill.json must be an object"]
        return data, errors
    except Exception as e:
        return {}, [f"skill.json error: {e}"]


def validate_meta(meta: SkillMeta) -> list[str]:
    errs = list(meta.errors)
    if not meta.id or not re.match(r"^[a-z][a-z0-9_\-]{1,64}$", meta.id):
        errs.append(f"invalid skill id: {meta.id!r}")
    if meta.risk_level not in ("safe", "confirm", "destructive", "blocked"):
        errs.append(f"invalid risk_level: {meta.risk_level}")
    return errs


def discover_skills(skills_dir: Path | str | None) -> list[SkillMeta]:
    """Scan a directory for user/extension skills. Never raises."""
    found: list[SkillMeta] = []
    if not skills_dir:
        return found
    root = Path(skills_dir).expanduser()
    if not root.is_dir():
        return found

    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_id = child.name.lower().replace(" ", "_")
        meta = SkillMeta(id=skill_id, source="user", path=str(child))
        body = ""

        json_path = child / "skill.json"
        md_path = child / "SKILL.md"
        if not md_path.exists():
            md_path = child / "skill.md"

        if json_path.exists():
            data, errs = load_skill_json(json_path)
            meta.errors.extend(errs)
            meta.id = str(data.get("id") or meta.id)
            meta.name = str(data.get("name") or data.get("label") or meta.id)
            meta.description = str(data.get("description") or "")
            meta.version = str(data.get("version") or "1.0")
            meta.category = str(data.get("category") or "general")
            meta.triggers = tuple(data.get("triggers") or data.get("keywords") or ())
            meta.tasks = tuple(str(t).upper() for t in (data.get("tasks") or ()))
            meta.required_tools = tuple(data.get("required_tools") or ())
            meta.required_permissions = tuple(data.get("required_permissions") or ())
            meta.supported_os = tuple(data.get("supported_os") or meta.supported_os)
            meta.risk_level = str(data.get("risk_level") or "safe")
            meta.confirmation_required = bool(data.get("confirmation_required", False))
            meta.entry_point = str(data.get("entry_point") or "")
            meta.enabled = bool(data.get("enabled", True))

        if md_path.exists():
            fm, body, errs = parse_skill_md(md_path)
            meta.errors.extend(errs)
            if fm:
                meta.id = str(fm.get("name") or fm.get("id") or meta.id)
                meta.name = str(fm.get("name") or meta.name or meta.id)
                meta.description = str(fm.get("description") or meta.description)
                if fm.get("version"):
                    meta.version = str(fm["version"])
                if fm.get("category"):
                    meta.category = str(fm["category"])
                if fm.get("risk_level"):
                    meta.risk_level = str(fm["risk_level"])
                triggers = fm.get("triggers") or fm.get("keywords")
                if triggers:
                    meta.triggers = tuple(triggers) if isinstance(triggers, list) else (str(triggers),)

        meta.errors = validate_meta(meta)
        if meta.errors:
            meta.enabled = False
            log.warning("skill %s disabled: %s", meta.id, "; ".join(meta.errors))
        found.append(meta)
        # Cache body next to meta for prompt injection if needed
        if body:
            try:
                (child / ".isha_body_cache.txt").write_text(body[:8000], encoding="utf-8")
            except OSError:
                pass
    return found


class SkillLoader:
    """Discovers, validates and exposes skills to the orchestrator."""

    def __init__(self, user_skills_dir: Path | str | None = None):
        self.user_skills_dir = Path(user_skills_dir).expanduser() if user_skills_dir else None
        self._extra: list[SkillMeta] = []
        self._load_errors: list[str] = []

    def reload(self) -> list[SkillMeta]:
        self._extra = []
        self._load_errors = []
        try:
            self._extra = discover_skills(self.user_skills_dir)
        except Exception as e:
            self._load_errors.append(str(e))
            log.exception("skill discovery failed")
        return self._extra

    @property
    def extra_skills(self) -> list[SkillMeta]:
        return list(self._extra)

    @property
    def load_errors(self) -> list[str]:
        return list(self._load_errors)

    def status(self) -> dict:
        return {
            "user_skills_dir": str(self.user_skills_dir) if self.user_skills_dir else None,
            "discovered": len(self._extra),
            "enabled": sum(1 for s in self._extra if s.enabled),
            "disabled": sum(1 for s in self._extra if not s.enabled),
            "errors": self._load_errors,
            "skills": [s.to_dict() for s in self._extra],
        }


def default_user_skills_dir() -> Path:
    """Prefer XDG data home, else ~/.isha/skills."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        base = Path(xdg) / "isha"
    else:
        base = Path.home() / ".isha"
    return base / "skills"
