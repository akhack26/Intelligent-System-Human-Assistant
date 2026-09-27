# ISHA installer — architecture (for maintainers)

The installer is a **deployment layer around the existing app**. It never imports `mani.py` in-process and
reuses ISHA's own runtime logic instead of duplicating it.

## What it reuses from ISHA

| ISHA already had | Installer uses it by |
|---|---|
| `.isha_runtime/` venv that `mani.py` re-execs into | creating exactly that venv (`runtime_manager.venv_python`) |
| package lists `_CORE_PACKAGES`, `_OPTIONAL_PACKAGES`, `_WINDOWS_ONLY_PACKAGES` | mirroring them in `installer_config.json` groups — **a test fails if they drift** |
| 3-layer llama.cpp resolver (native import → prebuilt wheel → official `llama-server`) | moving it verbatim to `isha_core/llama_runtime.py` (mani re-imports every name) and running `python -m isha_core.llama_runtime --setup --variant X --json` **inside the ISHA venv** |
| `ISHA_LLAMA_VARIANT` (cpu/vulkan/cuda/hip) | persisting the detected variant in `llamacpp/variant.txt`, which `mani.py` now reads |
| model discovery (`models/<role>/`, config `models.<role>`) | installing into those folders and writing those keys |
| self-diagnostics | running an equivalent probe inside the venv, including importing the installed `mani.py` |
| `_fix_opencv_qt_conflict` | the same fix after vision packages |

## Components

```
ISHA_Setup.exe (packaging/ISHA_Setup.iss, Inno Setup)  ─┐
ISHA_Setup.cmd → ISHA_Setup.ps1  (Windows bootstrapper) ├─► find/install Python ─► setup env (PyQt5)
install.sh                       (Linux bootstrapper)   ─┘            │
                                                                      ▼
installer/                                                 python -m installer  (GUI or --headless)
  setup_gui.py          wizard, first-run wizard, uninstall dialog (QThread workers, never blocks)
  installer.py          orchestrator: resumable steps, StepFailed(options), CLI
  hardware_detector.py  READY/WARNING/MISSING checks, GPU backend choice
  python_manager.py     candidates, compatibility, official install paths (winget / signed python.org / pkexec)
  runtime_manager.py    .isha_runtime venv, llama.cpp setup in that venv
  dependency_manager.py platform/feature-aware groups, per-package progress, offline wheelhouse
  download_manager.py   HTTPS-only resumable downloads, SHA-256, atomic rename
  model_manager.py      manifest, recommendation, presets, storage math, HF metadata, custom GGUF
  config_manager.py     isha_config.json merge (+backup), installer_state.json
  shortcut_manager.py   Start Menu/Desktop .lnk + HKCU uninstall entry; .desktop files on Linux
  uninstaller.py        category-based removal, safe by default
  diagnostics.py        first-run checks inside the runtime
  installer_config.json dependency groups, features, python policy, app files
  models_manifest.json  model catalogue (data, not code)
  TERMS.txt PRIVACY.txt
```

## Install steps

`preflight → copy_app → runtime → dependencies → llama → models → configure → shortcuts → diagnostics → finalize`

* Every step is idempotent and recorded in `installer_state.json → completed_steps`; an interrupted install
  resumes. `models` always re-checks (verified files are skipped).
* Features are chosen **before** dependencies so unused packages are never installed (the spec lists
  feature selection later; installing first and asking later would waste downloads).
* Failures raise `StepFailed(step, message, options)`. Options per step: dependencies `retry/exit`,
  llama `retry/skip/exit`, models `retry/change_model/skip/exit` (`choose_location` for disk errors),
  preflight disk `choose_location/change_model/exit`.
* Terms: the orchestrator itself refuses to run without acceptance (not only the GUI). Stored: terms version
  + timestamp, nothing else.

## GPU backend policy

Auto-selected only when a self-contained prebuilt build exists: **Vulkan** for NVIDIA/AMD GPUs when the
Vulkan loader is present (Windows and Linux). Otherwise CPU, with the reason shown. CUDA/HIP builds need
separately installed runtimes, so they are manual (`--variant cuda-12.4`). If a GPU build fails to install,
`llama_runtime.setup()` removes it and installs the CPU build (`fallback: true`, reported as a warning,
never fatal). A GPU variant makes the resolver use `llama-server` directly (the pip wheel is CPU-only).

## Model manifest

`installer/models_manifest.json`; maintainers may ship `models_manifest.local.json` to add/override entries
without editing code. Entry fields: `id, name, roles[], tier (small|balanced|advanced), source
{type: huggingface, repo, file} | url+filename, size_gb, ram_gb, sha256?, size?, extra_files[], license,
description`. Presets map to roles.

Integrity: for Hugging Face sources the installer reads `api/models/<repo>/tree/main`; the LFS `oid` is the
file's SHA-256, used together with the exact size. A pinned `sha256` in the manifest always wins. If neither
is available (offline, API change), the file is verified by size only and this is not reported as
checksum-verified. Nothing is marked installed until it is present and verified.

## Security properties

* HTTPS only (plain HTTP only to 127.0.0.1, for tests). Sources: python.org, PyPI, GitHub releases of
  ggml-org/llama.cpp, Hugging Face — all shown in the Privacy screen.
* The Python installer runs only with a valid Authenticode signature from the Python Software Foundation.
* No downloaded script is ever executed. llama.cpp archives are extracted with path-traversal checks.
* No admin rights, no security settings touched. `-ExecutionPolicy Bypass` is scoped to the setup process.
* The uninstaller deletes only known names inside a folder containing `installer_state.json`.

## Update design

* **App:** re-running a newer installer = upgrade: backup of `mani.py`/`isha_core`, file replace, dependency
  top-up, config merge (never overwritten), models untouched, `history` entry in the state file.
* **Dependencies:** `dependencies` step installs only missing packages; forcing upgrades is a future flag.
* **llama.cpp:** delete `.isha_runtime/llamacpp` (or a future `--update-runtime`) and re-run; the resolver
  fetches the newest release with fallback tags.
* **Model metadata:** `manifest_update_url` in `installer_config.json` is reserved for fetching a signed newer
  manifest; not enabled until a signing key exists.
* **Models:** never replaced automatically; new ones are offered, the user chooses.

## Building the Windows installer

On Windows with Inno Setup 6: `ISCC packaging\ISHA_Setup.iss` → `dist\ISHA_Setup.exe` (~1 MB; models are
downloaded, not bundled). For an **offline full installer**, run `packaging/build_offline_bundle.py` and
uncomment the `offline\*` line in the `.iss`. Code-sign the resulting exe for SmartScreen reputation.
