"""Installer tests - one test (or more) per path in spec §36.

Network is never used: pip is faked, llama.cpp setup is faked or fed a local
archive, and models come from a local HTTP server with Range support that can
drop connections on demand. Everything else is real: file copies, the state
file, config merging, shortcuts, uninstall, and diagnostics (which runs the
installed mani.py in a subprocess).
"""
import hashlib
import io
import json
import os
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer import config_manager as cm
from installer import dependency_manager as deps
from installer import hardware_detector as hwd
from installer import model_manager as mm
from installer import python_manager as pym
from installer import runtime_manager as rt
from installer import uninstaller as un
from installer.download_manager import Downloader, DownloadError
from installer.installer import Installer, InstallOptions, StepFailed, TermsNotAccepted
from isha_core.hardware import HardwareInfo, GPUInfo

from installer_helpers import State, make_server, FakeRun

ROOT = Path(__file__).resolve().parents[1]
MODEL = b"GGUF" + os.urandom(300_000)
MMPROJ = b"GGUF" + os.urandom(50_000)


def sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture()
def server():
    st = State()
    srv, base = make_server(st)
    yield st, base
    srv.shutdown()


# ============================================================== downloads
def test_download_complete_and_verified(server, tmp_path):
    st, base = server
    st.files["/m.gguf"] = MODEL
    seen = []
    d = Downloader(f"{base}/m.gguf", tmp_path / "m.gguf", len(MODEL), sha(MODEL), progress=seen.append)
    p = d.run()
    assert p.read_bytes() == MODEL and not (tmp_path / "m.gguf.part").exists()
    assert seen[-1].state == "done"


def test_download_interrupted_then_resumed(server, tmp_path):
    st, base = server
    st.files["/m.gguf"] = MODEL
    st.cut_after["/m.gguf"] = 100_000
    d = Downloader(f"{base}/m.gguf", tmp_path / "m.gguf", len(MODEL), sha(MODEL), retries=0)
    with pytest.raises(DownloadError) as e:
        d.run()
    assert e.value.kind == "network"
    part = tmp_path / "m.gguf.part"
    assert part.exists() and 0 < part.stat().st_size < len(MODEL)
    assert not (tmp_path / "m.gguf").exists()                 # never "installed" when incomplete
    p = Downloader(f"{base}/m.gguf", tmp_path / "m.gguf", len(MODEL), sha(MODEL)).run()
    assert p.read_bytes() == MODEL
    assert st.requests[-1][1] == f"bytes={part.stat().st_size if part.exists() else 100_000}-" or \
        st.requests[-1][1].startswith("bytes=")                # resumed with a Range request


def test_download_auto_retry_resumes(server, tmp_path):
    st, base = server
    st.files["/m.gguf"] = MODEL
    st.cut_after["/m.gguf"] = 50_000
    d = Downloader(f"{base}/m.gguf", tmp_path / "m.gguf", len(MODEL), sha(MODEL), retries=2)
    d._cancel.wait = lambda t: False                          # no back-off delay in tests
    assert d.run().read_bytes() == MODEL
    assert any(r and r.startswith("bytes=") for _, r in st.requests)


def test_server_ignoring_range_restarts_cleanly(server, tmp_path):
    st, base = server
    st.files["/m.gguf"] = MODEL
    st.cut_after["/m.gguf"] = 70_000
    with pytest.raises(DownloadError):
        Downloader(f"{base}/m.gguf", tmp_path / "m.gguf", len(MODEL), sha(MODEL), retries=0).run()
    st.ignore_range = True
    assert Downloader(f"{base}/m.gguf", tmp_path / "m.gguf", len(MODEL), sha(MODEL)).run().read_bytes() == MODEL


def test_checksum_mismatch_deletes_file(server, tmp_path):
    st, base = server
    st.files["/m.gguf"] = MODEL
    with pytest.raises(DownloadError) as e:
        Downloader(f"{base}/m.gguf", tmp_path / "m.gguf", len(MODEL), "0" * 64).run()
    assert e.value.kind == "checksum"
    assert not (tmp_path / "m.gguf").exists() and not (tmp_path / "m.gguf.part").exists()


def test_pause_resume_cancel(server, tmp_path):
    st, base = server
    big = os.urandom(6 * 1024 * 1024)
    st.files["/big.gguf"] = big
    states = []
    d = Downloader(f"{base}/big.gguf", tmp_path / "big.gguf", len(big), sha(big), progress=lambda p: states.append(p.state))
    d.pause()
    t = threading.Thread(target=lambda: setattr(d, "result", d.run()))
    t.start()
    time.sleep(0.6)
    assert "paused" in states and not (tmp_path / "big.gguf").exists()
    d.resume()
    t.join(20)
    assert d.result.read_bytes() == big
    d2 = Downloader(f"{base}/big.gguf", tmp_path / "b2.gguf", len(big))
    d2.pause()
    errs = []
    t2 = threading.Thread(target=lambda: errs.append(_catch(d2.run)))
    t2.start(); time.sleep(0.3); d2.cancel(); t2.join(10)
    assert errs and errs[0].kind == "cancelled" and not (tmp_path / "b2.gguf").exists()


def _catch(fn):
    try:
        fn()
    except DownloadError as e:
        return e


def test_insufficient_disk_refused_before_download(server, tmp_path):
    st, base = server
    st.files["/m.gguf"] = MODEL
    with pytest.raises(DownloadError) as e:
        Downloader(f"{base}/m.gguf", tmp_path / "m.gguf", len(MODEL), free_space_fn=lambda p: 1000).run()
    assert e.value.kind == "disk" and not st.requests


def test_https_only():
    with pytest.raises(DownloadError) as e:
        Downloader("http://example.com/model.gguf", Path("/tmp/x.gguf"))
    assert e.value.kind == "insecure"


# ============================================================== python
def fake_python_runner(versions: dict):
    """versions: candidate path -> (major, minor, micro, has_venv, bits)"""
    def run(argv):
        key = argv[0] if argv[0] != "py" else f"py{argv[1]}"
        v = versions.get(key)
        if v is None:
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"v": list(v[:3]), "exe": f"/opt/{key}", "venv": v[3], "bits": v[4]}))
    return run


def which_of(paths):
    return lambda n: f"/usr/bin/{n}" if n in paths else None


def test_python_missing():
    info = pym.find_best(runner=fake_python_runner({}), os_name="Linux", which=which_of(set()))
    assert not info.compatible and "not found" in info.reason.lower()


def test_python_present_prefers_recommended():
    runner = fake_python_runner({sys.executable: (3, 13, 1, True, 64), "/usr/bin/python3.12": (3, 12, 7, True, 64)})
    info = pym.find_best(runner=runner, os_name="Linux", which=which_of({"python3.12"}))
    assert info.compatible and info.version[:2] == (3, 12)


def test_python_too_old_newer_and_no_venv():
    old = pym.find_best(runner=fake_python_runner({sys.executable: (3, 8, 10, True, 64)}), os_name="Linux",
                        which=which_of(set()))
    assert not old.compatible and "too old" in old.reason
    new = pym.find_best(runner=fake_python_runner({sys.executable: (3, 14, 0, True, 64)}), os_name="Linux",
                        which=which_of(set()))
    assert new.compatible and "newer than tested" in new.warning
    nov = pym.find_best(runner=fake_python_runner({sys.executable: (3, 12, 0, False, 64)}), os_name="Linux",
                        which=which_of(set()))
    assert not nov.compatible and "venv" in nov.reason


def test_python_install_plans():
    assert pym.install_plan("Windows", which_of({"winget"}))["argv"][:4] == ["winget", "install", "-e", "--id"]
    assert pym.install_plan("Windows", which_of(set()))["method"] == "python.org"
    p = pym.install_plan("Linux", which_of({"apt-get", "pkexec"}))
    assert p["argv"][0] == "pkexec" and "python3-venv" in p["argv"]
    assert pym.install_plan("Linux", which_of({"apt-get"}))["method"] == "manual"
    assert pym.install_python({"method": "python.org", "url": "https://evil.example/py.exe",
                               "describe": ""})["success"] is False


# ============================================================== dependencies
def test_dependency_groups_are_platform_aware():
    feats = {"voice": True, "stt": False, "vision": False, "whatsapp": True, "advanced_pc": True}
    lin = deps.select_groups(feats, "Linux")
    win = deps.select_groups(feats, "Windows")
    assert "windows" not in lin and "windows" in win and "stt" not in lin and "vision" not in lin
    assert "core" in lin and "voice" in lin


def test_installer_groups_cover_every_package_mani_uses(mani):
    listed = {p.lower() for _, p in deps.packages_for(list(deps.load_installer_config()["dependency_groups"]))}
    for p in mani._CORE_PACKAGES + mani._OPTIONAL_PACKAGES + mani._WINDOWS_ONLY_PACKAGES:
        assert p.lower() in listed, f"{p} is used by mani.py but missing from installer_config.json"


def test_dependencies_missing_already_and_optional_failure():
    run = FakeRun(fail={"PyAudio"})
    run.installed = {"PyQt5"}
    rep = deps.install("/venv/python", ["core", "stt"], runner=run)
    st = {r.package: r.status for r in rep.results}
    assert st["PyQt5"] == "already" and st["psutil"] == "installed" and st["PyAudio"] == "failed"
    assert not rep.failed_core and rep.failed_optional[0].package == "PyAudio"
    assert "no compatible wheel" in rep.failed_optional[0].detail


def test_core_failure_reported():
    rep = deps.install("/venv/python", ["core"], runner=FakeRun(fail={"PyQt5"}))
    assert rep.failed_core and rep.failed_core[0].package == "PyQt5"


def test_offline_wheelhouse_args(tmp_path):
    (tmp_path / "wheels").mkdir()
    assert deps.pip_args(tmp_path, internet=False)[-3:] == ["--find-links", str(tmp_path / "wheels"), "--no-index"]
    assert "--no-index" not in deps.pip_args(tmp_path, internet=True)


# ============================================================== llama.cpp / GPU
def test_gpu_backend_choice():
    cpu = HardwareInfo(gpus=[])
    nv = HardwareInfo(gpus=[GPUInfo("NVIDIA RTX 3060", 12, 11, "nvidia")])
    intel = HardwareInfo(gpus=[GPUInfo("Intel UHD 620", vendor="intel")])
    assert hwd.choose_gpu_backend(cpu, True, "Windows")[0] == "cpu"
    assert hwd.choose_gpu_backend(nv, True, "Windows")[0] == "vulkan"
    v, why = hwd.choose_gpu_backend(nv, False, "Linux")
    assert v == "cpu" and "Vulkan runtime is missing" in why
    assert hwd.choose_gpu_backend(intel, True, "Linux")[0] == "cpu"


def _fake_llama_zip(path: Path, name="llama-b9999-bin-ubuntu-x64.zip", evil=False):
    buf = path / name
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("build/bin/llama-server", "#!/bin/sh\necho fake\n")
        if evil:
            z.writestr("../../escape.txt", "x")
    return buf


@pytest.fixture()
def llama_rt(tmp_path, monkeypatch):
    from isha_core import llama_runtime as L
    d = tmp_path / "llamacpp"
    monkeypatch.setattr(L, "LLAMACPP_DIR", d)
    monkeypatch.setattr(L, "_STAMP", d / ".installed.json")
    monkeypatch.setattr(L, "VARIANT_FILE", d / "variant.txt")
    return L


def test_llama_missing_installed_from_offline_archive(llama_rt, tmp_path):
    arch = _fake_llama_zip(tmp_path)
    res = llama_rt.setup("cpu", str(arch), emit=lambda d: None)
    assert res["success"] and res["server_exe"].endswith("llama-server")
    assert (llama_rt.LLAMACPP_DIR / "variant.txt").read_text() == "cpu"
    assert llama_rt.setup("cpu", None, emit=lambda d: None)["success"]      # already installed: reused


def test_llama_archive_path_traversal_rejected(llama_rt, tmp_path):
    arch = _fake_llama_zip(tmp_path, evil=True)
    res = llama_rt.setup("cpu", str(arch), emit=lambda d: None)
    assert not res["success"] and "unsafe path" in " ".join(res["tried"])
    assert not (tmp_path / "escape.txt").exists()


def test_gpu_failure_falls_back_to_cpu(llama_rt, tmp_path, monkeypatch):
    real = llama_rt._install_llamacpp

    def fake(local_archive=None, progress=None):
        if llama_rt._VARIANT == "vulkan":
            raise RuntimeError("vulkan asset missing")
        return real(local_archive=_fake_llama_zip(tmp_path), progress=progress)
    monkeypatch.setattr(llama_rt, "_install_llamacpp", fake)
    res = llama_rt.setup("vulkan", None, emit=lambda d: None)
    assert res["success"] and res["variant"] == "cpu" and res["fallback"]


def test_gpu_variant_skips_cpu_only_wheel(llama_rt, monkeypatch):
    monkeypatch.setattr(llama_rt, "_VARIANT", "vulkan")
    monkeypatch.setattr(llama_rt, "_RESOLVED", None)
    called = []
    monkeypatch.setattr(llama_rt, "_try_import_llama", lambda: called.append("native") or (object, ""))
    monkeypatch.setattr(llama_rt, "_install_llamacpp", lambda **k: "exe")
    monkeypatch.delenv("ISHA_SKIP_BACKEND", raising=False)
    assert llama_rt.resolve_backend()[1] == "server" and not called


# ============================================================== models
def manifest_for(base):
    return {"models": [
        {"id": "gen-s", "name": "Gen S", "roles": ["general"], "tier": "small", "ram_gb": 3,
         "url": f"{base}/gen.gguf", "size": len(MODEL), "sha256": sha(MODEL), "size_gb": 0.0003},
        {"id": "gen-a", "name": "Gen A", "roles": ["general", "reasoning"], "tier": "advanced", "ram_gb": 8,
         "url": f"{base}/gen.gguf", "filename": "gen_adv.gguf", "size": len(MODEL), "sha256": sha(MODEL)},
        {"id": "code-s", "name": "Code S", "roles": ["coding"], "tier": "small", "ram_gb": 3,
         "url": f"{base}/code.gguf", "size": len(MODEL), "sha256": sha(MODEL)},
        {"id": "vis", "name": "Vision", "roles": ["vision"], "tier": "balanced", "ram_gb": 5,
         "url": f"{base}/vis.gguf", "size": len(MODEL), "sha256": sha(MODEL),
         "extra_files": [{"source": {"url": f"{base}/mmproj.gguf"}, "size": len(MMPROJ), "sha256": sha(MMPROJ)}]},
        {"id": "fast", "name": "Fast", "roles": ["fast"], "tier": "small", "ram_gb": 1,
         "url": f"{base}/fast.gguf", "size": len(MODEL), "sha256": sha(MODEL)}],
        "presets": {"minimal": {"roles": ["general"]}, "recommended": {"roles": ["general", "coding", "reasoning"]},
                    "advanced": {"roles": ["general", "coding", "reasoning", "fast", "vision"]}}}


@pytest.mark.parametrize("ram,vram,tier", [(4, 0, "small"), (8, 0, "balanced"), (16, 0, "advanced"),
                                           (8, 8, "advanced"), (None, 0, "balanced")])
def test_hardware_recommendation(ram, vram, tier):
    assert mm.recommend_tier(ram, vram)[0] == tier


def test_real_manifest_is_consistent():
    man = mm.load_manifest()
    es = mm.entries(man)
    assert {r for e in es for r in e.roles} >= {"general", "coding", "reasoning", "fast", "vision"}
    for e in es:
        for f in e.files:
            assert f.url.startswith("https://huggingface.co/") and f.filename.endswith(".gguf")
    for tier in ("small", "balanced", "advanced"):
        for preset in ("minimal", "recommended", "advanced"):
            plan = mm.plan_preset(preset, tier, man)
            assert "general" in plan


def test_presets_dedupe_reasoning_on_small_machines():
    man = mm.load_manifest()
    plan = mm.plan_preset("recommended", "small", man)
    assert plan["reasoning"] is plan["general"]
    assert len(mm.unique_entries(plan)) == 2                 # general + coding, no extra reasoning download
    adv = mm.plan_preset("advanced", "advanced", man)
    assert adv["reasoning"].id == "general-advanced" and "vision" in adv


def test_storage_check_counts_only_missing_files(server, tmp_path):
    st, base = server
    man = manifest_for(base)
    plan = mm.plan_preset("minimal", "small", man)
    need = mm.storage_needed(plan, tmp_path / "models", runtime_installed=True)
    assert need["models"] == len(MODEL) and need["enough"]
    p = tmp_path / "models" / "general" / "gen.gguf"
    p.parent.mkdir(parents=True); p.write_bytes(MODEL)
    assert mm.storage_needed(plan, tmp_path / "models", True)["models"] == 0


def test_gguf_present_is_not_redownloaded(server, tmp_path):
    st, base = server
    st.files["/gen.gguf"] = MODEL
    e = mm.entries(manifest_for(base))[0]
    mm.install_entry(e, tmp_path)
    n = len(st.requests)
    mm.install_entry(e, tmp_path)
    assert len(st.requests) == n


def test_multi_file_model_and_offline_copy(server, tmp_path):
    st, base = server
    vis = [e for e in mm.entries(manifest_for(base)) if e.id == "vis"][0]
    off = tmp_path / "offline"
    (off / "models").mkdir(parents=True)
    (off / "models" / "vis.gguf").write_bytes(MODEL)
    (off / "models" / "mmproj.gguf").write_bytes(MMPROJ)
    paths = mm.install_entry(vis, tmp_path / "models", offline_dir=off)
    assert [p.name for p in paths] == ["vis.gguf", "mmproj.gguf"] and not st.requests


def test_offline_copy_with_wrong_checksum_rejected(server, tmp_path):
    st, base = server
    e = mm.entries(manifest_for(base))[0]
    off = tmp_path / "offline"; (off / "models").mkdir(parents=True)
    (off / "models" / "gen.gguf").write_bytes(b"GGUF corrupted")
    with pytest.raises(DownloadError):
        mm.install_entry(e, tmp_path / "models", offline_dir=off)
    assert not (tmp_path / "models" / "general" / "gen.gguf").exists()


def test_hf_metadata_resolution():
    fs = mm._filespec({"type": "huggingface", "repo": "Org/Repo", "file": "m.gguf"}, 1.0)
    tree = [{"path": "README.md", "size": 5}, {"path": "m.gguf", "size": 1234, "lfs": {"oid": "ab" * 32, "size": 1234}}]
    mm.resolve_hf_metadata(fs, fetch=lambda url, t: tree)
    assert fs.size == 1234 and fs.sha256 == "ab" * 32
    missing = mm._filespec({"type": "huggingface", "repo": "Org/Repo", "file": "gone.gguf"}, 1.0)
    with pytest.raises(DownloadError):
        mm.resolve_hf_metadata(missing, fetch=lambda url, t: tree)
    offline = mm._filespec({"type": "huggingface", "repo": "Org/Repo", "file": "m.gguf"}, 1.0)
    mm.resolve_hf_metadata(offline, fetch=lambda url, t: (_ for _ in ()).throw(OSError("offline")))
    assert offline.sha256 is None                             # honest: unknown, not invented


def test_custom_gguf(tmp_path):
    good = tmp_path / "my.gguf"; good.write_bytes(MODEL)
    bad = tmp_path / "bad.gguf"; bad.write_bytes(b"NOTGGUF")
    assert mm.add_custom(good, "general", tmp_path / "models") == good          # referenced in place
    copied = mm.add_custom(good, "coding", tmp_path / "models", copy=True)
    assert copied.read_bytes() == MODEL and copied.parent.name == "coding"
    with pytest.raises(ValueError):
        mm.add_custom(bad, "general", tmp_path / "models")


# ============================================================== full installation flow
def venv_aware_runner(fake: FakeRun):
    """Real-looking venv creation (symlink to this Python) + fake pip."""
    def run(argv):
        if len(argv) > 2 and argv[1:3] == ["-m", "venv"]:
            target = Path(argv[-1])
            py = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")
            py.parent.mkdir(parents=True, exist_ok=True)
            if not py.exists():
                os.symlink(sys.executable, py)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["-c", "import sys; print(sys.version)"]:
            return SimpleNamespace(returncode=0, stdout=sys.version, stderr="")
        return fake(argv)
    return run


def fake_llama(install_dir, variant, offline_dir, progress):
    d = Path(install_dir) / ".isha_runtime" / "llamacpp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "llama-server").write_text("fake")
    (d / "variant.txt").write_text("cpu")
    return {"success": True, "variant": "cpu", "fallback": variant != "cpu", "server_exe": str(d / "llama-server")}


@pytest.fixture()
def make_installer(server, tmp_path, home):
    st, base = server
    for n in ("gen.gguf", "code.gguf", "vis.gguf", "fast.gguf"):
        st.files["/" + n] = MODEL
    st.files["/mmproj.gguf"] = MMPROJ
    events = []

    def factory(target, **kw):
        feats = kw.pop("features", {"voice": True, "stt": False, "vision": False, "file_index": True,
                                    "whatsapp": False, "advanced_pc": True, "coding": True})
        o = InstallOptions(install_dir=target, accept_terms=kw.pop("accept", True), python_exe=sys.executable,
                           features=feats, preset=kw.pop("preset", "minimal"), tier=kw.pop("tier", "small"),
                           autonomy="balanced", **kw)
        inst = Installer(o, on_event=events.append, source_root=ROOT, pip_runner=venv_aware_runner(FakeRun()),
                         llama_setup=fake_llama)
        inst.manifest = manifest_for(base)
        inst.report.update(hardware=HardwareInfo(ram_total_gb=16, ram_available_gb=10), gpu_backend="cpu")
        return inst
    factory.events = events
    factory.server = st
    return factory


def test_fresh_installation_end_to_end(make_installer, tmp_path, home):
    target = tmp_path / "ISHA"
    inst = make_installer(target, preset="recommended").run()
    assert (target / "mani.py").exists() and (target / "isha_core" / "router.py").exists()
    assert (target / "installer" / "uninstaller.py").exists()
    assert (target / "models" / "general" / "gen.gguf").read_bytes() == MODEL
    assert (target / "models" / "coding" / "code.gguf").exists()
    cfg = json.loads((target / "isha_config.json").read_text())
    assert cfg["models"]["general"] == "models/general/gen.gguf"
    assert cfg["models"]["reasoning"] == cfg["models"]["general"]          # reused, not re-downloaded
    assert cfg["autonomy_level"] == "medium" and cfg["stt_enabled"] is False
    assert set(cfg["blocked_tools"]) >= {"send_whatsapp_message"}          # WhatsApp disabled
    state = json.loads((target / "installer_state.json").read_text())
    assert state["terms"]["version"] == "1.0" and "accepted_at" in state["terms"]
    assert set(state["terms"]) == {"version", "accepted_at"}                # minimal record only
    assert state["complete"] and state["first_run_complete"]
    apps = home / ".local" / "share" / "applications"
    assert (apps / "isha.desktop").exists() and (target / "isha.sh").exists()
    diag = {r["name"]: r for r in inst.report["diagnostics"]}
    assert diag["Python runtime"]["status"] == "READY"
    assert diag["GGUF model"]["status"] == "READY"
    assert diag["llama.cpp"]["status"] == "READY"
    assert diag["Tools"]["status"] == "READY", diag["Tools"]               # installed mani.py imports
    assert diag["STT"]["status"] == "WARNING" and "disabled" in diag["STT"]["detail"]
    assert diag["Model loading"]["status"] == "WARNING"                    # honest: not tested
    kinds = {e["type"] for e in make_installer.events}
    assert {"step", "package", "download"} <= kinds


def test_terms_not_accepted_installs_nothing(make_installer, tmp_path):
    target = tmp_path / "ISHA"
    with pytest.raises(TermsNotAccepted):
        make_installer(target, accept=False).run()
    assert not (target / "mani.py").exists() and not (target / "installer_state.json").exists()


def test_headless_cli_refuses_without_terms(tmp_path, monkeypatch):
    from installer import installer as I
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    code = I.main(["--headless", "--dir", str(tmp_path / "X"), "--preset", "none"])
    assert code == 3 and not (tmp_path / "X" / "mani.py").exists()


def test_insufficient_disk_stops_before_download(make_installer, tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "free_space", lambda p: 10)
    inst = make_installer(tmp_path / "ISHA")
    with pytest.raises(StepFailed) as e:
        inst.run()
    assert e.value.step == "preflight" and "choose_location" in e.value.options
    assert "Not enough disk space" in e.value.message and not make_installer.server.requests


def test_failed_download_offers_recovery_and_resume(make_installer, tmp_path):
    make_installer.server.cut_after["/gen.gguf"] = 10_000
    inst = make_installer(tmp_path / "ISHA")
    choices = []

    def on_fail(f):
        choices.append(f.options)
        return "retry"
    import installer.download_manager as dm
    orig = dm.Downloader.__init__

    def no_retry(self, *a, **k):
        k["retries"] = 0
        orig(self, *a, **k)
    dm.Downloader.__init__ = no_retry
    try:
        inst.run(on_failure=on_fail)
    finally:
        dm.Downloader.__init__ = orig
    assert choices and choices[0] == ("retry", "change_model", "skip", "exit")
    assert (tmp_path / "ISHA" / "models" / "general" / "gen.gguf").read_bytes() == MODEL


def test_skip_models_is_allowed_and_reported(make_installer, tmp_path):
    make_installer.server.files.pop("/gen.gguf")
    inst = make_installer(tmp_path / "ISHA")
    inst.run(on_failure=lambda f: "skip")
    assert any("GGUF models skipped" in w for w in inst.warnings)
    diag = {r["name"]: r for r in inst.report["diagnostics"]}
    assert diag["GGUF model"]["status"] == "MISSING"                       # never claimed


def test_interrupted_install_resumes_completed_steps(make_installer, tmp_path):
    target = tmp_path / "ISHA"
    inst = make_installer(target)
    inst.run(steps=["preflight", "copy_app", "runtime"])
    assert cm.State(target).is_done("runtime")
    make_installer.events.clear()
    make_installer(target).run()
    resumed = [e for e in make_installer.events if e.get("resumed")]
    assert {e["step"] for e in resumed} >= {"copy_app", "runtime"}


def test_upgrade_keeps_config_and_models_and_backs_up(make_installer, tmp_path):
    target = tmp_path / "ISHA"
    make_installer(target).run()
    cfg_path = target / "isha_config.json"
    cfg = json.loads(cfg_path.read_text()); cfg["temperature"] = 0.33
    cfg_path.write_text(json.dumps(cfg))
    (target / "isha_long_memory.json").write_text('[{"id":"m1","text":"keep me","category":"fact"}]')
    n = len(make_installer.server.requests)
    inst = make_installer(target)
    inst.cfg = dict(inst.cfg, app_version="0.6.0")
    inst.run()
    cfg2 = json.loads(cfg_path.read_text())
    assert cfg2["temperature"] == 0.33                                  # user value preserved
    assert (target / "isha_long_memory.json").read_text().count("keep me") == 1
    assert len(make_installer.server.requests) == n                     # models not re-downloaded
    assert any((target / "backup").glob("0.5.0-*/mani.py"))
    st = json.loads((target / "installer_state.json").read_text())
    assert st["version"] == "0.6.0" and st["history"][-1]["from"] == "0.5.0"
    assert list(target.glob("isha_config.json.bak-*"))                  # backup before change


def test_uninstall_defaults_keep_user_data(make_installer, tmp_path, home):
    target = tmp_path / "ISHA"
    make_installer(target).run()
    (target / "isha_long_memory.json").write_text("[]")
    res = un.uninstall(target)
    assert not (target / "mani.py").exists() and not (target / ".isha_runtime").exists()
    assert (target / "models" / "general" / "gen.gguf").exists()
    assert (target / "isha_config.json").exists() and (target / "isha_long_memory.json").exists()
    assert (target / "ISHA_USER_DATA_KEPT.txt").exists()
    assert not (home / ".local/share/applications/isha.desktop").exists()


def test_reinstall_after_uninstall_reuses_models(make_installer, tmp_path):
    target = tmp_path / "ISHA"
    make_installer(target).run()
    un.uninstall(target, {"config": True})              # keeps models
    n = len(make_installer.server.requests)
    make_installer(target).run()
    assert (target / "mani.py").exists() and len(make_installer.server.requests) == n


def test_full_uninstall_removes_everything(make_installer, tmp_path):
    target = tmp_path / "ISHA"
    make_installer(target).run()
    res = un.uninstall(target, {k: True for k in un.DEFAULTS})
    assert not target.exists() and not res.errors


def test_uninstall_refuses_non_isha_folder(tmp_path):
    (tmp_path / "mani.py").write_text("important user file")
    with pytest.raises(RuntimeError):
        un.uninstall(tmp_path)
    assert (tmp_path / "mani.py").exists()


# ============================================================== features / diagnostics
def test_disabled_features_are_really_off(mani, tmp_path):
    ch = cm.feature_config({"voice": False, "stt": False, "vision": False, "whatsapp": False,
                            "advanced_pc": False, "coding": True, "file_index": False}, "ask")
    assert ch["tts_enabled"] is False and ch["file_index_enabled"] is False and ch["autonomy_level"] == "low"
    assert {"describe_screen", "run_terminal_command", "send_whatsapp_message"} <= set(ch["blocked_tools"])
    assert "run_project" not in ch["blocked_tools"]
    cfg = mani.load_config()
    saved = Path(mani.CONFIG_FILE).read_text() if Path(mani.CONFIG_FILE).exists() else None
    try:
        cfg["blocked_tools"] = ch["blocked_tools"]
        mani.save_config(cfg)
        summary = mani.capability_summary()
        assert "send_whatsapp_message" not in summary and "describe_screen" not in summary
        assert "find_files" in summary
        ok, out, _ = mani.run_tool_gated("send_whatsapp_message", {"contact": "x", "message": "y"},
                                         ask=lambda *a: True, cfg=cfg)
        assert not ok and "blocked" in out
    finally:
        if saved is None:
            Path(mani.CONFIG_FILE).unlink(missing_ok=True)
        else:
            Path(mani.CONFIG_FILE).write_text(saved)


def test_reenabling_a_feature_unblocks_it():
    off = cm.feature_config({"whatsapp": False}, "advanced")
    on = cm.feature_config({"whatsapp": True}, "advanced", existing_blocked=off["blocked_tools"] + ["my_tool"])
    assert "send_whatsapp_message" not in on["blocked_tools"] and "my_tool" in on["blocked_tools"]


def test_system_check_reports_real_values(tmp_path):
    rep = hwd.run_system_check(tmp_path, pym.find_best(), probe_internet=False)
    names = [c.name for c in rep.checks]
    assert names[:5] == ["Operating System", "CPU", "RAM", "Storage", "Python"]
    assert rep.get("Permissions").status == "READY"
    assert all(c.status in ("READY", "WARNING", "MISSING") for c in rep.checks)


def test_first_run_needed_only_for_fresh_installs(tmp_path):
    from installer.installer import needs_first_run
    assert needs_first_run(tmp_path)
    (tmp_path / "isha_config.json").write_text("{}")
    assert not needs_first_run(tmp_path)                 # existing users are not nagged


# ============================================================== GUI (offscreen)
def test_setup_gui_terms_gate_and_navigation(tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from installer import setup_gui as G
    monkeypatch.setattr(G.hwd, "check_internet", lambda *a, **k: (False, ""))
    wiz = G.Wizard(G.setup_pages)
    wiz.opts.install_dir = tmp_path / "ISHA"
    assert wiz.next.text() == "Get Started"
    wiz.go(1)
    terms = wiz.pages[wiz.idx]
    assert terms.title == "Terms" and not wiz.next.isEnabled()          # disabled until agreed
    wiz.go(1)
    assert wiz.pages[wiz.idx] is terms                                  # cannot skip
    terms.agree.setChecked(True)
    assert wiz.next.isEnabled()
    wiz.go(1)
    assert wiz.opts.accept_terms and wiz.pages[wiz.idx].title == "Privacy"
    wiz.go(1)
    sysp = wiz.pages[wiz.idx]
    sysp._w.wait(60000); app.processEvents()
    assert sysp.report is not None and sysp.can_next()
    wiz.go(1); wiz.go(1)                                                 # location -> features
    assert wiz.pages[wiz.idx].title == "Features"
    wiz.go(1)
    models = wiz.pages[wiz.idx]
    assert models.title == "AI model" and "Recommendation" in models.hwinfo.text()
    models.radios["custom"].setChecked(True)
    assert not models.can_next()                                          # custom needs a choice
    models.radios["minimal"].setChecked(True)
    assert models.table.rowCount() == 1 and "required" in models.storage.text()
    wiz.close()


def test_first_run_and_uninstall_dialogs_construct(tmp_path):
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from installer import setup_gui as G

    def factory(w):
        w.opts.install_dir = tmp_path
        return G.first_run_pages(w)
    wiz = G.Wizard(factory, "Welcome to ISHA")
    assert [p.title for p in wiz.pages] == ["Welcome", "AI model", "Voice", "Permissions", "Search", "Tools",
                                             "Install", "Finish"]
    wiz.close()
    (tmp_path / "installer_state.json").write_text("{}")
    dlg = G.UninstallDialog(tmp_path)
    assert not dlg.boxes["models"].isChecked() and not dlg.boxes["memory"].isChecked()
    assert dlg.boxes["app"].isChecked() and not dlg.boxes["app"].isEnabled()
    dlg.close()


def test_existing_server_binary_skips_pip_on_launch(llama_rt, tmp_path, monkeypatch):
    llama_rt.setup("cpu", str(_fake_llama_zip(tmp_path)), emit=lambda d: None)
    monkeypatch.setattr(llama_rt, "_VARIANT", "cpu")
    monkeypatch.setattr(llama_rt, "_RESOLVED", None)
    monkeypatch.delenv("ISHA_SKIP_BACKEND", raising=False)
    monkeypatch.setattr(llama_rt, "_try_import_llama", lambda: (None, "no module"))
    monkeypatch.setattr(llama_rt, "_try_prebuilt_wheel", lambda: pytest.fail("pip must not run"))
    assert llama_rt.resolve_backend()[1] == "server"
