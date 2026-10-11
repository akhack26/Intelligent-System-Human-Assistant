"""Three user-selectable GGUF model slots (the "model picker").

The user keeps up to three GGUF files. Each slot has a display name, a path
and a small set of *roles* (general / coding / reasoning / study / fast /
vision) that tell Auto routing which tasks it may handle::

    "model_slots": [
        {"name": "Qwen 3B",  "path": "models/general/qwen.gguf", "roles": ["general", "fast"]},
        {"name": "Coder 7B", "path": "D:/AI/coder.gguf",         "roles": ["coding"]},
        {"name": "",         "path": "",                         "roles": ["reasoning", "study"]}
    ],
    "model_selection": "auto"        # auto | slot1 | slot2 | slot3

Three-slot mode is active as soon as at least one slot has a path. From then
on routing only ever chooses among the configured slots - no directory
auto-discovery - so a fourth .gguf lying in models/ or ~/Downloads is never
picked silently. With all three slots empty ISHA keeps the legacy behaviour
(per-role ``models`` paths, ``gguf_model_path`` / ``code_model_path`` and
models/<role>/ folders).

Migration from those legacy keys happens in memory on load and only reaches
isha_config.json when the user saves settings. Model files are referenced,
never moved, copied or deleted.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import router

SLOT_IDS = ("slot1", "slot2", "slot3")
SELECTIONS = ("auto",) + SLOT_IDS
ROLE_ORDER = ("general", "coding", "reasoning", "study", "fast", "vision")
ROLE_LABELS = {"general": "General", "coding": "Coding", "reasoning": "Reasoning",
               "study": "Study", "fast": "Fast", "vision": "Vision"}
# Roles an empty slot gets, so that filling in a path "just works".
DEFAULT_ROLES = (["general", "fast"], ["coding"], ["reasoning", "study"])


def _clean_roles(raw) -> list:
    out = []
    for r in raw if isinstance(raw, (list, tuple)) else []:
        r = str(r).strip().lower()
        if r in ROLE_ORDER and r not in out:
            out.append(r)
    return out


def empty_slot(index: int) -> dict:
    return {"name": "", "path": "", "roles": list(DEFAULT_ROLES[index])}


def normalise_slots(raw) -> list:
    """Exactly three well-formed slot dicts, whatever was stored."""
    slots = []
    items = raw if isinstance(raw, list) else []
    for i in range(3):
        src = items[i] if i < len(items) and isinstance(items[i], dict) else {}
        slot = {
            "name": str(src.get("name") or "").strip()[:40],
            "path": str(src.get("path") or "").strip().strip('"'),
            "roles": _clean_roles(src.get("roles")) if "roles" in src else list(DEFAULT_ROLES[i]),
        }
        slots.append(slot)
    return slots


def normalise_selection(value) -> str:
    v = str(value or "auto").strip().lower().replace(" ", "")
    v = {"model1": "slot1", "model2": "slot2", "model3": "slot3", "1": "slot1", "2": "slot2",
         "3": "slot3"}.get(v, v)
    return v if v in SELECTIONS else "auto"


def slot_index(slot_id: str) -> int:
    return SLOT_IDS.index(slot_id) if slot_id in SLOT_IDS else -1


def display_name(slot: dict, index: int) -> str:
    """'Model 2 · Coder 7B' style label used by every picker."""
    name = str(slot.get("name") or "").strip()
    if not name and slot.get("path"):
        name = Path(str(slot["path"])).stem[:28]
    return f"Model {index + 1}" + (f" · {name}" if name else "")


def norm_key(raw: str, models_dir: Path | None = None, project_root: Path | None = None) -> str:
    p = None
    if models_dir is not None and project_root is not None:
        try:
            p = router._resolve(raw, Path(models_dir), Path(project_root))
        except Exception:
            p = None
    if p is None:
        p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
    return os.path.normcase(os.path.normpath(str(p)))


def legacy_refs(cfg: dict) -> list:
    """[(role, raw_path)] for every legacy model setting that is filled in."""
    models = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
    refs = []
    for role in ROLE_ORDER:
        raw = str(models.get(role) or "").strip()
        if not raw and role == "general":
            raw = str(cfg.get("gguf_model_path") or "").strip()
        if not raw and role == "coding":
            raw = str(cfg.get("code_model_path") or cfg.get("code_model") or "").strip()
        if raw:
            refs.append((role, raw))
    return refs


def _exists(raw: str, models_dir, project_root) -> bool:
    if models_dir is None or project_root is None:
        return Path(os.path.expandvars(os.path.expanduser(str(raw)))).is_file()
    try:
        p = router._resolve(raw, Path(models_dir), Path(project_root))
        return p is not None and p.is_file()
    except Exception:
        return False


def migrate(cfg: dict, models_dir: Path | None = None, project_root: Path | None = None,
            discover=None) -> dict:
    """Make sure cfg has ``model_slots`` + ``model_selection``. In memory only.

    Without ``model_slots`` in the config, the slots are filled from what the
    LEGACY rules resolve right now - per-role ``models`` paths, the legacy
    ``gguf_model_path`` / ``code_model_path`` / ``code_model`` keys, the
    models/<role>/ folders and (for general) global auto-detection - so the
    same files keep serving the same tasks. Only files that exist are used (an
    old Ollama tag or a moved file never switches a working setup into slot
    mode). Roles that resolve to the same file share one slot, in the order
    general, coding, reasoning, study, fast, vision. Anything beyond three
    files is reported by :func:`unassigned_legacy` and stays in the config.
    Because this runs on every load until the user saves settings, a model
    dropped into models/ before then is still picked up.
    """
    cfg["model_selection"] = normalise_selection(cfg.get("model_selection"))
    if isinstance(cfg.get("model_slots"), list):
        cfg["model_slots"] = normalise_slots(cfg["model_slots"])
        return cfg
    groups = {}

    def _add(role, raw):
        key = norm_key(raw, models_dir, project_root)
        g = groups.setdefault(key, {"path": raw, "roles": []})
        if role not in g["roles"]:
            g["roles"].append(role)

    legacy = None
    if models_dir is not None and project_root is not None:
        try:
            legacy = router.ModelRegistry({k: v for k, v in cfg.items() if k != "model_slots"},
                                          Path(models_dir), Path(project_root), discover=discover)
        except Exception:
            legacy = None
    if legacy is not None:
        for role in ROLE_ORDER:
            e = legacy.get(role)                 # existing files only
            if e is not None and "mmproj" not in e.path.name.lower():
                # keep the user's own spelling of a configured path where there is one
                raw = next((r for rr, r in legacy_refs(cfg) if rr == role), "") if e.source != "auto" else ""
                _add(role, raw if raw and _exists(raw, models_dir, project_root) else str(e.path))
    else:
        for role, raw in legacy_refs(cfg):
            if _exists(raw, models_dir, project_root):
                _add(role, raw)
    slots = []
    for g in list(groups.values())[:3]:
        slots.append({"name": "", "path": g["path"], "roles": g["roles"]})
    while len(slots) < 3:
        slots.append(empty_slot(len(slots)))
    cfg["model_slots"] = normalise_slots(slots)
    return cfg


def slots_active(cfg: dict) -> bool:
    return any(str(s.get("path") or "").strip() for s in normalise_slots(cfg.get("model_slots")))


def unassigned_legacy(cfg: dict, models_dir: Path | None = None, project_root: Path | None = None) -> list:
    """Existing legacy models (settings or models/<role>/ folders; global
    ~/Downloads discovery deliberately not included) that no slot points at -
    only meaningful in three-slot mode. Human-readable notes."""
    if not slots_active(cfg):
        return []
    keys = {norm_key(s["path"], models_dir, project_root)
            for s in normalise_slots(cfg.get("model_slots")) if s.get("path")}
    cands = [(role, raw) for role, raw in legacy_refs(cfg) if _exists(raw, models_dir, project_root)]
    if models_dir is not None and project_root is not None:
        try:
            legacy = router.ModelRegistry({k: v for k, v in cfg.items() if k != "model_slots"},
                                          Path(models_dir), Path(project_root), discover=None)
            cands += [(r, str(legacy.get(r).path)) for r in ROLE_ORDER
                      if legacy.get(r) is not None and "mmproj" not in legacy.get(r).path.name.lower()]
        except Exception:
            pass
    notes, seen = [], set()
    for role, raw in cands:
        k = norm_key(raw, models_dir, project_root)
        if k in keys or k in seen:
            continue
        seen.add(k)
        notes.append(f"Legacy '{role}' model {Path(raw).name} is not in any slot, so ISHA does not use it. "
                     f"Its setting/file is kept; put it in a slot to use it again.")
    return notes


# GGUF files that load fine but cannot hold a conversation on their own: a
# speculative-decoding draft head (EAGLE / EAGLE3 / Medusa / MTP / "draft")
# produces word salad when used as the main model.
_DRAFT_RE = re.compile(r"(?<![a-z])(eagle\d*|medusa|mtp|draft|speculative)(?![a-z])", re.I)


def not_chat_reason(path) -> str:
    """"" for a normal chat model, else why this file is not one."""
    name = Path(str(path)).name.lower()
    if "mmproj" in name or "projector" in name:
        return "a vision projector (mmproj), not a chat model"
    if _DRAFT_RE.search(name):
        return ("a speculative-decoding draft file (EAGLE/draft), not a chat model - it only works "
                "together with its full base model; on its own it outputs gibberish")
    return ""


def describe(cfg: dict, models_dir: Path, project_root: Path) -> list:
    """One row per slot for the settings UI / status text."""
    rows = []
    for i, s in enumerate(normalise_slots(cfg.get("model_slots"))):
        row = {"id": SLOT_IDS[i], "index": i, "name": s["name"], "label": display_name(s, i),
               "path": s["path"], "roles": list(s["roles"]), "resolved": "", "size_gb": 0.0}
        if not s["path"]:
            row["status"] = "empty"
        else:
            p = router._resolve(s["path"], Path(models_dir), Path(project_root))
            row["resolved"] = str(p) if p else ""
            if p is not None and p.is_file():
                row["status"] = "ok"
                try:
                    row["size_gb"] = round(p.stat().st_size / (1024 ** 3), 2)
                except Exception:
                    pass
                why = not_chat_reason(p)
                if why:
                    # projector / draft head: loads, but is not a chat model
                    row["status"] = "projector" if "projector" in why else "draft"
                    row["reason"] = why
            else:
                row["status"] = "missing"
        rows.append(row)
    return rows


def resolve_slot(cfg: dict, slot_id: str, models_dir: Path, project_root: Path):
    """(Path | None, row) for one slot. Path is only returned when the file exists."""
    i = slot_index(slot_id)
    if i < 0:
        return None, {"status": "invalid", "label": slot_id}
    row = describe(cfg, models_dir, project_root)[i]
    if row["status"] == "ok":
        return Path(row["resolved"]), row
    return None, row


def build_registry(cfg: dict, models_dir: Path, project_root: Path):
    """(ModelRegistry, notes). Roles map to the FIRST slot that lists them.
    General falls back to the first configured slot when no slot claims it;
    vision is only ever a slot explicitly marked Vision (a text model cannot see)."""
    rows = describe(cfg, models_dir, project_root)
    entries, notes = {}, []
    # A working slot always wins a role over a slot whose file is missing; a
    # missing one is only kept (so routing can report it) when nothing else has the role.
    ordered = [r for r in rows if r["status"] == "ok"] + [r for r in rows if r["status"] == "missing"]
    for role in ROLE_ORDER:
        for r in ordered:
            if role in r["roles"]:
                entries[role] = router.ModelEntry(role, Path(r["resolved"] or r["path"]), "slot",
                                                  slot=r["id"], label=r["label"])
                break
    if "general" not in entries or not entries["general"].exists:
        first_ok = next((r for r in rows if r["status"] == "ok"), None)
        if first_ok is not None:
            why = (f"the General slot's file is missing ({entries['general'].label})" if "general" in entries
                   else "no slot is marked General")
            entries["general"] = router.ModelEntry("general", Path(first_ok["resolved"]), "slot",
                                                   slot=first_ok["id"], label=first_ok["label"])
            notes.append(f"{why[0].upper() + why[1:]}; {first_ok['label']} handles general chat.")
    for r in rows:
        if r["status"] == "missing":
            notes.append(f"{r['label']}: file not found ({r['path']}).")
        elif r["status"] in ("projector", "draft"):
            notes.append(f"{r['label']}: {Path(r['path']).name} is {r.get('reason', 'not a chat model')}. "
                         "It is not used - pick a normal instruct GGUF for this slot.")
        elif r["status"] == "ok" and not r["roles"]:
            notes.append(f"{r['label']} has no roles - it is only used when picked manually.")
    return router.ModelRegistry.from_entries(entries, models_dir, project_root), notes


def slot_for_path(cfg: dict, path, models_dir: Path, project_root: Path) -> str:
    """Which slot (if any) points at this file - for honest status display."""
    if not path:
        return ""
    key = os.path.normcase(os.path.normpath(str(path)))
    for r in describe(cfg, models_dir, project_root):
        if r["resolved"] and os.path.normcase(os.path.normpath(r["resolved"])) == key:
            return r["id"]
    return ""


def pinned_path(cfg: dict, models_dir: Path, project_root: Path) -> str:
    """In three-slot mode, a path that is ALWAYS one of the slots: the first
    working slot, else the first configured (missing) one. Used wherever a cfg
    would otherwise fall through to directory auto-discovery."""
    rows = [r for r in describe(cfg, models_dir, project_root) if r["path"]]
    ok = [r for r in rows if r["status"] == "ok"]
    if ok:
        return ok[0]["resolved"]
    missing = [r for r in rows if r["status"] == "missing"]
    if missing:                      # the load error then names the file to fix
        return missing[0]["resolved"] or missing[0]["path"]
    # only draft/projector files configured: never load them as a chat model
    return str(Path(models_dir) / "NO-USABLE-CHAT-MODEL-IN-SLOTS (set one in Settings - Models).gguf")
