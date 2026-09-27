"""ISHA's isolated runtime: the .isha_runtime virtual environment (the same
directory mani.py already uses and re-execs into) and the llama.cpp runtime,
installed by running isha_core.llama_runtime *inside that venv* so the
native-wheel layer lands in the interpreter ISHA actually runs with."""
from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

RUNTIME_DIRNAME = ".isha_runtime"


def venv_python(install_dir: Path, gui: bool = False, os_name: str = platform.system()) -> Path:
    base = Path(install_dir) / RUNTIME_DIRNAME
    if os_name == "Windows":
        return base / "Scripts" / ("pythonw.exe" if gui else "python.exe")
    return base / "bin" / "python3"


def create_venv(install_dir: Path, base_python: str, progress=None, runner=None) -> dict:
    say = progress or (lambda m: None)
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=900,
                                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    py = venv_python(install_dir)
    if py.exists():
        r = run([str(py), "-c", "import sys; print(sys.version)"])
        if getattr(r, "returncode", 1) == 0:
            say("Existing ISHA runtime reused.")
            return {"success": True, "python": str(py), "reused": True}
        say("Existing runtime is broken - recreating it.")
    say("Creating isolated Python environment...")
    r = run([base_python, "-m", "venv", "--clear", str(Path(install_dir) / RUNTIME_DIRNAME)])
    if getattr(r, "returncode", 1) != 0 or not py.exists():
        hint = " On Debian/Ubuntu: sudo apt install python3-venv" if platform.system() == "Linux" else ""
        return {"success": False, "message": f"Could not create the virtual environment: "
                f"{(getattr(r, 'stderr', '') or '')[-400:]}{hint}"}
    say("Upgrading pip...")
    run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip", "wheel"])
    return {"success": True, "python": str(py), "reused": False}


def find_offline_llama_archive(offline_dir: Path | None, variant: str, os_name: str = platform.system()):
    if not offline_dir:
        return None
    d = Path(offline_dir) / "llamacpp"
    if not d.is_dir():
        return None
    plat = {"Windows": "win", "Linux": "ubuntu", "Darwin": "macos"}.get(os_name, "")
    for a in sorted(d.glob("llama-*")):
        n = a.name.lower()
        if plat in n and (variant in n if variant != "cpu" else ("cpu" in n or ("vulkan" not in n and "cuda" not in n))):
            return a
    return None


def setup_llama(install_dir: Path, variant: str, offline_dir=None, progress=None, popen=None) -> dict:
    """Run `python -m isha_core.llama_runtime --setup --json` in the ISHA venv
    and stream its JSON progress lines."""
    py = venv_python(install_dir)
    argv = [str(py), "-m", "isha_core.llama_runtime", "--setup", "--variant", variant, "--json"]
    arch = find_offline_llama_archive(offline_dir, variant)
    if arch:
        argv += ["--archive", str(arch)]
    say = progress or (lambda d: None)
    env = {**os.environ, "PYTHONPATH": str(install_dir), "PYTHONIOENCODING": "utf-8"}
    env.pop("ISHA_LLAMA_VARIANT", None)
    p = (popen or subprocess.Popen)(argv, cwd=str(install_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    final = None
    for line in p.stdout:
        line = line.strip()
        if not line.startswith("{"):
            say({"stage": "llama", "message": line[:200]})
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("final"):
            final = d
        else:
            say(d)
    p.wait()
    return final or {"success": False, "tried": [f"runtime setup exited with code {p.returncode}"]}
