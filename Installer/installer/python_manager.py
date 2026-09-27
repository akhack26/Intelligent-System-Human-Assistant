"""Find a compatible Python, or install one through an official channel.

Windows: winget (Python.Python.3.12, per-user) or the official python.org
installer, whose Authenticode signature must be valid and issued to the
Python Software Foundation before it is run. Linux: the distribution's
package manager via pkexec (graphical password prompt owned by the OS).
The installer itself never needs `pip install ...` typed by the user.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from installer import load_installer_config

_PROBE = ("import sys, json\n"
          "ok_venv = True\n"
          "try:\n    import venv, ensurepip\nexcept Exception:\n    ok_venv = False\n"
          "print(json.dumps({'v': list(sys.version_info[:3]), 'exe': sys.executable, 'venv': ok_venv,"
          " 'bits': 64 if sys.maxsize > 2**32 else 32}))")


@dataclass
class PythonInfo:
    path: str = ""
    version: tuple = ()
    has_venv: bool = False
    bits: int = 64
    compatible: bool = False
    warning: str = ""
    reason: str = ""

    @property
    def version_str(self) -> str:
        return ".".join(map(str, self.version)) if self.version else "?"


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in s.split("."))


def evaluate(info: PythonInfo, cfg: dict | None = None) -> PythonInfo:
    cfg = (cfg or load_installer_config())["python"]
    lo, hi = _ver(cfg["min"]), _ver(cfg["max_tested"])
    v = tuple(info.version[:2])
    if not info.version:
        info.compatible, info.reason = False, "Python not found"
    elif v < lo:
        info.compatible, info.reason = False, f"Python {info.version_str} is too old (ISHA needs {cfg['min']}+)"
    elif info.bits != 64:
        info.compatible, info.reason = False, "32-bit Python cannot run the AI runtime; 64-bit is required"
    elif not info.has_venv:
        info.compatible = False
        info.reason = (f"Python {info.version_str} has no 'venv'/'ensurepip' module"
                       + (" (install python3-venv)" if platform.system() == "Linux" else ""))
    else:
        info.compatible = True
        if v > hi:
            info.warning = (f"Python {info.version_str} is newer than tested ({cfg['max_tested']}); some "
                            f"optional packages (e.g. mediapipe) may have no wheels yet")
    return info


def probe(path: str, runner=None) -> PythonInfo | None:
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=30,
                                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    try:
        r = run([path, "-c", _PROBE] if not isinstance(path, list) else path + ["-c", _PROBE])
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout.strip().splitlines()[-1])
        return PythonInfo(path=d["exe"], version=tuple(d["v"]), has_venv=bool(d["venv"]), bits=d.get("bits", 64))
    except Exception:
        return None


def candidates(os_name: str = platform.system(), which=shutil.which) -> list:
    out = [[sys.executable]]
    if os_name == "Windows":
        if which("py"):
            for v in ("3.12", "3.11", "3.13", "3.10"):
                out.append(["py", f"-{v}"])
        for n in ("python", "python3"):
            p = which(n)
            # the Microsoft Store alias opens the Store instead of running Python
            if p and "WindowsApps" not in p:
                out.append([p])
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
        if local.is_dir():
            for d in sorted(local.glob("Python3*"), reverse=True):
                exe = d / "python.exe"
                if exe.exists():
                    out.append([str(exe)])
    else:
        for n in ("python3.12", "python3.11", "python3.13", "python3.10", "python3"):
            p = which(n)
            if p:
                out.append([p])
    seen, uniq = set(), []
    for c in out:
        k = " ".join(c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def find_best(runner=None, os_name: str = platform.system(), which=shutil.which, cfg=None) -> PythonInfo:
    cfg = cfg or load_installer_config()
    rec = _ver(cfg["python"]["recommended"])
    found = []
    for c in candidates(os_name, which):
        info = probe(c if len(c) > 1 else c[0], runner=runner)
        if info:
            found.append(evaluate(info, cfg))
    good = [f for f in found if f.compatible]
    if good:
        # prefer the recommended minor version, then no warning, then newest
        good.sort(key=lambda f: (tuple(f.version[:2]) != rec, bool(f.warning), [-x for x in f.version]))
        return good[0]
    if found:
        best = max(found, key=lambda f: f.version)
        return best
    return evaluate(PythonInfo(), cfg)


# ---------------------------------------------------------------- installing
def install_plan(os_name: str = platform.system(), which=shutil.which, cfg=None) -> dict:
    cfg = (cfg or load_installer_config())["python"]
    if os_name == "Windows":
        if which("winget"):
            return {"method": "winget",
                    "argv": ["winget", "install", "-e", "--id", cfg["windows_winget_id"], "--scope", "user",
                             "--silent", "--accept-package-agreements", "--accept-source-agreements"],
                    "describe": f"Install Python {cfg['recommended']} for this user with winget (official)."}
        return {"method": "python.org", "url": cfg["windows_installer_url"],
                "describe": f"Download the official Python {cfg['windows_installer_version']} installer from "
                            f"python.org (signature verified) and install it for this user only."}
    if os_name == "Linux":
        for mgr, argv in (("apt-get", ["apt-get", "install", "-y", "python3", "python3-venv", "python3-pip"]),
                          ("dnf", ["dnf", "install", "-y", "python3", "python3-pip"]),
                          ("pacman", ["pacman", "-S", "--needed", "--noconfirm", "python", "python-pip"]),
                          ("zypper", ["zypper", "--non-interactive", "install", "python3", "python3-pip"])):
            if which(mgr):
                if which("pkexec"):
                    return {"method": mgr, "argv": ["pkexec"] + argv,
                            "describe": f"Install Python with {mgr} (your system will ask for your password)."}
                return {"method": "manual", "command": "sudo " + " ".join(argv),
                        "describe": "Run this in a terminal, then restart the installer."}
    return {"method": "manual", "command": "", "describe": "Install Python 3.10-3.13 from python.org."}


def verify_authenticode(path: Path, signer: str) -> tuple:
    """Windows only. (ok, detail). Uses PowerShell's Get-AuthenticodeSignature."""
    ps = ("$s = Get-AuthenticodeSignature -LiteralPath $env:ISHA_SIG_FILE; "
          "Write-Output ($s.Status.ToString() + '|' + $s.SignerCertificate.Subject)")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "ISHA_SIG_FILE": str(path)},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        status, _, subject = (r.stdout or "").strip().partition("|")
    except Exception as e:
        return False, f"signature check failed: {e}"
    ok = status == "Valid" and signer.lower() in subject.lower()
    return ok, f"{status}: {subject}"


def install_python(plan: dict, progress=None, runner=None) -> dict:
    say = progress or (lambda m: None)
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=1800))
    if plan["method"] == "manual":
        return {"success": False, "manual": True, "message": plan["describe"] + " " + plan.get("command", "")}
    if plan["method"] == "python.org":
        cfg = load_installer_config()["python"]
        tmp = Path(tempfile.mkdtemp(prefix="isha_py_")) / Path(plan["url"]).name
        if not plan["url"].startswith("https://www.python.org/"):
            return {"success": False, "message": "Refusing a non-python.org Python download URL."}
        say(f"Downloading {plan['url']}")
        urllib.request.urlretrieve(plan["url"], tmp)
        ok, detail = verify_authenticode(tmp, cfg["authenticode_signer"])
        if not ok:
            tmp.unlink(missing_ok=True)
            return {"success": False, "message": f"Python installer signature not trusted ({detail}); not run."}
        say("Running the official Python installer (per-user, quiet)")
        r = run([str(tmp), "/quiet", "InstallAllUsers=0", "Include_launcher=1", "Include_pip=1",
                 "PrependPath=0", "Include_test=0"])
    else:
        say(plan["describe"])
        r = run(plan["argv"])
    after = find_best()
    if after.compatible:
        return {"success": True, "message": f"Python {after.version_str} installed at {after.path}", "python": after}
    tail = ((getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or ""))[-600:]
    return {"success": False, "message": f"Python install did not produce a compatible Python. {tail}"}
