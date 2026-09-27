# Configuration

Settings live in `isha_config.json` next to `mani.py` (created on first save). The file is **flat** so
every config written by older ISHA builds keeps loading; new keys are merged in with defaults and your
existing values are never overwritten. User paths are never hard-coded: Desktop, Downloads, etc. are
discovered from the OS (Windows Known Folders incl. OneDrive, Linux XDG user dirs).

Environment variables: `ISHA_DATA_DIR` (where index/audit/memory files go; default project folder),
`ISHA_LLAMA_BACKEND` (`native` / `server`), `ISHA_LLAMA_VARIANT`, `ISHA_SKIP_BACKEND=1` (tests / no model).

## Keys by category

### models
| Key | Default | Meaning |
|---|---|---|
| `models` | all `""` | per-role GGUF paths: general, coding, reasoning, study, fast, vision |
| `gguf_model_path` | `""` | legacy general model (still honoured) |
| `code_model_path` | `""` | legacy coding model (still honoured) |
| `context_length` | 4096 | llama.cpp context |
| `gpu_layers` | 0 | layers offloaded to GPU |
| `cpu_threads` | 0 | 0 = auto |
| `max_loaded_models` | 1 | models kept resident (capped by RAM) |
| `preload_model` | true | load at startup |

### routing
| Key | Default | Meaning |
|---|---|---|
| `model_mode` | `auto` | `auto` or a fixed role |
| `model_routing` | true | enable task-based routing in auto mode |
| `hardware_aware_loading` | true | skip models that don't fit free memory |
| `agent_mode` | true | use the plan→act→observe loop |
| `agent_max_steps` | 8 | hard cap on tool calls per request |

### permissions / security
| Key | Default | Meaning |
|---|---|---|
| `autonomy_level` | `high` | `low` / `medium` / `high` (see SECURITY.md) |
| `confirm_risky_tools` | true | legacy switch; `false` auto-approves *confirm* tier only |
| `trusted_messaging` | false | send WhatsApp without the confirmation dialog |
| `trusted_tools` | `[]` | confirm-tier tools you pre-approve |
| `blocked_tools` | `[]` | tools that must never run |

### automation / coding
| Key | Default | Meaning |
|---|---|---|
| `max_auto_fix_attempts` | 3 | run+fix attempts (hard cap 5) |
| `code_run_timeout` | 20 | seconds per verification run |
| `preferred_package_manager` | `""` | e.g. `winget`, `flatpak` |
| `fast_tools` | true | reply with the tool's own result for deterministic commands (no extra LLM call) |

### search
| Key | Default | Meaning |
|---|---|---|
| `file_index_enabled` | true | local SQLite index |
| `file_index_roots` | `[]` | folders to index (`[]` = home) |
| `file_index_excludes` | see `file_index.DEFAULT_EXCLUDES` | folder names/paths never entered |
| `file_index_autostart_delay` | 20 | seconds after launch before indexing |

### messaging
`whatsapp_mode` (`auto`/`desktop`/`web`), `whatsapp_country_code` (`91`), `whatsapp_desktop_wait`,
`whatsapp_web_wait`, `whatsapp_autosend_by_name` — unchanged from the previous build.

### voice / performance / ui
`tts_enabled`, `tts_voice`, `tts_rate`, `stream_tts`, `streaming`, `perf_metrics`, `temperature`,
`top_p`, `top_k`, `max_tokens`, `history_turns`, `show_agent_panel` (open Agent Center at launch),
`audit_log_enabled` (write `isha_audit.jsonl`; the in-memory log always exists).

## Example

```json
{
  "models": {"general": "models/general/qwen2.5-3b-instruct-q4_k_m.gguf",
             "coding": "models/coding/qwen2.5-coder-7b-instruct-q4_k_m.gguf"},
  "model_mode": "auto",
  "autonomy_level": "high",
  "trusted_messaging": false,
  "max_auto_fix_attempts": 3,
  "file_index_roots": ["C:/Users/me", "D:/Projects"],
  "preferred_package_manager": "winget"
}
```

## Local files ISHA writes

| File | Content |
|---|---|
| `isha_config.json` | settings |
| `isha_chat_history.json` | day-by-day chat history (existing) |
| `isha_memory/` | vector memory (existing) |
| `isha_long_memory.json` | facts you asked ISHA to remember |
| `isha_file_index.db` | file index (paths, names, sizes, dates — no file contents) |
| `isha_audit.jsonl` | action log (secrets redacted) |
| `whatsapp_contacts.json` | saved WhatsApp numbers (existing) |

All stay on your machine.
