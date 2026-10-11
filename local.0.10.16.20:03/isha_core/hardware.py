"""Hardware inspection used for model-loading decisions and diagnostics.

Everything here is best-effort and never raises: an unknown value is None,
never a guess. ``HardwareInfo.summary()`` is safe to show to the user.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    import psutil
    _PSUTIL = True
except Exception:  # pragma: no cover
    psutil = None
    _PSUTIL = False

GB = 1024 ** 3


@dataclass
class GPUInfo:
    name: str
    vram_total_gb: float | None = None
    vram_free_gb: float | None = None
    vendor: str = "unknown"


@dataclass
class HardwareInfo:
    os: str = ""
    os_release: str = ""
    arch: str = ""
    python: str = ""
    cpu_name: str = ""
    cpu_physical: int | None = None
    cpu_logical: int | None = None
    cpu_percent: float | None = None
    ram_total_gb: float | None = None
    ram_available_gb: float | None = None
    ram_percent: float | None = None
    disk_free_gb: float | None = None
    disk_total_gb: float | None = None
    gpus: list = field(default_factory=list)
    probed_at: float = 0.0

    @property
    def total_vram_gb(self) -> float:
        return sum((g.vram_total_gb or 0.0) for g in self.gpus)

    @property
    def free_vram_gb(self) -> float:
        return sum((g.vram_free_gb or 0.0) for g in self.gpus)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_vram_gb"] = self.total_vram_gb
        return d

    def summary(self) -> str:
        lines = [f"OS: {self.os} {self.os_release} ({self.arch})",
                 f"Python: {self.python}"]
        cpu = self.cpu_name or "CPU"
        cores = []
        if self.cpu_physical:
            cores.append(f"{self.cpu_physical} physical")
        if self.cpu_logical:
            cores.append(f"{self.cpu_logical} logical")
        lines.append(f"CPU: {cpu}" + (f" ({', '.join(cores)} cores)" if cores else "")
                     + (f", {self.cpu_percent:.0f}% busy" if self.cpu_percent is not None else ""))
        if self.ram_total_gb is not None:
            lines.append(f"RAM: {self.ram_available_gb:.1f} GB free of {self.ram_total_gb:.1f} GB"
                         f" ({self.ram_percent:.0f}% used)")
        else:
            lines.append("RAM: unknown (psutil missing)")
        if self.gpus:
            for g in self.gpus:
                v = (f", VRAM {g.vram_free_gb:.1f}/{g.vram_total_gb:.1f} GB free"
                     if g.vram_total_gb else "")
                lines.append(f"GPU: {g.name}{v}")
        else:
            lines.append("GPU: none detected (CPU inference)")
        if self.disk_total_gb:
            lines.append(f"Disk (ISHA drive): {self.disk_free_gb:.1f} GB free of {self.disk_total_gb:.1f} GB")
        return "\n".join(lines)


def _cpu_name() -> str:
    name = platform.processor() or ""
    if platform.system() == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
    return name


def _nvidia_gpus() -> list:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                gpus.append(GPUInfo(parts[0], float(parts[1]) / 1024, float(parts[2]) / 1024, "nvidia"))
            except ValueError:
                gpus.append(GPUInfo(parts[0], vendor="nvidia"))
    return gpus


def _windows_gpus() -> list:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '|' + $_.AdapterRAM }"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    gpus = []
    for line in out.strip().splitlines():
        name, _, ram = line.partition("|")
        if not name.strip():
            continue
        vram = None
        try:
            # AdapterRAM is a uint32 and caps at 4 GB; treat it as a lower bound
            vram = int(ram) / GB if ram.strip() else None
        except ValueError:
            pass
        vendor = ("nvidia" if "nvidia" in name.lower() else
                  "amd" if ("amd" in name.lower() or "radeon" in name.lower()) else
                  "intel" if "intel" in name.lower() else "unknown")
        gpus.append(GPUInfo(name.strip(), vram, None, vendor))
    return gpus


def _linux_gpus_lspci() -> list:
    exe = shutil.which("lspci")
    if not exe:
        return []
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    gpus = []
    for line in out.splitlines():
        low = line.lower()
        if "vga compatible" in low or "3d controller" in low:
            name = line.split(":", 2)[-1].strip()
            vendor = ("nvidia" if "nvidia" in low else "amd" if ("amd" in low or "ati" in low)
                      else "intel" if "intel" in low else "unknown")
            gpus.append(GPUInfo(name, vendor=vendor))
    return gpus


_CACHE = {"info": None, "gpu": None}


def probe(refresh_gpu: bool = False, disk_path: str | None = None) -> HardwareInfo:
    """Snapshot of the machine. GPU detection spawns processes, so it is cached."""
    info = HardwareInfo(
        os=platform.system(), os_release=platform.release(), arch=platform.machine(),
        python=platform.python_version(), cpu_name=_cpu_name(), probed_at=time.time())
    if _PSUTIL:
        try:
            info.cpu_physical = psutil.cpu_count(logical=False)
            info.cpu_logical = psutil.cpu_count(logical=True)
            info.cpu_percent = psutil.cpu_percent(interval=0.1)
            vm = psutil.virtual_memory()
            info.ram_total_gb = vm.total / GB
            info.ram_available_gb = vm.available / GB
            info.ram_percent = vm.percent
        except Exception:
            pass
    else:
        info.cpu_logical = os.cpu_count()
    try:
        du = shutil.disk_usage(disk_path or str(Path(__file__).resolve().parent))
        info.disk_free_gb, info.disk_total_gb = du.free / GB, du.total / GB
    except Exception:
        pass
    if _CACHE["gpu"] is None or refresh_gpu:
        gpus = _nvidia_gpus()
        if not gpus:
            if platform.system() == "Windows":
                gpus = _windows_gpus()
            elif platform.system() == "Linux":
                gpus = _linux_gpus_lspci()
        _CACHE["gpu"] = gpus
    info.gpus = list(_CACHE["gpu"])
    _CACHE["info"] = info
    return info


def model_fits(model_size_gb: float, info: HardwareInfo, headroom_gb: float = 1.5,
               already_loaded_gb: float = 0.0) -> bool:
    """Will a GGUF of this size fit in currently available memory?

    A GGUF needs roughly its file size plus KV-cache/context overhead.
    Unknown RAM (no psutil) -> assume it fits and let llama.cpp decide.
    """
    if info.ram_available_gb is None:
        return True
    budget = info.ram_available_gb + info.free_vram_gb + already_loaded_gb - headroom_gb
    return model_size_gb * 1.15 <= budget


def recommend_resident_models(sizes_gb: list, info: HardwareInfo, requested: int = 1,
                              headroom_gb: float = 2.0) -> int:
    """How many models may stay loaded at once, never more than requested."""
    requested = max(1, int(requested))
    if info.ram_total_gb is None or not sizes_gb:
        return requested
    budget = (info.ram_total_gb * 0.75) + info.total_vram_gb - headroom_gb
    total, count = 0.0, 0
    for s in sorted(sizes_gb, reverse=True):
        if total + s * 1.15 > budget:
            break
        total += s * 1.15
        count += 1
    return max(1, min(requested, count or 1))
