"""OS-aware discovery of the user's standard folders.

The old code hard-coded ``Path.home() / "Desktop"``. That is wrong on:
  * Windows with OneDrive folder redirection (Desktop lives under OneDrive),
  * Linux desktops with localized XDG dirs ("Schreibtisch", "डेस्कटॉप"),
  * any machine where the user moved Downloads to another drive.

``known_folder("desktop")`` asks the OS first and only falls back to the
home-relative guess when the OS has no answer.
"""
from __future__ import annotations

import os
import platform
import re
from functools import lru_cache
from pathlib import Path

_OS = platform.system()

# Windows KNOWNFOLDERID GUIDs
_WIN_GUIDS = {
    "desktop":   "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures":  "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music":     "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos":    "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
}

_XDG_KEYS = {
    "desktop": "XDG_DESKTOP_DIR", "documents": "XDG_DOCUMENTS_DIR",
    "downloads": "XDG_DOWNLOAD_DIR", "pictures": "XDG_PICTURES_DIR",
    "music": "XDG_MUSIC_DIR", "videos": "XDG_VIDEOS_DIR",
}

_DEFAULT_NAMES = {
    "desktop": "Desktop", "documents": "Documents", "downloads": "Downloads",
    "pictures": "Pictures", "music": "Music", "videos": "Videos",
}

# Hinglish / Hindi aliases people actually say
ALIASES = {
    "desktop": "desktop", "डेस्कटॉप": "desktop",
    "documents": "documents", "document": "documents", "docs": "documents",
    "downloads": "downloads", "download": "downloads", "डाउनलोड": "downloads",
    "pictures": "pictures", "picture": "pictures", "photos": "pictures",
    "music": "music", "gaane": "music", "songs": "music",
    "videos": "videos", "video": "videos",
    "home": "home", "~": "home",
}


def _win_known_folder(guid: str):
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

        g = GUID()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(g))
        out = ctypes.c_wchar_p()
        ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
        value = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
        return Path(value) if value else None
    except Exception:
        return None


def _xdg_user_dir(key: str, home: Path):
    cfg_home = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    f = cfg_home / "user-dirs.dirs"
    try:
        text = f.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.search(rf'^{key}="?([^"\n]+)"?', text, re.M)
    if not m:
        return None
    value = m.group(1).replace("$HOME", str(home))
    p = Path(os.path.expandvars(value))
    # xdg sets DESKTOP=$HOME when the folder was deliberately removed
    return p if p != home else None


@lru_cache(maxsize=32)
def _known_folder_cached(name: str, home_str: str) -> str:
    home = Path(home_str)
    if name == "home":
        return str(home)
    if _OS == "Windows" and name in _WIN_GUIDS:
        p = _win_known_folder(_WIN_GUIDS[name])
        if p:
            return str(p)
    if _OS == "Linux" and name in _XDG_KEYS:
        p = _xdg_user_dir(_XDG_KEYS[name], home)
        if p:
            return str(p)
    default = home / _DEFAULT_NAMES.get(name, name)
    if _OS == "Windows" and not default.exists():
        od = os.environ.get("OneDrive")
        if od and (Path(od) / _DEFAULT_NAMES.get(name, name)).exists():
            return str(Path(od) / _DEFAULT_NAMES.get(name, name))
    return str(default)


def known_folder(name: str) -> Path:
    """Return the OS path for a standard folder name (or alias)."""
    key = ALIASES.get((name or "").strip().lower(), (name or "").strip().lower())
    return Path(_known_folder_cached(key, str(Path.home())))


def all_known_folders() -> dict:
    return {k: known_folder(k) for k in ("desktop", "documents", "downloads",
                                          "pictures", "music", "videos", "home")}


def clear_cache():
    _known_folder_cached.cache_clear()


def resolve_user_path(raw: str) -> Path:
    """'desktop', 'downloads/reports', '~/x', '%USERPROFILE%\\y' or absolute."""
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        raise ValueError("Path is empty.")
    key = s.lower().rstrip("/\\")
    if key in ALIASES:
        return known_folder(key)
    parts = re.split(r"[\\/]+", s, maxsplit=1)
    if len(parts) == 2 and parts[0].lower() in ALIASES:
        return known_folder(parts[0].lower()) / parts[1]
    return Path(os.path.expandvars(os.path.expanduser(s)))
