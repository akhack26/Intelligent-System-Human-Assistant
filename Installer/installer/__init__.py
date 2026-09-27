"""ISHA installer: bootstrap-independent setup, first-run wizard and uninstaller.

Runs from the downloaded package folder (or from an installed ISHA folder for
first-run / repair / uninstall). Uses isha_core for shared logic (hardware,
paths, llama.cpp runtime) and never imports mani.py in-process.
"""
import json
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PKG_DIR.parent


def load_installer_config() -> dict:
    return json.loads((PKG_DIR / "installer_config.json").read_text(encoding="utf-8"))
