"""GGUF model selection, recommendation, storage planning and installation.

The catalogue is data (models_manifest.json, optionally overridden by
models_manifest.local.json next to it) - maintainers update models without
touching code. For Hugging Face sources the exact size and SHA-256 are read
from the Hugging Face API at download time (the LFS object id *is* the
SHA-256) and the file is verified against them; a pinned "sha256" in the
manifest always wins.
"""
from __future__ import annotations

import json
import shutil
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from installer import PKG_DIR
from installer.download_manager import Downloader, DownloadError, free_space, finalize_file

GB = 1024 ** 3
TIERS = ("small", "balanced", "advanced")


@dataclass
class FileSpec:
    url: str
    filename: str
    size: int | None = None          # bytes (exact once resolved)
    sha256: str | None = None
    approx_gb: float = 0.0
    repo: str = ""
    hf_file: str = ""

    @property
    def size_gb(self) -> float:
        return (self.size / GB) if self.size else self.approx_gb


@dataclass
class ModelEntry:
    id: str
    name: str
    roles: list
    tier: str
    files: list                      # [FileSpec] - first is the model, rest e.g. mmproj
    ram_gb: float
    description: str = ""
    license: str = ""

    @property
    def size_gb(self) -> float:
        return sum(f.size_gb for f in self.files)


def _filespec(src: dict, approx: float, sha: str | None = None, size: int | None = None) -> FileSpec:
    if src.get("type") == "huggingface":
        repo, fname = src["repo"], src["file"]
        url = f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(fname)}"
        return FileSpec(url, Path(fname).name, size, sha, approx, repo, fname)
    url = src.get("url") or ""
    return FileSpec(url, src.get("filename") or Path(urllib.parse.urlparse(url).path).name, size, sha, approx)


def load_manifest(path: Path | None = None) -> dict:
    p = Path(path) if path else PKG_DIR / "models_manifest.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    local = p.with_name("models_manifest.local.json")
    if path is None and local.exists():
        extra = json.loads(local.read_text(encoding="utf-8"))
        by_id = {m["id"]: m for m in data["models"]}
        for m in extra.get("models", []):
            by_id[m["id"]] = m
        data["models"] = list(by_id.values())
        data.setdefault("presets", {}).update(extra.get("presets", {}))
    return data


def entries(manifest: dict) -> list:
    out = []
    for m in manifest["models"]:
        src = m.get("source") or {"url": m.get("url"), "filename": m.get("filename")}
        files = [_filespec(src, float(m.get("size_gb") or 0), m.get("sha256"), m.get("size"))]
        for x in m.get("extra_files", []):
            files.append(_filespec(x["source"], float(x.get("size_gb") or 0), x.get("sha256"), x.get("size")))
        out.append(ModelEntry(m["id"], m["name"], list(m["roles"]), m.get("tier", "balanced"), files,
                              float(m.get("ram_gb") or 0), m.get("description", ""), m.get("license", "")))
    return out


# ---------------------------------------------------------------- recommendation
def recommend_tier(ram_total_gb: float | None, vram_gb: float = 0.0) -> tuple:
    """Technical rule: a Q4_K_M model needs ~its file size in RAM plus context
    and OS headroom. 1.5B (~1.1 GB) fits 4-8 GB machines, 3B (~2 GB) wants
    8 GB+, 7B (~4.7 GB) wants 16 GB+ or a 6 GB+ GPU."""
    if ram_total_gb is None:
        return "balanced", "RAM unknown - choosing the balanced 3B model."
    if ram_total_gb >= 15.5 or (vram_gb >= 6 and ram_total_gb >= 8):
        return "advanced", f"{ram_total_gb:.0f} GB RAM{f' + {vram_gb:.0f} GB VRAM' if vram_gb else ''} fits a 7B model."
    if ram_total_gb >= 7.5:
        return "balanced", f"{ram_total_gb:.0f} GB RAM fits a 3B model comfortably (7B would be tight)."
    return "small", f"{ram_total_gb:.0f} GB RAM - a 1.5B model keeps ISHA responsive."


def _pick(all_entries, role, tier):
    order = {"small": ["small", "balanced", "advanced"], "balanced": ["balanced", "small", "advanced"],
             "advanced": ["advanced", "balanced", "small"]}[tier]
    cands = [e for e in all_entries if role in e.roles]
    for t in order:
        for e in cands:
            if e.tier == t:
                return e
    return None


def plan_preset(preset: str, tier: str, manifest: dict) -> dict:
    """-> {role: ModelEntry}. Roles without a suitable entry map to the general
    model file (no extra download), e.g. reasoning on small machines."""
    all_e = entries(manifest)
    roles = manifest["presets"][preset]["roles"]
    plan = {}
    general = _pick(all_e, "general", tier)
    for role in roles:
        if role == "reasoning" and tier != "advanced":
            plan[role] = general                  # same file, no second download
            continue
        e = _pick(all_e, role, "small" if role == "fast" else tier)
        if role == "coding" and tier == "balanced":
            e = _pick(all_e, "coding", "small")   # no 3B coder in the catalogue: 1.5B coder is the fit
        if e is not None:
            plan[role] = e
    return plan


def unique_entries(plan: dict) -> list:
    seen, out = set(), []
    for e in plan.values():
        if e and e.id not in seen:
            seen.add(e.id)
            out.append(e)
    return out


def model_path(models_dir: Path, entry: ModelEntry, f: FileSpec) -> Path:
    return Path(models_dir) / entry.roles[0] / f.filename


def storage_needed(plan: dict, models_dir: Path, runtime_installed: bool, runtime_gb: float = 1.6) -> dict:
    model_b, temp_b = 0, 0
    for e in unique_entries(plan):
        for f in e.files:
            p = model_path(models_dir, e, f)
            if p.exists() and (not f.size or p.stat().st_size == f.size):
                continue
            size = f.size or int(f.approx_gb * GB)
            part = p.with_name(p.name + ".part")
            have = part.stat().st_size if part.exists() else 0
            model_b += max(0, size - have)
    temp_b = int(model_b * 0.02) + 128 * 1024 * 1024
    runtime_b = 0 if runtime_installed else int(runtime_gb * GB)
    total = model_b + temp_b + runtime_b
    avail = free_space(models_dir)
    return {"models": model_b, "runtime": runtime_b, "temp": temp_b, "total": total,
            "available": avail, "enough": avail >= total}


# ---------------------------------------------------------------- HF metadata
def resolve_hf_metadata(fs: FileSpec, fetch=None, timeout: float = 20) -> FileSpec:
    """Fill exact size + sha256 from the Hugging Face API. Best effort."""
    if not fs.repo or (fs.size and fs.sha256):
        return fs
    fetch = fetch or _fetch_json
    try:
        tree = fetch(f"https://huggingface.co/api/models/{fs.repo}/tree/main", timeout)
        for item in tree:
            if item.get("path") == fs.hf_file:
                lfs = item.get("lfs") or {}
                fs.size = fs.size or int(lfs.get("size") or item.get("size") or 0) or None
                fs.sha256 = fs.sha256 or (lfs.get("oid") or lfs.get("sha256") or None)
                return fs
        raise DownloadError(f"'{fs.hf_file}' is not in {fs.repo} (manifest out of date?)", "http", retryable=False)
    except DownloadError:
        raise
    except Exception:
        return fs          # offline or API change: size/checksum from manifest (if any)


def _fetch_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "ISHA-Installer/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- install
@dataclass
class InstallResult:
    installed: dict = field(default_factory=dict)     # role -> path
    skipped: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def install_entry(entry: ModelEntry, models_dir: Path, offline_dir: Path | None = None,
                  progress=None, fetch=None, register=None, downloader_cls=Downloader) -> list:
    """Download/copy+verify all files of one entry. Returns final paths.
    register(downloader) lets the UI hold a handle for pause/resume/cancel."""
    paths = []
    for f in entry.files:
        resolve_hf_metadata(f, fetch=fetch)
        dest = model_path(models_dir, entry, f)
        src_off = (Path(offline_dir) / "models" / f.filename) if offline_dir else None
        if dest.exists() and (not f.size or dest.stat().st_size == f.size) and \
                (not f.sha256 or _sha(dest) == f.sha256.lower()):
            paths.append(dest)                      # already installed and verified
            continue
        if src_off is not None and src_off.exists():   # offline bundle: copy + same verification
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            shutil.copyfile(src_off, tmp)
            finalize_file(tmp, dest, f.size, f.sha256)
            paths.append(dest)
            continue
        d = downloader_cls(f.url, dest, expected_size=f.size, sha256=f.sha256,
                           progress=(lambda p, fn=f.filename: progress(fn, p)) if progress else None)
        if register:
            register(d)
        paths.append(d.run())
    return paths


def _sha(path: Path) -> str:
    from installer.download_manager import sha256_file
    return sha256_file(path)


def is_gguf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"GGUF"
    except Exception:
        return False


def add_custom(path: Path, role: str, models_dir: Path, copy: bool = False) -> Path:
    """Use an existing GGUF. Referenced in place by default (no duplicate GBs)."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"File not found: {p}")
    if not is_gguf(p):
        raise ValueError(f"'{p.name}' is not a GGUF model file (wrong header).")
    if copy:
        dest = Path(models_dir) / role / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if free_space(dest.parent) < p.stat().st_size + 64 * 1024 * 1024:
            raise ValueError("Not enough disk space to copy the model.")
        shutil.copy2(p, dest)
        return dest
    return p
