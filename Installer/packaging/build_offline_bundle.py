"""Build an offline bundle (offline/) so ISHA can be installed without internet.

    python packaging/build_offline_bundle.py --platform win_amd64 --python 3.12 --llama cpu --models general-small

Creates:
    offline/wheels/     pip wheels for the chosen platform/Python (pip download --only-binary)
    offline/llamacpp/   the official llama.cpp release archive for the platform/variant
    offline/models/     GGUF files from the manifest, verified against Hugging Face SHA-256
    offline/python/     (Windows) the official python.org installer, if --python-installer is given
The installer detects offline/ next to it automatically (or pass --offline-dir).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from installer import load_installer_config  # noqa: E402
from installer import model_manager as mm  # noqa: E402
from installer.download_manager import Downloader  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "offline"))
    ap.add_argument("--platform", default="win_amd64", help="pip platform tag, e.g. win_amd64, manylinux2014_x86_64")
    ap.add_argument("--python", default="3.12")
    ap.add_argument("--groups", default="core,system,windows,voice,stt,vision")
    ap.add_argument("--llama", default="cpu", help="cpu or vulkan")
    ap.add_argument("--llama-os", default="win", choices=["win", "ubuntu"])
    ap.add_argument("--models", default="general-small", help="manifest ids, comma separated ('' = none)")
    ap.add_argument("--python-installer", action="store_true", help="also fetch the python.org installer (Windows)")
    a = ap.parse_args()
    out = Path(a.out)
    cfg = load_installer_config()
    pkgs = []
    for g in a.groups.split(","):
        spec = cfg["dependency_groups"].get(g.strip())
        if spec and (not spec.get("platforms") or ("Windows" in spec["platforms"]) == a.platform.startswith("win")):
            pkgs += spec["packages"]
    (out / "wheels").mkdir(parents=True, exist_ok=True)
    print("Downloading wheels:", " ".join(pkgs))
    subprocess.run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--dest", str(out / "wheels"),
                    "--platform", a.platform, "--python-version", a.python, "pip", "wheel"] + pkgs, check=False)
    from isha_core import llama_runtime as L
    L._VARIANT = a.llama
    L._IS_WINDOWS = a.llama_os == "win"
    L._IS_LINUX = a.llama_os == "ubuntu"
    url, tag = L._find_asset_url()
    if url:
        (out / "llamacpp").mkdir(parents=True, exist_ok=True)
        Downloader(url, out / "llamacpp" / url.rsplit("/", 1)[-1]).run()
        print("llama.cpp", tag, "saved")
    else:
        print("WARNING: llama.cpp asset not found")
    ids = [x for x in a.models.split(",") if x]
    by_id = {e.id: e for e in mm.entries(mm.load_manifest())}
    for mid in ids:
        e = by_id[mid]
        for f in e.files:
            mm.resolve_hf_metadata(f)
            Downloader(f.url, out / "models" / f.filename, f.size, f.sha256,
                       progress=lambda p: print(f"\r  {f.filename}: {p.percent:5.1f}%", end="")).run()
            print()
    if a.python_installer:
        u = cfg["python"]["windows_installer_url"]
        Downloader(u, out / "python" / u.rsplit("/", 1)[-1]).run()
    (out / "bundle.json").write_text(json.dumps({"platform": a.platform, "python": a.python, "llama": a.llama,
                                                 "models": ids, "version": cfg["app_version"]}, indent=2))
    print("Offline bundle ready:", out)


if __name__ == "__main__":
    main()
