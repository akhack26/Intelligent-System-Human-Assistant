"""Platform-aware Python package installation into ISHA's runtime venv.

Package groups come from installer_config.json, which mirrors the lists in
mani.py (_CORE_PACKAGES, _OPTIONAL_PACKAGES, _WINDOWS_ONLY_PACKAGES) - a test
fails if they drift apart. Packages are installed one at a time so the UI can
show real progress and one broken optional package cannot block the rest.
An offline wheelhouse (offline/wheels) is used when present.
"""
from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from installer import load_installer_config

_FIND_SPEC = ("import importlib.util, json, sys\n"
              "names = json.loads(sys.argv[1])\n"
              "out = {}\n"
              "for pkg, mod in names.items():\n"
              "    try:\n        out[pkg] = importlib.util.find_spec(mod) is not None\n"
              "    except Exception:\n        out[pkg] = False\n"
              "print(json.dumps(out))")


@dataclass
class PackageResult:
    package: str
    group: str
    status: str          # installed | already | failed | skipped
    detail: str = ""


@dataclass
class DependencyReport:
    results: list = field(default_factory=list)

    @property
    def failed_core(self) -> list:
        return [r for r in self.results if r.status == "failed" and r.group == "core"]

    @property
    def failed_optional(self) -> list:
        return [r for r in self.results if r.status == "failed" and r.group != "core"]

    def summary(self) -> str:
        c = {s: sum(1 for r in self.results if r.status == s) for s in ("installed", "already", "failed")}
        return f"{c['installed']} installed, {c['already']} already present, {c['failed']} failed"


def select_groups(features: dict, os_name: str = platform.system(), cfg=None) -> list:
    cfg = cfg or load_installer_config()
    groups = ["core"]
    for fname, spec in cfg["features"].items():
        if features.get(fname, spec.get("default", False)):
            groups += spec["groups"]
    if os_name == "Windows":
        groups.append("windows")
    out = []
    for g in groups:
        spec = cfg["dependency_groups"][g]
        if spec.get("platforms") and os_name not in spec["platforms"]:
            continue
        if g not in out:
            out.append(g)
    return out


def packages_for(groups: list, cfg=None) -> list:
    cfg = cfg or load_installer_config()
    seen, out = set(), []
    for g in groups:
        for p in cfg["dependency_groups"][g]["packages"]:
            if p not in seen:
                seen.add(p)
                out.append((g, p))
    return out


def check_installed(python_exe: str, packages: list, cfg=None, runner=None) -> dict:
    cfg = cfg or load_installer_config()
    names = {p: cfg["import_names"].get(p, p.replace("-", "_")) for p in packages}
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=120,
                                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    try:
        r = run([python_exe, "-c", _FIND_SPEC, json.dumps(names)])
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {p: False for p in packages}


def pip_args(offline_dir: Path | None, internet: bool) -> list:
    args = ["--disable-pip-version-check", "--no-input", "--prefer-binary"]
    wheels = Path(offline_dir) / "wheels" if offline_dir else None
    if wheels and wheels.is_dir():
        args += ["--find-links", str(wheels)]
        if not internet:
            args.append("--no-index")
    return args


def install(python_exe: str, groups: list, progress=None, offline_dir=None, internet=True,
            runner=None, cancel=None, cfg=None) -> DependencyReport:
    """progress(index, total, package, status, detail)."""
    cfg = cfg or load_installer_config()
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=1800,
                                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    items = packages_for(groups, cfg)
    have = check_installed(python_exe, [p for _, p in items], cfg, runner=runner)
    rep = DependencyReport()
    say = progress or (lambda *a: None)
    base = [python_exe, "-m", "pip", "install"] + pip_args(offline_dir, internet)
    for i, (group, pkg) in enumerate(items, 1):
        if cancel is not None and cancel.is_set():
            rep.results.append(PackageResult(pkg, group, "skipped", "cancelled"))
            continue
        if have.get(pkg):
            rep.results.append(PackageResult(pkg, group, "already"))
            say(i, len(items), pkg, "already", "")
            continue
        say(i, len(items), pkg, "installing", "")
        r = run(base + [pkg])
        if getattr(r, "returncode", 1) == 0:
            rep.results.append(PackageResult(pkg, group, "installed"))
            say(i, len(items), pkg, "installed", "")
        else:
            out = ((getattr(r, "stdout", "") or "") + "\n" + (getattr(r, "stderr", "") or "")).strip()
            detail = _explain_pip_error(pkg, out, group, cfg)
            rep.results.append(PackageResult(pkg, group, "failed", detail))
            say(i, len(items), pkg, "failed", detail)
    if any(p in ("opencv-python-headless", "mediapipe") for _, p in items):
        _fix_opencv_qt_conflict(python_exe, run, base)
    return rep


def _explain_pip_error(pkg: str, out: str, group: str, cfg: dict) -> str:
    low = out.lower()
    if "no matching distribution" in low or "could not find a version" in low:
        why = "no compatible wheel for this Python version/platform"
    elif "portaudio" in low or ("pyaudio" in pkg.lower() and "error" in low):
        why = cfg["dependency_groups"].get("stt", {}).get("linux_note", "PortAudio library missing")
    elif "connection" in low or "timed out" in low or "network" in low:
        why = "network error while downloading"
    elif "permission" in low:
        why = "permission denied writing to the runtime folder"
    else:
        lines = [ln for ln in out.splitlines() if ln.strip()]
        why = lines[-1][:200] if lines else "unknown pip error"
    return why


def _fix_opencv_qt_conflict(python_exe, run, base):
    """Same fix mani.py applies: mediapipe can pull the non-headless OpenCV,
    whose bundled Qt breaks PyQt5's platform plugin. Force headless back."""
    run([python_exe, "-m", "pip", "uninstall", "-y", "-q", "opencv-python", "opencv-contrib-python"])
    run(base + ["--force-reinstall", "--no-deps", "opencv-python-headless"])
