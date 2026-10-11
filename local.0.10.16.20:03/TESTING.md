# Testing

```bash
pip install pytest PyQt5 psutil
python -m pytest tests -q
```

The suite is fully headless: `tests/conftest.py` points `HOME` at a temporary profile (with its own
Desktop/Downloads/Trash), sets `ISHA_SKIP_BACKEND=1` (no llama.cpp download), `QT_QPA_PLATFORM=offscreen`
and `ISHA_DATA_DIR` to a temp folder. **It never touches your real files, trash or settings.**

## What is covered (113 tests)

| File | Covers |
|---|---|
| `test_router.py` | task classification (15 Hinglish/English phrases), coding→coding / command→fast / reasoning→reasoning, study→reasoning fallback, manual override, vision never falls back, missing files not claimed, legacy config keys, low-RAM fallback |
| `test_permissions.py` | safe/confirm/critical × low/medium/high, trusted messaging, blocked & unknown tools, critical always asks |
| `test_file_search.py` | SQLite index (extension, media type, size, date, location, folders), exclusions, incremental add/remove, fallback scan, NL parser, engine candidates |
| `test_commands.py` | 13 blocked command patterns, shell detection, read-only classification, direct execution |
| `test_coding.py` | templates, calculator runs with test input, intentional error → detect → fix → rerun, syntax errors, bounded retries + hard cap, blocked code never executed, risky-fix approval, interactive programs, STOP, secret env scrubbing |
| `test_memory_software_system.py` | remember/forget/show + secret refusal, memory command parsing, working memory, package plans (winget / apt+pkexec / manual sudo), install verification, verified Linux trash emptying, audit redaction |
| `test_integration_mani.py` | the real `mani.py`: acceptance tests (below), WhatsApp parser variants + preview + no send on deny, audit log, approval-as-confirmation, agent loop (plan/act/observe, failure→retry, step budget, stop, denied tool), plan executor, OSTaskWorker regression, diagnostics, capability categories |
| `test_gui_smoke.py` | window + Agent Center + Stop button construct, update and close offscreen |

## Acceptance tests (spec §47) — status

| # | Test | Automated? | Result in CI sandbox (Linux, no model) |
|---|---|---|---|
| 1 | "Chrome kholo" opens Chrome | routing only | routes to `open_app(chrome)`; launching needs a desktop — **manual** |
| 2 | "Downloads mein PDF dhundo" returns files | ✅ | real files returned (index and scan) |
| 3 | "Desktop par TestApp naam ka Python project banao" | ✅ | folder + files created and verified |
| 4 | "TestApp run karo" | ✅ | runs after approval, output captured |
| 5 | intentional bug → fix → retry | ✅ (fake fixer) | detect/feed error/rewrite/rerun verified; real fixes need a coding GGUF — **manual** |
| 6 | "Rahul ko WhatsApp karo…" | parser + gate ✅ | contact/message parsed, preview + confirmation; actual sending needs WhatsApp — **manual** |
| 7 | "Recycle Bin clean karo" | ✅ | asks (confirm), denied = nothing deleted, approved = emptied + verified (Linux trash) |
| 8 | "RAM kitni use ho rahi hai?" | ✅ | real psutil numbers |
| 9 | reasoning → reasoning model | ✅ | |
| 10 | "Python ka program bana do" → coding model | ✅ | |

## Manual checklist on your PC

1. `python mani.py` — self-check lists components; Model backend/Model files READY once a GGUF is in `models/`.
2. "Chrome kholo", "Notepad band karo", "Volume 50 percent kar do".
3. "Recycle Bin clean karo" on Windows → dialog → bin empty (verified by `SHQueryRecycleBinW`).
4. "VS Code download karo" → dialog shows the winget/apt command → installs → verified.
5. Put a bug in `Desktop/TestApp/main.py`, say "TestApp run karo" with a coding model loaded.
6. "Rahul ka number 98xxxxxxxx save karo", then "Rahul ko WhatsApp karo ki test" → preview → Yes.
7. Press ■ / Esc during a long request — everything stops.

## Regenerating TOOLS.md

```bash
ISHA_SKIP_BACKEND=1 QT_QPA_PLATFORM=offscreen python tools_dev/gen_tools_md.py
```
