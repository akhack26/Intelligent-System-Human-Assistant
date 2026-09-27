"""Platform-specific system operations behind one interface.

    SystemController
        ├── WindowsSystemController
        ├── LinuxSystemController
        └── MacSystemController

mani.py already contains a lot of OS code (``WindowsController`` - which is
actually cross-platform - volume, brightness, power, windows). That code is
kept. This hierarchy holds the operations that needed *verification* or
did not exist: Recycle Bin / Trash status + empty-with-verify, verified
volume reads, and OS identification. New OS integrations go here.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path


def _res(success: bool, message: str, **data) -> dict:
    return {"success": success, "message": message, "error": None if success else message, **data}


class SystemController:
    name = "generic"

    # ---- Recycle Bin / Trash ----
    def trash_status(self) -> dict:
        return _res(False, "Trash status is not supported on this OS.")

    def empty_trash(self) -> dict:
        return _res(False, "Emptying the trash is not supported on this OS.")

    # ---- audio ----
    def get_volume(self):
        return None

    # ---- info ----
    def os_info(self) -> dict:
        return {"os": platform.system(), "release": platform.release(), "version": platform.version(),
                "arch": platform.machine(), "hostname": platform.node(), "python": platform.python_version()}


class LinuxSystemController(SystemController):
    name = "linux"

    def __init__(self, home: Path | None = None):
        self._home = home

    @property
    def trash_dirs(self) -> list:
        home = self._home or Path.home()
        data_home = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
        dirs = [data_home / "Trash"]
        # per-mount trash (.Trash-<uid>) on other drives
        try:
            uid = os.getuid()
            for mnt in (Path("/media"), Path("/mnt"), Path("/run/media")):
                if mnt.is_dir():
                    for sub in mnt.rglob(f".Trash-{uid}"):
                        dirs.append(sub)
                        if len(dirs) > 20:
                            break
        except Exception:
            pass
        return dirs

    def _count(self):
        items, size = 0, 0
        for t in self.trash_dirs:
            files = t / "files"
            if not files.is_dir():
                continue
            for entry in files.iterdir():
                items += 1
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        for root, _, fs in os.walk(entry):
                            for f in fs:
                                try:
                                    size += os.lstat(os.path.join(root, f)).st_size
                                except OSError:
                                    pass
                    else:
                        size += entry.lstat().st_size
                except OSError:
                    pass
        return items, size

    def trash_status(self) -> dict:
        items, size = self._count()
        return _res(True, f"Trash me {items} item(s) hain ({size / 1048576:.1f} MB).", items=items, bytes=size)

    def empty_trash(self) -> dict:
        before, size = self._count()
        if before == 0:
            return _res(True, "Trash pehle se khaali hai.", removed=0, remaining=0, verified=True)
        # prefer the desktop's own implementation when present
        if self._home is None and shutil.which("gio"):   # never touch the real trash in tests
            try:
                subprocess.run(["gio", "trash", "--empty"], capture_output=True, timeout=120)
            except Exception:
                pass
        failed = []
        for t in self.trash_dirs:
            for sub in ("files", "info", "expunged"):
                d = t / sub
                if not d.is_dir():
                    continue
                for item in d.iterdir():
                    try:
                        if item.is_dir() and not item.is_symlink():
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                    except Exception as e:
                        failed.append(f"{item.name}: {e}")
            try:
                (t / "directorysizes").unlink(missing_ok=True)
            except Exception:
                pass
        after, _ = self._count()
        ok = after == 0
        msg = (f"Trash khaali kar di - {before} item(s), {size / 1048576:.1f} MB free hua (verified)."
               if ok else f"Trash poori khaali nahi hui: {after} item(s) abhi bhi bache hain.")
        return _res(ok, msg, removed=before - after, remaining=after, verified=True, failed=failed[:5])

    def get_volume(self):
        if shutil.which("pactl"):
            try:
                out = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                                     capture_output=True, text=True, timeout=5).stdout
                m = re.search(r"(\d+)%", out)
                if m:
                    return int(m.group(1))
            except Exception:
                pass
        if shutil.which("wpctl"):
            try:
                out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                                     capture_output=True, text=True, timeout=5).stdout
                m = re.search(r"([\d.]+)", out)
                if m:
                    return int(round(float(m.group(1)) * 100))
            except Exception:
                pass
        if shutil.which("amixer"):
            try:
                out = subprocess.run(["amixer", "get", "Master"], capture_output=True, text=True, timeout=5).stdout
                m = re.search(r"\[(\d+)%\]", out)
                if m:
                    return int(m.group(1))
            except Exception:
                pass
        return None


class WindowsSystemController(SystemController):
    name = "windows"

    def _query(self):
        """SHQueryRecycleBinW -> (items, bytes) or (None, None)."""
        try:
            import ctypes
            from ctypes import wintypes

            class SHQUERYRBINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong),
                            ("i64NumItems", ctypes.c_longlong)]

            info = SHQUERYRBINFO()
            info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
            hr = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
            if hr != 0:
                return None, None
            return int(info.i64NumItems), int(info.i64Size)
        except Exception:
            return None, None

    def trash_status(self) -> dict:
        items, size = self._query()
        if items is None:
            return _res(False, "Recycle Bin query nahi ho paayi.")
        return _res(True, f"Recycle Bin me {items} item(s) hain ({size / 1048576:.1f} MB).", items=items, bytes=size)

    def empty_trash(self) -> dict:
        before, size = self._query()
        if before == 0:
            return _res(True, "Recycle Bin pehle se khaali hai.", removed=0, remaining=0, verified=True)
        try:
            import ctypes
            # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
            hr = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7)
        except Exception as e:
            return _res(False, f"Recycle Bin empty nahi hui: {e}")
        after, _ = self._query()
        if after is None:
            ok = hr == 0
            return _res(ok, "Recycle Bin empty command chala, par verify nahi ho paaya." if ok else
                        f"Recycle Bin empty nahi hui (code {hr}).", verified=False)
        ok = after == 0
        freed = (size or 0) / 1048576
        msg = (f"Recycle Bin khaali kar di - {before} item(s), {freed:.1f} MB free hua (verified)." if ok
               else f"Recycle Bin poori khaali nahi hui: {after} item(s) bache hain (code {hr}).")
        return _res(ok, msg, removed=(before or 0) - after, remaining=after, verified=True)


class MacSystemController(SystemController):
    name = "macos"

    def trash_status(self) -> dict:
        t = Path.home() / ".Trash"
        try:
            items = list(t.iterdir())
        except Exception as e:
            return _res(False, f"Trash read nahi hui: {e}")
        return _res(True, f"Trash me {len(items)} item(s) hain.", items=len(items))

    def empty_trash(self) -> dict:
        before = self.trash_status().get("items", 0)
        try:
            subprocess.run(["osascript", "-e", 'tell application "Finder" to empty trash'],
                           capture_output=True, timeout=120)
        except Exception as e:
            return _res(False, f"Trash empty nahi hui: {e}")
        after = self.trash_status().get("items", 0)
        return _res(after == 0, "Trash khaali kar di (verified)." if after == 0 else
                    f"{after} item(s) bache hain.", removed=before - after, remaining=after, verified=True)


def get_controller() -> SystemController:
    osn = platform.system()
    if osn == "Windows":
        return WindowsSystemController()
    if osn == "Linux":
        return LinuxSystemController()
    if osn == "Darwin":
        return MacSystemController()
    return SystemController()
