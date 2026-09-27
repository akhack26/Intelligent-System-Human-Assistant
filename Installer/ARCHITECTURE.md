# ISHA Architecture

## Decision: incremental, not a rewrite

`mani.py` (≈11.5k lines before this upgrade) already contained a working GUI, llama.cpp backend, tool
registry, risk tiers, agent loop, WhatsApp and TTS/STT. Splitting it into the suggested `core/ models/
tools/ ...` tree in one step would have broken working code for no user-visible gain. So:

* **`mani.py` stays the entry point** and keeps every existing class, function and tool (68 original
  tools, all still registered).
* **New subsystems live in `isha_core/`**, a headless package (no PyQt, no model loading) so each piece is
  unit-tested on its own. `mani.py` wires them into its existing registry, gateway, workers and GUI.
* Future refactors can move more of `mani.py` into `isha_core/` one subsystem at a time.

## Request flow

```
text / voice ─► ISHAWindow._send_to_llm
                  │  STOP.reset, audit "request", classify task
                  ▼
            plan_request()  ── split compound command ("aur", "phir", ",") into segments
                  │
                  ├─ match_task(segment)      deterministic, no LLM:
                  │    WhatsApp parser → match_agent_command (memory, file search, software,
                  │    projects, run/fix, model control, system info, pronoun follow-ups)
                  │    → YouTube / URL / web search / site / close app → match_quick_command
                  │
                  ├─ all segments known, 1 step  → _execute_tool_and_reply   (no LLM at all)
                  ├─ several steps               → PlanExecutorWorker        (LLM only for unknown parts)
                  ├─ small talk                  → streaming chat completion
                  └─ anything else               → AgentWorker (PLAN → ACT → OBSERVE → REPLAN loop)
                                                      │
               pick_cfg_for(text) ◄───────────────────┤  model router picks the GGUF
                                                      ▼
                                   run_tool_gated(name, args, ask)
                                     resolve name → risk tier → permissions.decide()
                                     → ask user (dialog) if needed → execute_tool()
                                     → audit log → working memory → observation back to the model
```

## Architecture map (existing code → category)

| # | Category | Where |
|---|---|---|
| 1 | Entry point | `main()` at the bottom of `mani.py`; `_ensure_environment()` bootstrap at import |
| 2 | GUI | `ISHAWindow`, `QuickSettingsPopup`, `ConfigWindow`, `HistoryWindow`, `CommDockWindow`, **new `AgentPanelWindow`** |
| 3 | Configuration | `DEFAULT_CONFIG`, `load_config/save_config/_migrate_config` + **`isha_core/config.py`** (new keys merged) |
| 4 | Model backend | `resolve_backend()` (native wheel → prebuilt wheel → `LlamaServer`) |
| 5 | Model loading | `GGUFModelManager` (`GGUF`), LRU residency, `find_gguf_models`, `resolve_gguf_path` |
| 6 | Model routing | **`pick_cfg_for()` → `isha_core/router.py`** (was: coding-only regex) |
| 7 | Agent loop | `AgentWorker._loop`, `PlanExecutorWorker`, `plan_request`, `looks_like_fake_action` |
| 8 | Tool registry | `@tool`, `TOOL_REGISTRY`, `TOOL_CATEGORY`, `TOOL_ALIASES`, `resolve_tool_name`, `list_capabilities` |
| 9 | Tool gateway | `execute_tool()` (allow-list + schema validation) + **`run_tool_gated()`** |
| 10 | Risk system | `TOOL_RISK`, `tool_risk()` + **`isha_core/permissions.py`** (autonomy levels) |
| 11 | File search | `FileMediaManager.deep_search`, `search_files` + **`isha_core/file_index.py`, `nl_search.py`** |
| 12 | Coding tools | `create_and_save_code`, `run_code_file`, `autonomous_code_agent` + **`isha_core/coding.py`** |
| 13 | WhatsApp | `parse_whatsapp_command`, `send_whatsapp_message`, contacts JSON (unchanged, preview added) |
| 14 | Memory | `VectorMemoryEngine`, chat history + **`isha_core/memory.py`** (long-term + working) |
| 15 | TTS | `TTSWorker`, `SentenceStreamer` (unchanged) |
| 16 | STT | `MicListenerThread` (unchanged) |
| 17 | Vision | none before → **`isha_core/vision.py`** (local multimodal GGUF or OCR) |
| 18 | OS control | `WindowsController` (cross-platform despite the name), volume/brightness helpers + **`isha_core/system.py`** |
| 19 | Diagnostics | `run_self_diagnostics` + **`_component_status()`** (READY/DEGRADED/UNAVAILABLE) |
| 20 | Background workers | `GGUFWorker`, `AgentWorker`, `OSTaskWorker`, `SystemStatsMonitor`, `TTSWorker`, hand workers, **file indexer thread** |

## `isha_core/` modules

| Module | Responsibility |
|---|---|
| `router.py` | `classify_task()` (14 task types, Hinglish aware), `ModelRegistry` (roles → files), `ModelRouter` (fallback chain, manual override, RAM check) |
| `hardware.py` | CPU/RAM/GPU/VRAM/disk probe; `model_fits`, `recommend_resident_models` |
| `permissions.py` | risk tier × autonomy × trust flags → allow / ask / deny |
| `audit.py` | action log (ring buffer + JSONL), secret redaction, GUI subscriptions |
| `control.py` | global STOP: flag + tracked child processes |
| `file_index.py` | SQLite index, incremental updates, exclusions, pause/rebuild, fallback scan |
| `nl_search.py` | natural language → `SearchQuery` candidates; `FileSearchEngine` |
| `commands.py` | structured terminal commands: tokenize, blocklist, shell detection, timeout |
| `software.py` | install/uninstall/update via winget/choco/scoop/apt/dnf/pacman/zypper/flatpak/snap/brew + verification |
| `system.py` | `SystemController` → Windows/Linux/Mac: Recycle Bin status + verified empty, volume read |
| `coding.py` | project templates, safe runner, bounded fix loop with risk-delta check |
| `memory.py` | `LongTermMemory`, `WorkingMemory`, memory-command parser |
| `vision.py` | vision capability status, multimodal GGUF call, OCR |
| `paths.py` | OS known-folder discovery (Windows Known Folders, XDG user dirs, OneDrive) |
| `config.py` | new config defaults and category map |

## Why deterministic first

Small local models (1.5–7B) are unreliable planners. Everything that can be recognized without a model
(open/close apps, file search, system info, projects, memory, WhatsApp, software) is matched by
`plan_request()` and executed directly — faster, and it can never hallucinate success. The LLM handles
open-ended requests through the agent loop, where every claim must be backed by a tool result.
