"""ISHA installer orchestrator + command line.

Steps (each idempotent and recorded in installer_state.json, so a re-run
resumes instead of starting over):

    preflight -> copy_app -> runtime -> dependencies -> llama -> models
              -> configure -> shortcuts -> diagnostics -> finalize

A failing step raises StepFailed with the recovery options that make sense
for it (retry / skip / change_model / choose_location / exit); the GUI and
the CLI both present exactly those.

    python -m installer                       # GUI (default when a display exists)
    python -m installer --headless --accept-terms --preset minimal
    python -m installer --uninstall [--remove-models ...]
    python -m installer --first-run --install-dir <dir>
    python -m installer --diagnose --install-dir <dir> [--deep]
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from installer import SOURCE_ROOT, load_installer_config
from installer import config_manager as cm
from installer import dependency_manager as deps
from installer import diagnostics
from installer import hardware_detector as hwd
from installer import model_manager as mm
from installer import python_manager as pym
from installer import runtime_manager as rt
from installer import shortcut_manager as sc
from installer.download_manager import DownloadError, human_bytes

STEPS = ["preflight", "copy_app", "runtime", "dependencies", "llama", "models", "configure",
         "shortcuts", "diagnostics", "finalize"]
STEP_LABELS = {"preflight": "Checking requirements", "copy_app": "Installing ISHA files",
               "runtime": "Python runtime", "dependencies": "Python packages", "llama": "llama.cpp AI runtime",
               "models": "GGUF models", "configure": "Configuration", "shortcuts": "Shortcuts",
               "diagnostics": "Diagnostics", "finalize": "Finishing"}
OPTIONAL_STEPS = {"llama", "models", "shortcuts", "diagnostics"}


class TermsNotAccepted(Exception):
    pass


class StepFailed(Exception):
    def __init__(self, step: str, message: str, options=("retry", "exit")):
        super().__init__(message)
        self.step, self.message, self.options = step, message, tuple(options)


def default_install_dir(os_name: str = platform.system()) -> Path:
    if os_name == "Windows":
        # Per-user and writable: ISHA writes config/models next to mani.py, which
        # C:\Program Files would forbid without running ISHA as administrator.
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Programs" / "ISHA"
    if os_name == "Darwin":
        return Path.home() / "Applications" / "ISHA"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "isha"


@dataclass
class InstallOptions:
    install_dir: Path = field(default_factory=default_install_dir)
    accept_terms: bool = False
    privacy_ack: bool = True
    python_exe: str = ""
    features: dict = field(default_factory=dict)
    autonomy: str = "advanced"                 # ask | balanced | advanced
    preset: str = "minimal"                    # minimal | recommended | advanced | none | custom
    tier: str = ""                             # "" = recommend from hardware
    model_ids: list = field(default_factory=list)          # custom selection
    custom_models: dict = field(default_factory=dict)      # role -> existing .gguf path
    copy_custom: bool = False
    variant: str = ""                          # "" = auto-detect
    offline_dir: Path | None = None
    desktop_shortcut: bool = True
    index_roots: list | None = None
    index_excludes: list | None = None
    internet: bool = True
    deep_diagnostics: bool = False


class Installer:
    def __init__(self, opts: InstallOptions, on_event=None, source_root: Path = SOURCE_ROOT,
                 pip_runner=None, llama_setup=None, downloader_cls=None, fetch=None):
        self.o = opts
        self.o.install_dir = Path(opts.install_dir).expanduser().resolve()
        self.cfg = load_installer_config()
        self.source = Path(source_root).resolve()
        self.emit = on_event or (lambda e: None)
        self.cancel = threading.Event()
        self.state = cm.State(self.o.install_dir)
        self.warnings: list = []
        self.report: dict = {}
        self._pip_runner = pip_runner
        self._llama_setup = llama_setup or rt.setup_llama
        self._downloader_cls = downloader_cls
        self._fetch = fetch
        self.active_downloader = None
        self.manifest = mm.load_manifest()

    # ------------------------------------------------------------ helpers
    def _ev(self, **kw):
        try:
            self.emit(kw)
        except Exception:
            pass

    def model_plan(self) -> dict:
        info = self.report.get("hardware")
        if self.o.preset == "none":
            return {}
        if self.o.preset == "custom":
            by_id = {e.id: e for e in mm.entries(self.manifest)}
            plan = {}
            for mid in self.o.model_ids:
                e = by_id.get(mid)
                if e:
                    for r in e.roles:
                        plan.setdefault(r, e)
            return plan
        tier = self.o.tier
        if not tier:
            ram = getattr(info, "ram_total_gb", None) if info else None
            vram = getattr(info, "total_vram_gb", 0.0) if info else 0.0
            tier = mm.recommend_tier(ram, vram)[0]
        return mm.plan_preset(self.o.preset, tier, self.manifest)

    # ------------------------------------------------------------ steps
    def step_preflight(self):
        if not self.o.accept_terms:
            raise TermsNotAccepted("The Terms & Conditions must be accepted before installation.")
        self.o.install_dir.mkdir(parents=True, exist_ok=True)
        ok, why = hwd.writable(self.o.install_dir)
        if not ok:
            raise StepFailed("preflight", f"Cannot write to {self.o.install_dir}: {why}",
                             ("choose_location", "exit"))
        if not self.o.python_exe:
            best = pym.find_best()
            if not best.compatible:
                raise StepFailed("preflight", f"{best.reason}. Install Python {self.cfg['python']['recommended']} "
                                 f"(the Welcome screen offers this) and retry.", ("retry", "exit"))
            self.o.python_exe = best.path
        plan = self.model_plan()
        runtime_ok = rt.venv_python(self.o.install_dir).exists()
        need = mm.storage_needed(plan, self.o.install_dir / "models", runtime_ok, self.cfg["runtime_space_gb"])
        self.report["storage"] = need
        if not need["enough"]:
            raise StepFailed("preflight", (f"Not enough disk space. Required: {human_bytes(need['total'])}, "
                                           f"available: {human_bytes(need['available'])}."),
                             ("choose_location", "change_model", "exit"))
        self.state.record_terms(self.cfg["terms_version"])
        self.state.data.setdefault("install_dir", str(self.o.install_dir))
        self.state.save()

    def step_copy_app(self):
        if self.source == self.o.install_dir:
            self._ev(type="log", message="Running from the install folder - no copy needed.")
            return
        prev = self.state.data.get("version")
        if prev and (self.o.install_dir / "mani.py").exists():
            bdir = self.o.install_dir / "backup" / f"{prev}-{datetime.now():%Y%m%d-%H%M%S}"
            bdir.mkdir(parents=True, exist_ok=True)
            for name in ("mani.py", "isha_core"):
                src = self.o.install_dir / name
                if src.is_dir():
                    shutil.copytree(src, bdir / name, ignore=shutil.ignore_patterns("__pycache__"))
                elif src.exists():
                    shutil.copy2(src, bdir / name)
            backups = sorted((self.o.install_dir / "backup").iterdir())
            for old in backups[:-2]:
                shutil.rmtree(old, ignore_errors=True)
            self._ev(type="log", message=f"Upgrade: previous version {prev} backed up to {bdir}")
        names = [n for n in self.cfg["app_files"] if (self.source / n).exists()]
        names += [p.name for p in self.source.glob("*.md") if p.name not in names]
        for i, name in enumerate(names, 1):
            src, dst = self.source / name, self.o.install_dir / name
            if src.is_dir():
                tmp = dst.with_name(dst.name + ".new")
                shutil.rmtree(tmp, ignore_errors=True)
                shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                                         "models_manifest.local.json"))
                shutil.rmtree(dst, ignore_errors=True)
                tmp.rename(dst)
            else:
                shutil.copy2(src, dst)
            self._ev(type="progress", step="copy_app", fraction=i / len(names), message=name)
        for role in ("general", "coding", "reasoning", "study", "fast", "vision"):
            (self.o.install_dir / "models" / role).mkdir(parents=True, exist_ok=True)
        if not (self.o.install_dir / "mani.py").exists():
            raise StepFailed("copy_app", "mani.py was not found in the installer package.", ("exit",))

    def step_runtime(self):
        res = rt.create_venv(self.o.install_dir, self.o.python_exe,
                             progress=lambda m: self._ev(type="log", message=m), runner=self._pip_runner)
        if not res["success"]:
            raise StepFailed("runtime", res["message"], ("retry", "exit"))

    def step_dependencies(self):
        groups = deps.select_groups(self.o.features)
        py = str(rt.venv_python(self.o.install_dir))

        def prog(i, n, pkg, status, detail):
            self._ev(type="package", step="dependencies", index=i, total=n, package=pkg, status=status,
                     detail=detail)
        rep = deps.install(py, groups, progress=prog, offline_dir=self.o.offline_dir, internet=self.o.internet,
                           runner=self._pip_runner, cancel=self.cancel)
        self.report["dependencies"] = rep
        for r in rep.failed_optional:
            self.warnings.append(f"{r.package} ({r.group}) not installed: {r.detail}")
        self.state.data["optional_failures"] = [f"{r.package}: {r.detail}" for r in rep.failed_optional]
        if rep.failed_core:
            raise StepFailed("dependencies", "Core packages failed: " + "; ".join(
                f"{r.package}: {r.detail}" for r in rep.failed_core), ("retry", "exit"))

    def step_llama(self):
        variant = self.o.variant or self.report.get("gpu_backend", "cpu")
        res = self._llama_setup(self.o.install_dir, variant, self.o.offline_dir,
                                lambda d: self._ev(type="llama", **d))
        self.report["llama"] = res
        if not res.get("success"):
            raise StepFailed("llama", "llama.cpp runtime could not be installed: " + "; ".join(res.get("tried", [])),
                             ("retry", "skip", "exit"))
        if res.get("fallback"):
            self.warnings.append(f"GPU build ({variant}) failed; ISHA will use CPU mode.")
            self._ev(type="log", message="GPU acceleration unavailable - ISHA will use CPU mode.")
        self.state.data["llama_variant"] = res.get("variant", "cpu")

    def step_models(self):
        plan = self.model_plan()
        installed = dict(self.state.data.get("models") or {})
        models_dir = self.o.install_dir / "models"
        for e in mm.unique_entries(plan):
            if self.cancel.is_set():
                raise StepFailed("models", "Cancelled.", ("retry", "skip", "exit"))
            self._ev(type="model", step="models", model=e.name, id=e.id, message=f"Installing {e.name}")
            try:
                kw = {"downloader_cls": self._downloader_cls} if self._downloader_cls else {}
                paths = mm.install_entry(
                    e, models_dir, self.o.offline_dir,
                    progress=lambda fn, p: self._ev(type="download", file=fn, done=p.done, total=p.total,
                                                    speed=p.speed, eta=p.eta, state=p.state),
                    fetch=self._fetch, register=lambda d: setattr(self, "active_downloader", d), **kw)
            except DownloadError as ex:
                opts = ("retry", "change_model", "skip", "exit")
                if ex.kind == "disk":
                    opts = ("choose_location", "change_model", "skip", "exit")
                raise StepFailed("models", f"{e.name}: {ex}", opts)
            finally:
                self.active_downloader = None
            rel = paths[0].relative_to(self.o.install_dir).as_posix()
            for role, entry in plan.items():
                if entry is e:
                    installed[role] = rel
            self.state.data["models"] = installed
            self.state.save()
        for role, path in self.o.custom_models.items():
            try:
                p = mm.add_custom(Path(path), role, models_dir, copy=self.o.copy_custom)
            except ValueError as ex:
                raise StepFailed("models", str(ex), ("retry", "skip", "exit"))
            installed[role] = p.relative_to(self.o.install_dir).as_posix() \
                if p.is_relative_to(self.o.install_dir) else str(p)
        self.state.data["models"] = installed

    def step_configure(self):
        cfg_path = self.o.install_dir / cm.CONFIG_FILE
        existing = cm.read_json(cfg_path)
        changes = cm.feature_config(self.o.features, self.o.autonomy, self.o.index_roots, self.o.index_excludes,
                                    existing.get("blocked_tools"))
        models = self.state.data.get("models") or {}
        if models:
            changes["models"] = dict(models)
        variant = self.state.data.get("llama_variant", "cpu")
        if variant != "cpu" and int(existing.get("gpu_layers", 0) or 0) == 0:
            changes["gpu_layers"] = 99
        if not existing:
            changes.setdefault("model_mode", "auto")
        cm.update_config(self.o.install_dir, changes)
        self.state.data["features"] = {k: bool(v) for k, v in self.o.features.items()}
        self.state.data["autonomy"] = self.o.autonomy
        self.state.save()

    def step_shortcuts(self):
        res = sc.create(self.o.install_dir, desktop=self.o.desktop_shortcut)
        try:
            size_kb = sum(f.stat().st_size for f in self.o.install_dir.rglob("*") if f.is_file()) // 1024
            sc.register_uninstaller(self.o.install_dir, self.cfg["app_version"], size_kb)
        except Exception as e:
            self.warnings.append(f"Apps & features entry not created: {e}")
        self.state.data["shortcuts"] = res.get("created", [])

    def step_diagnostics(self):
        py = str(rt.venv_python(self.o.install_dir))
        results = diagnostics.run(self.o.install_dir, py, deep=self.o.deep_diagnostics)
        self.report["diagnostics"] = results
        self.state.data["last_diagnostics"] = results

    def step_finalize(self):
        d = self.state.data
        prev = d.get("version")
        d["version"] = self.cfg["app_version"]
        d.setdefault("installed_at", cm.now())
        d["updated_at"] = cm.now()
        if prev and prev != d["version"]:
            d.setdefault("history", []).append({"from": prev, "to": d["version"], "at": cm.now()})
        d["first_run_complete"] = True
        d["warnings"] = self.warnings
        d["completed_steps"] = []             # a finished install starts fresh next time
        d["complete"] = True
        self.state.save()

    # ------------------------------------------------------------ driver
    def run_step(self, name: str):
        self._ev(type="step", step=name, status="start", label=STEP_LABELS[name])
        getattr(self, f"step_{name}")()
        if name not in ("preflight", "finalize"):
            self.state.step_done(name)
        self._ev(type="step", step=name, status="done", label=STEP_LABELS[name])

    def run(self, steps=None, on_failure=None):
        """on_failure(StepFailed) -> 'retry' | 'skip' | 'exit' (default: raise)."""
        if "hardware" not in self.report:
            info = hwd.hw.probe()
            self.report["hardware"] = info
            self.report["gpu_backend"], self.report["gpu_reason"] = hwd.choose_gpu_backend(
                info, hwd.vulkan_available())
        resume = not self.state.data.get("complete")
        for name in (steps or STEPS):
            if resume and name not in ("preflight", "configure", "diagnostics", "finalize") and \
                    self.state.is_done(name) and name != "models":
                self._ev(type="step", step=name, status="done", label=STEP_LABELS[name], resumed=True)
                continue
            while True:
                try:
                    self.run_step(name)
                    break
                except StepFailed as f:
                    self._ev(type="step", step=name, status="failed", label=STEP_LABELS[name],
                             message=f.message, options=f.options)
                    choice = on_failure(f) if on_failure else "raise"
                    if choice == "retry":
                        continue
                    if choice == "skip" and "skip" in f.options:
                        self.warnings.append(f"{STEP_LABELS[name]} skipped: {f.message}")
                        self._ev(type="step", step=name, status="skipped", label=STEP_LABELS[name])
                        break
                    raise
        return self


# ------------------------------------------------------------ first-run / helpers
def needs_first_run(install_dir: Path) -> bool:
    install_dir = Path(install_dir)
    st = cm.read_json(install_dir / cm.STATE_FILE)
    if st.get("first_run_complete"):
        return False
    # Existing users who configured ISHA before the installer existed are not nagged.
    return not (install_dir / cm.CONFIG_FILE).exists()


def _parse_features(s: str | None, disable: str | None, cfg: dict) -> dict:
    feats = {k: v.get("default", False) for k, v in cfg["features"].items()}
    if s:
        chosen = {x.strip() for x in s.split(",") if x.strip()}
        feats = {k: (k in chosen) for k in feats}
    for x in (disable or "").split(","):
        if x.strip() in feats:
            feats[x.strip()] = False
    return feats


def headless(args) -> int:
    cfg = load_installer_config()
    print(f"ISHA {cfg['app_version']} installer (headless)")
    if not args.accept_terms:
        print((Path(__file__).parent / "TERMS.txt").read_text(encoding="utf-8"))
        if args.yes or input("Do you accept the Terms & Conditions? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Terms not accepted - nothing was installed.")
            return 3
        args.accept_terms = True
    custom = {}
    for item in args.custom_model or []:
        role, _, path = item.partition("=")
        custom[role.strip()] = path.strip()
    opts = InstallOptions(
        install_dir=Path(args.dir or default_install_dir()), accept_terms=True,
        features=_parse_features(args.features, args.disable, cfg), autonomy=args.autonomy,
        preset=("custom" if args.models else args.preset), tier=args.tier or "",
        model_ids=[m for m in (args.models or "").split(",") if m], custom_models=custom,
        variant=args.variant or "", offline_dir=Path(args.offline_dir) if args.offline_dir else None,
        desktop_shortcut=not args.no_desktop_shortcut, deep_diagnostics=args.deep)
    last = {"t": 0}

    def show(e):
        t = e.get("type")
        if t == "step" and e["status"] in ("start", "failed", "skipped"):
            print(f"\n== {e['label']} [{e['status']}]" + (f": {e.get('message')}" if e.get("message") else ""))
        elif t == "package" and e["status"] in ("installed", "failed", "already"):
            mark = {"installed": "+", "already": "=", "failed": "!"}[e["status"]]
            print(f"  {mark} {e['package']}" + (f"  ({e['detail']})" if e.get("detail") else ""))
        elif t == "download" and (time.time() - last["t"] > 2 or e["state"] == "done"):
            last["t"] = time.time()
            pct = 100 * e["done"] / e["total"] if e["total"] else 0
            eta = f", ETA {int(e['eta'])}s" if e.get("eta") else ""
            print(f"  {e['file']}: {pct:5.1f}% {human_bytes(e['done'])}/{human_bytes(e['total'])} "
                  f"@ {human_bytes(e['speed'])}/s{eta} [{e['state']}]")
        elif t in ("log",):
            print("  " + e["message"])

    def on_fail(f: StepFailed):
        if args.yes:
            return "skip" if "skip" in f.options else "exit"
        ans = input(f"  Options: {', '.join(f.options)} > ").strip().lower()
        return ans if ans in f.options else "exit"

    inst = Installer(opts, on_event=show)
    print("\nChecking system...")
    rep = hwd.run_system_check(opts.install_dir, pym.find_best(), probe_internet=True)
    for c in rep.checks:
        print(f"  {c.status:<8} {c.name}: {c.detail}")
    opts.internet = rep.internet
    inst.report.update(hardware=rep.hardware, gpu_backend=rep.gpu_backend, gpu_reason=rep.gpu_reason)
    if rep.blocking:
        print("Cannot continue:", "; ".join(c.detail for c in rep.blocking))
        return 2
    try:
        inst.run(on_failure=on_fail)
    except TermsNotAccepted as e:
        print(e)
        return 3
    except StepFailed as f:
        print(f"\nInstallation stopped at '{STEP_LABELS[f.step]}': {f.message}")
        print("Run the installer again to resume from this step.")
        return 1
    print("\nDiagnostics:")
    for r in inst.report.get("diagnostics", []):
        print(f"  {r['status']:<8} {r['name']}: {r['detail']}")
    for w in inst.warnings:
        print("  warning:", w)
    print(f"\nISHA is installed in {opts.install_dir}")
    print("Start it from the ISHA shortcut, or run:", " ".join(sc.launch_command(opts.install_dir)))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="installer", description="ISHA installer")
    ap.add_argument("--headless", action="store_true", help="text-mode installation")
    ap.add_argument("--dir", help="installation folder")
    ap.add_argument("--accept-terms", action="store_true")
    ap.add_argument("--yes", action="store_true", help="non-interactive")
    ap.add_argument("--preset", default="minimal", choices=["minimal", "recommended", "advanced", "none"])
    ap.add_argument("--tier", choices=["small", "balanced", "advanced"])
    ap.add_argument("--models", help="comma-separated manifest ids (custom selection)")
    ap.add_argument("--custom-model", action="append", help="role=path/to/model.gguf")
    ap.add_argument("--features", help="comma-separated features to enable (default: installer defaults)")
    ap.add_argument("--disable", help="comma-separated features to disable")
    ap.add_argument("--autonomy", default="advanced", choices=["ask", "balanced", "advanced"])
    ap.add_argument("--variant", help="llama.cpp build: cpu, vulkan, cuda-12.4 ...")
    ap.add_argument("--offline-dir", help="folder with wheels/, llamacpp/, models/")
    ap.add_argument("--no-desktop-shortcut", action="store_true")
    ap.add_argument("--deep", action="store_true", help="diagnostics: also load the model")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--install-dir", help="existing installation (uninstall / first-run / diagnose)")
    for cat in ("models", "cache", "config", "memory", "all"):
        ap.add_argument(f"--remove-{cat}", action="store_true")
    ap.add_argument("--first-run", action="store_true", help="first-run configuration wizard")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--check", action="store_true", help="system check only")
    return ap


def _has_display() -> bool:
    if platform.system() in ("Windows", "Darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    here = Path(args.install_dir) if args.install_dir else SOURCE_ROOT
    if args.check:
        rep = hwd.run_system_check(Path(args.dir or default_install_dir()), pym.find_best())
        for c in rep.checks:
            print(f"{c.status:<8} {c.name}: {c.detail}")
        print(f"llama.cpp backend: {rep.gpu_backend} ({rep.gpu_reason})")
        return 0 if not rep.blocking else 2
    if args.diagnose:
        for r in diagnostics.run(here, deep=args.deep):
            print(f"{r['status']:<8} {r['name']}: {r['detail']}")
        return 0
    if args.uninstall:
        if not args.headless and _has_display():
            try:
                from installer.setup_gui import run_uninstall_gui
                return run_uninstall_gui(here)
            except ImportError:
                pass
        from installer.uninstaller import main_cli
        return main_cli(here, args)
    if args.first_run:
        from installer.setup_gui import run_first_run
        return run_first_run(here)
    if args.headless or not _has_display():
        return headless(args)
    try:
        from installer.setup_gui import run_setup
    except ImportError as e:
        print(f"GUI unavailable ({e}); falling back to text mode.")
        return headless(args)
    return run_setup(Path(args.dir) if args.dir else None, offline_dir=args.offline_dir)


if __name__ == "__main__":
    raise SystemExit(main())
