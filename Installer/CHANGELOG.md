# Changelog

## 0.5.0 — installer & setup system

* New `installer/` package: GUI wizard (Welcome, Terms, Privacy, System check, Location, Features, AI model,
  Install, Diagnostics, Finish), first-run wizard, uninstall dialog, headless CLI.
* Bootstrappers `ISHA_Setup.cmd`/`.ps1` (Windows, installs Python via winget or signature-verified python.org
  installer) and `install.sh` (Linux); `packaging/ISHA_Setup.iss` builds `ISHA_Setup.exe`.
* Resumable, HTTPS-only, SHA-256-verified model downloads with pause/resume/cancel, speed and ETA; model
  catalogue in `models_manifest.json`; hardware-based recommendation; storage check before downloading.
* llama.cpp resolver moved from `mani.py` to `isha_core/llama_runtime.py` (re-imported, unchanged behaviour)
  with persisted GPU variant, offline archives, safe extraction, CPU fallback, and a CLI. When a
  `llama-server` is already installed, launches no longer try `pip` first.
* Feature choices are enforced: disabled features skip their packages, turn off config switches and put
  their tools in `blocked_tools`, which are also hidden from the model's capability list. New `stt_enabled`.
* Fixed: `requirements.txt` lines with inline comments broke mani's first-launch fallback install.
* Shared visual theme `isha_core/theme.py` (installer + Agent Center).

## 0.5.0 — agent upgrade (builds on 0.4, nothing removed)

### Fixed (existing bugs)
* Every deterministic quick command ("Chrome kholo") showed a Qt `TypeError` as ISHA's reply:
  `OSTaskWorker.task_finished` was `pyqtSignal(dict)` but tools return strings.
* "Recycle Bin clean karo" opened the bin instead of emptying it.
* "Downloads mein PDF dhundo" became a Google search.
* Emptying the Recycle Bin/Trash reported success without checking.
* Desktop/Downloads were hard-coded as `~/Desktop` (wrong with OneDrive or localized Linux folders).
* An approved `delete_file_safely` / `system_power_action` still refused unless the model also sent `confirm=true`.
* `run_terminal_command` passed model text to `shell=True` with a weak blocklist.
* Import could trigger pip installs/downloads — now skippable with `ISHA_SKIP_BACKEND=1`.

### Added
* Multi-model router (general/coding/reasoning/study/fast/vision) with fallback, manual mode, RAM check.
* Permission gateway with autonomy levels, trusted messaging/tools, blocked tools; critical always asks.
* Action log (`isha_audit.jsonl`) with secret redaction; Agent Center window; visible Stop button.
* SQLite file index + natural-language file search (Hindi/Hinglish/English).
* Software install/uninstall/update via official package managers with verification.
* Coding agent: project scaffolding (Python/web), run + bounded auto-fix with risk-delta approval.
* Long-term memory (remember/forget/show), working memory for follow-ups and browser context.
* Vision capability (local multimodal GGUF or OCR), READY/DEGRADED/UNAVAILABLE diagnostics.
* 26 new tools (94 total), spec capability categories, 113 tests, full documentation.
