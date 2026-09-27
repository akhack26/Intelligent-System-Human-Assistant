# Changelog

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
