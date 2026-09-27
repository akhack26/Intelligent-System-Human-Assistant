"""OS launchers so ISHA starts without a terminal.

Windows: Start Menu folder (ISHA + Uninstall ISHA), optional Desktop shortcut,
and a per-user "Apps & features" entry (HKCU - no admin needed).
Linux: freedesktop .desktop entries in ~/.local/share/applications, optional
Desktop launcher, and a small launcher script in the install folder.
"""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from installer import load_installer_config
from installer.runtime_manager import venv_python

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ISHA"


def _icon(install_dir: Path, ext: str):
    for name in (f"isha{ext}", f"ISHA{ext}", f"logo{ext}"):
        p = Path(install_dir) / "icon" / name
        if p.exists():
            return p
    return None


def launch_command(install_dir: Path, os_name: str = platform.system()) -> list:
    return [str(venv_python(install_dir, gui=True, os_name=os_name)), str(Path(install_dir) / "mani.py")]


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _windows_lnk(path: Path, target: str, args: str, workdir: str, icon: str | None, desc: str, runner):
    path.parent.mkdir(parents=True, exist_ok=True)
    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(path)});"
              f"$s.TargetPath={_ps_quote(target)};$s.Arguments={_ps_quote(args)};"
              f"$s.WorkingDirectory={_ps_quote(workdir)};$s.Description={_ps_quote(desc)};"
              + (f"$s.IconLocation={_ps_quote(icon)};" if icon else "") + "$s.Save()")
    r = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    return getattr(r, "returncode", 1) == 0 and path.exists()


def create(install_dir: Path, desktop: bool = True, os_name: str = platform.system(), runner=None,
           home: Path | None = None) -> dict:
    install_dir = Path(install_dir)
    base_run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=60,
                                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))

    def run(argv):
        """Helper tools (gio, update-desktop-database, powershell) are optional."""
        try:
            return base_run(argv)
        except (OSError, subprocess.SubprocessError) as e:
            return subprocess.CompletedProcess(argv, 127, "", str(e))
    made = []
    if os_name == "Windows":
        appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
        menu = appdata / "Microsoft/Windows/Start Menu/Programs/ISHA"
        gui_py = str(venv_python(install_dir, gui=True, os_name=os_name))
        icon = _icon(install_dir, ".ico")
        if _windows_lnk(menu / "ISHA.lnk", gui_py, f'"{install_dir / "mani.py"}"', str(install_dir),
                        str(icon) if icon else None, "ISHA local AI assistant", run):
            made.append(str(menu / "ISHA.lnk"))
        if _windows_lnk(menu / "Uninstall ISHA.lnk", gui_py, "-m installer --uninstall", str(install_dir),
                        None, "Uninstall ISHA", run):
            made.append(str(menu / "Uninstall ISHA.lnk"))
        if desktop:
            from isha_core.paths import known_folder
            lnk = known_folder("desktop") / "ISHA.lnk"
            if _windows_lnk(lnk, gui_py, f'"{install_dir / "mani.py"}"', str(install_dir),
                            str(icon) if icon else None, "ISHA local AI assistant", run):
                made.append(str(lnk))
        (install_dir / "ISHA.cmd").write_text(
            f'@echo off\r\ncd /d "%~dp0"\r\nstart "" "{gui_py}" "%~dp0mani.py"\r\n', encoding="utf-8")
        made.append(str(install_dir / "ISHA.cmd"))
        return {"success": bool(made), "created": made}

    # Linux / other freedesktop systems
    home = Path(home or Path.home())
    apps = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    py = venv_python(install_dir, os_name=os_name)
    launcher = install_dir / "isha.sh"
    launcher.write_text(f'#!/bin/sh\ncd "{install_dir}"\nexec "{py}" "{install_dir / "mani.py"}" "$@"\n',
                        encoding="utf-8")
    launcher.chmod(0o755)
    icon = _icon(install_dir, ".png")
    ver = load_installer_config()["app_version"]

    def entry(name, exec_, comment, extra=""):
        return (f"[Desktop Entry]\nType=Application\nVersion=1.0\nName={name}\nComment={comment}\n"
                f'Exec="{exec_}"{extra}\nPath={install_dir}\nTerminal=false\n'
                f"Categories=Utility;\n" + (f"Icon={icon}\n" if icon else "") + f"X-ISHA-Version={ver}\n")
    main = apps / "isha.desktop"
    main.write_text(entry("ISHA", launcher, "Local AI assistant"), encoding="utf-8")
    uninst = apps / "isha-uninstall.desktop"
    uninst.write_text(entry("Uninstall ISHA", py, "Remove ISHA", " -m installer --uninstall")
                      .replace(f'Exec="{py}" -m', f'Exec=env PYTHONPATH="{install_dir}" "{py}" -m'),
                      encoding="utf-8")
    made += [str(launcher), str(main), str(uninst)]
    if desktop:
        try:
            from isha_core.paths import known_folder
            d = known_folder("desktop")
            if d.is_dir():
                dst = d / "isha.desktop"
                dst.write_text(main.read_text(encoding="utf-8"), encoding="utf-8")
                dst.chmod(0o755)
                run(["gio", "set", str(dst), "metadata::trusted", "true"])
                made.append(str(dst))
        except Exception:
            pass
    run(["update-desktop-database", str(apps)])
    return {"success": True, "created": made}


def register_uninstaller(install_dir: Path, version: str, size_kb: int = 0) -> bool:
    """Windows 'Apps & features' entry for the current user."""
    if platform.system() != "Windows":
        return False
    import winreg
    gui_py = venv_python(install_dir, gui=True)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        vals = {"DisplayName": "ISHA", "DisplayVersion": version, "Publisher": "ISHA Project",
                "InstallLocation": str(install_dir),
                "UninstallString": f'"{gui_py}" -m installer --uninstall --install-dir "{install_dir}"',
                "DisplayIcon": str(_icon(install_dir, ".ico") or gui_py)}
        for name, v in vals.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, v)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        if size_kb:
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, int(size_kb))
    return True


def remove(install_dir: Path, os_name: str = platform.system(), home: Path | None = None) -> list:
    removed = []
    cands = []
    if os_name == "Windows":
        appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
        cands.append(appdata / "Microsoft/Windows/Start Menu/Programs/ISHA")
        try:
            from isha_core.paths import known_folder
            cands.append(known_folder("desktop") / "ISHA.lnk")
        except Exception:
            pass
        try:
            import winreg
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
            removed.append("HKCU\\" + UNINSTALL_KEY)
        except Exception:
            pass
    else:
        home = Path(home or Path.home())
        apps = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / "applications"
        cands += [apps / "isha.desktop", apps / "isha-uninstall.desktop"]
        try:
            from isha_core.paths import known_folder
            cands.append(known_folder("desktop") / "isha.desktop")
        except Exception:
            pass
    import shutil
    for c in cands:
        try:
            if c.is_dir():
                shutil.rmtree(c)
                removed.append(str(c))
            elif c.exists():
                c.unlink()
                removed.append(str(c))
        except Exception:
            pass
    return removed
