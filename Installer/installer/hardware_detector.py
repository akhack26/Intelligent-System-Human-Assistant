"""System compatibility checks and GPU-backend selection for the installer.

Every check returns READY / WARNING / MISSING with a human reason; nothing is
marked READY unless it was actually probed.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import ssl
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from isha_core import hardware as hw

READY, WARNING, MISSING = "READY", "WARNING", "MISSING"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fatal: bool = False          # MISSING + fatal blocks installation


@dataclass
class SystemReport:
    checks: list = field(default_factory=list)
    hardware: object = None
    gpu_backend: str = "cpu"
    gpu_reason: str = ""
    internet: bool = False

    @property
    def blocking(self) -> list:
        return [c for c in self.checks if c.status == MISSING and c.fatal]

    def get(self, name):
        return next((c for c in self.checks if c.name == name), None)


def check_internet(urls=("https://pypi.org/simple/pip/", "https://github.com"), timeout=6) -> tuple:
    ctx = ssl.create_default_context()
    for u in urls:
        try:
            req = urllib.request.Request(u, method="HEAD", headers={"User-Agent": "ISHA-Installer"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                if r.status < 500:
                    return True, u
        except Exception:
            continue
    return False, ""


def writable(path: Path) -> tuple:
    p = Path(path)
    probe_dir = p
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    try:
        with tempfile.NamedTemporaryFile(dir=probe_dir, prefix=".isha_w", delete=True):
            pass
        return True, str(probe_dir)
    except Exception as e:
        return False, f"{probe_dir}: {e}"


def vulkan_available(os_name: str = platform.system()) -> bool:
    if os_name == "Windows":
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        return Path(sysroot, "System32", "vulkan-1.dll").exists()
    if os_name == "Linux":
        for d in ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
                  "/usr/local/lib"):
            try:
                if any(Path(d).glob("libvulkan.so.1*")):
                    return True
            except Exception:
                pass
        if shutil.which("ldconfig"):
            try:
                out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=5).stdout
                return "libvulkan.so.1" in out
            except Exception:
                return False
    return False


def choose_gpu_backend(info, vulkan: bool, os_name: str = platform.system()) -> tuple:
    """-> (variant, reason). Only backends that exist as self-contained prebuilt
    llama.cpp releases are auto-selected: Vulkan works on NVIDIA and AMD with
    their normal drivers on Windows and Linux. CUDA/HIP builds need separately
    installed runtimes, so they are offered only as a manual choice."""
    gpus = list(getattr(info, "gpus", []) or [])
    if os_name == "Darwin":
        return "cpu", "macOS builds use Metal automatically."
    strong = [g for g in gpus if g.vendor in ("nvidia", "amd")]
    if not gpus:
        return "cpu", "No GPU detected. ISHA will use CPU mode."
    if not strong:
        return "cpu", (f"GPU '{gpus[0].name}' is an integrated/unknown GPU; CPU mode is usually as fast. "
                       "You can still choose Vulkan manually.")
    if not vulkan:
        return "cpu", (f"GPU '{strong[0].name}' found but the Vulkan runtime is missing "
                       "(update the graphics driver). ISHA will use CPU mode.")
    return "vulkan", f"GPU acceleration via Vulkan on '{strong[0].name}'."


def run_system_check(install_dir: Path, python_info=None, need_gb: float = 4.0,
                     probe_internet: bool = True) -> SystemReport:
    info = hw.probe(refresh_gpu=True, disk_path=str(_existing_parent(install_dir)))
    rep = SystemReport(hardware=info)
    osn = platform.system()
    supported = osn in ("Windows", "Linux")
    rel = platform.release()
    rep.checks.append(Check("Operating System", READY if supported else WARNING,
                            f"{osn} {rel} ({platform.machine()})" +
                            ("" if supported else " - not officially supported; installer may still work")))
    cores = info.cpu_logical or 0
    rep.checks.append(Check("CPU", READY if cores >= 4 else WARNING,
                            f"{info.cpu_name or 'CPU'}, {cores} threads" +
                            ("" if cores >= 4 else " - models will be slow on fewer than 4 threads")))
    if info.ram_total_gb is None:
        rep.checks.append(Check("RAM", WARNING, "could not be measured"))
    else:
        # "4 GB" machines report ~3.7-3.9 GB; a 1.5B Q4 model needs ~2 GB. Below 2.5 GB nothing useful fits.
        st = READY if info.ram_total_gb >= 7.5 else WARNING if info.ram_total_gb >= 2.5 else MISSING
        rep.checks.append(Check("RAM", st, f"{info.ram_total_gb:.1f} GB total, {info.ram_available_gb:.1f} GB free"
                                + (" - only the smaller models will fit" if st == WARNING else
                                   " - at least 3 GB is needed" if st == MISSING else ""), fatal=(st == MISSING)))
    free = info.disk_free_gb or 0.0
    st = READY if free >= need_gb else MISSING
    rep.checks.append(Check("Storage", st, f"{free:.1f} GB free at {_existing_parent(install_dir)} "
                            f"(needs about {need_gb:.1f} GB)", fatal=(st == MISSING)))
    if python_info is None:
        rep.checks.append(Check("Python", WARNING, "not checked"))
    elif python_info.compatible:
        rep.checks.append(Check("Python", READY if not python_info.warning else WARNING,
                                f"Python {python_info.version_str} ({python_info.path})"
                                + (f" - {python_info.warning}" if python_info.warning else "")))
    else:
        rep.checks.append(Check("Python", MISSING, python_info.reason or "no compatible Python found",
                                fatal=False))           # installer offers to install it
    vk = vulkan_available(osn)
    rep.gpu_backend, rep.gpu_reason = choose_gpu_backend(info, vk, osn)
    if info.gpus:
        vram = f", {info.total_vram_gb:.1f} GB VRAM" if info.total_vram_gb else ""
        rep.checks.append(Check("GPU", READY if rep.gpu_backend != "cpu" else WARNING,
                                f"{', '.join(g.name for g in info.gpus)}{vram}. {rep.gpu_reason}"))
    else:
        rep.checks.append(Check("GPU", WARNING, rep.gpu_reason))
    if probe_internet:
        rep.internet, via = check_internet()
        rep.checks.append(Check("Internet", READY if rep.internet else WARNING,
                                f"reachable ({via})" if rep.internet else
                                "offline - only an offline bundle / already-downloaded files can be used"))
    ok, where = writable(install_dir)
    rep.checks.append(Check("Permissions", READY if ok else MISSING,
                            f"can write to {where}" if ok else f"cannot write: {where}", fatal=not ok))
    return rep


def _existing_parent(p: Path) -> Path:
    p = Path(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p
