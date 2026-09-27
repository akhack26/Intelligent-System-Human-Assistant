"""Uninstaller. Defaults protect user data: application + runtime + shortcuts
are removed; models, configuration, memory and cache are kept unless the
user explicitly ticks them.

Safety: only known ISHA names inside a folder that carries
installer_state.json are ever deleted; symlinks are removed as links, never
followed.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from installer import load_installer_config
from installer.config_manager import STATE_FILE
from installer import shortcut_manager

CATEGORIES = {
    "app": ["mani.py", "isha_core", "installer", "icon", "tests", "tools_dev", "packaging", "requirements.txt",
            "isha_config.example.json", "ISHA.cmd", "isha.sh", "ISHA_Setup.ps1", "ISHA_Setup.cmd", "install.sh",
            "LICENSE", "*.md"],
    "runtime": [".isha_runtime"],
    "models": ["models"],
    "cache": ["isha_file_index.db", "isha_file_index.db-journal", "isha_audit.jsonl", "isha_error.log",
              "isha_crash.log", ".downloads", ".diag", "backup", "__pycache__", ".isha_run.log"],
    "config": ["isha_config.json", "isha_config.json.bak-*", "whatsapp_contacts.json", STATE_FILE],
    "memory": ["isha_memory", "isha_long_memory.json", "isha_chat_history.json"],
}
DEFAULTS = {"app": True, "runtime": True, "models": False, "cache": False, "config": False, "memory": False}


@dataclass
class UninstallResult:
    removed: list
    kept: list
    errors: list
    deferred: list


def plan(install_dir: Path, choices: dict) -> dict:
    root = Path(install_dir)
    out = {"remove": [], "keep": []}
    for cat, patterns in CATEGORIES.items():
        for pat in patterns:
            for p in sorted(root.glob(pat)):
                (out["remove"] if choices.get(cat, DEFAULTS[cat]) else out["keep"]).append((cat, p))
    return out


def _inside(root: Path, p: Path) -> bool:
    try:
        return Path(os.path.abspath(p)).is_relative_to(Path(os.path.abspath(root)))
    except Exception:
        return False


def _running_from(runtime: Path) -> bool:
    try:
        return Path(sys.executable).resolve().is_relative_to(runtime.resolve())
    except Exception:
        return False


def uninstall(install_dir: Path, choices: dict | None = None, os_name: str = platform.system(),
              home: Path | None = None) -> UninstallResult:
    root = Path(install_dir)
    choices = {**DEFAULTS, **(choices or {})}
    if not (root / STATE_FILE).exists():
        raise RuntimeError(f"'{root}' does not look like an ISHA installation ({STATE_FILE} missing); "
                           f"nothing was deleted.")
    p = plan(root, choices)
    removed, errors, deferred = [], [], []
    removed += shortcut_manager.remove(root, os_name=os_name, home=home)
    for cat, path in p["remove"]:
        if not _inside(root, path):
            errors.append(f"refused (outside install dir): {path}")
            continue
        if cat == "runtime" and os_name == "Windows" and _running_from(path):
            deferred.append(str(path))
            continue
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            removed.append(str(path))
        except Exception as e:
            errors.append(f"{path}: {e}")
    kept = [str(x) for _, x in p["keep"]]
    if kept:
        (root / "ISHA_USER_DATA_KEPT.txt").write_text(
            "ISHA was uninstalled. These files were kept on purpose (models / settings / memory):\n"
            + "\n".join(kept) + "\nDelete this folder yourself if you do not need them.\n", encoding="utf-8")
    if deferred:
        # Windows cannot delete the interpreter it is running from; remove it after we exit.
        cmd = f'ping -n 3 127.0.0.1 >nul & rmdir /s /q "{deferred[0]}"'
        if not kept:
            cmd += f' & rmdir "{root}"'
        subprocess.Popen(["cmd", "/c", cmd], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "DETACHED_PROCESS", 0))
    elif not kept:
        try:
            root.rmdir()
        except OSError:
            pass
    return UninstallResult(removed, kept, errors, deferred)


def main_cli(install_dir: Path, args) -> int:
    choices = dict(DEFAULTS)
    for cat in ("models", "cache", "config", "memory"):
        if getattr(args, f"remove_{cat}", False) or getattr(args, "remove_all", False):
            choices[cat] = True
    p = plan(install_dir, choices)
    print(f"Uninstalling ISHA from {install_dir}")
    print("Will remove:", ", ".join(sorted({c for c, _ in p["remove"]})) or "nothing")
    print("Will keep:  ", ", ".join(sorted({c for c, _ in p["keep"]})) or "nothing")
    if not getattr(args, "yes", False):
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    res = uninstall(install_dir, choices)
    print(f"Removed {len(res.removed)} item(s); kept {len(res.kept)}; errors: {len(res.errors)}")
    for e in res.errors:
        print("  !", e)
    return 0 if not res.errors else 2
