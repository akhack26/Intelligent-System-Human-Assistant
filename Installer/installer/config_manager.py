"""Installation state and ISHA configuration written by the installer.

* isha_config.json   - ISHA's own config. Merged, never blindly replaced; a
                       timestamped backup is written before any change.
* installer_state.json - version, install time, terms version + acceptance
                       timestamp (nothing else about the user), chosen
                       features, installed models, llama.cpp variant, and
                       completed steps (so an interrupted install can resume).
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from installer import load_installer_config

STATE_FILE = "installer_state.json"
CONFIG_FILE = "isha_config.json"
AUTONOMY = {"ask": "low", "balanced": "medium", "advanced": "high"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def write_json_atomic(path: Path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class State:
    def __init__(self, install_dir: Path):
        self.path = Path(install_dir) / STATE_FILE
        self.data = read_json(self.path)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.path, self.data)

    def record_terms(self, version: str):
        self.data["terms"] = {"version": version, "accepted_at": now()}

    def terms_ok(self, version: str) -> bool:
        return (self.data.get("terms") or {}).get("version") == version

    def step_done(self, step: str):
        self.data.setdefault("completed_steps", [])
        if step not in self.data["completed_steps"]:
            self.data["completed_steps"].append(step)
        self.save()

    def is_done(self, step: str) -> bool:
        return step in self.data.get("completed_steps", [])


def update_config(install_dir: Path, changes: dict, backup: bool = True) -> Path:
    """Apply explicit wizard choices on top of the existing config."""
    path = Path(install_dir) / CONFIG_FILE
    cfg = read_json(path)
    if path.exists() and backup:
        shutil.copy2(path, path.with_name(f"{CONFIG_FILE}.bak-{datetime.now():%Y%m%d-%H%M%S}"))
    for k, v in changes.items():
        if k == "models" and isinstance(v, dict):
            cfg.setdefault("models", {})
            cfg["models"].update({r: p for r, p in v.items() if p})
        else:
            cfg[k] = v
    write_json_atomic(path, cfg)
    return path


def feature_config(features: dict, autonomy: str = "high", index_roots=None, index_excludes=None,
                   existing_blocked=None, cfg=None) -> dict:
    """Wizard choices -> ISHA config keys. A disabled feature is really off:
    its packages are not installed, its config switch is false and its tools
    are put in blocked_tools, so ISHA reports them unavailable instead of
    pretending."""
    cfg = cfg or load_installer_config()
    blocked = set(existing_blocked or [])
    all_feature_tools = {t for ts in cfg["feature_tools"].values() for t in ts}
    blocked -= all_feature_tools                       # recompute from this choice
    for feat, tools in cfg["feature_tools"].items():
        if not features.get(feat, True):
            blocked |= set(tools)
    out = {
        "tts_enabled": bool(features.get("voice", True)),
        "stt_enabled": bool(features.get("stt", True)),
        "vision_enabled": bool(features.get("vision", False)),
        "file_index_enabled": bool(features.get("file_index", True)),
        "autonomy_level": AUTONOMY.get(autonomy, autonomy if autonomy in ("low", "medium", "high") else "high"),
        "blocked_tools": sorted(blocked),
        "features": {k: bool(v) for k, v in features.items()},
    }
    if index_roots is not None:
        out["file_index_roots"] = [str(r) for r in index_roots]
    if index_excludes is not None:
        out["file_index_excludes"] = list(index_excludes)
    return out
