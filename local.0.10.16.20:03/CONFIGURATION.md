# Configuration

Settings live in `isha_config.json` next to `mani.py` (created on first save). The file is **flat** so
every config written by older ISHA builds keeps loading; new keys are merged in with defaults and your
existing values are never overwritten. User paths are never hard-coded: Desktop, Downloads, etc. are
discovered from the OS (Windows Known Folders incl. OneDrive, Linux XDG user dirs).

Environment variables: `ISHA_DATA_DIR` (where index/audit/memory/projects/artifacts go; default project folder),
`ISHA_NO_WEBENGINE=1` (never load the optional embedded browser for artifact previews),
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

### model picker (three slots)
| Key | Default | Meaning |
|---|---|---|
| `model_slots` | migrated from the keys above | exactly three `{name, path, roles}` entries; `roles` ⊆ general, coding, reasoning, study, fast, vision |
| `model_selection` | `auto` | `auto`, `slot1`, `slot2` or `slot3` — a manual slot wins over all routing |

Three-slot mode is active as soon as one slot has a path; routing then only ever uses the slots (no
auto-discovery of a fourth model). With all slots empty, the legacy keys above work exactly as before.
See [MODELS.md](MODELS.md#three-model-slots-the-model-picker).

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
| `autonomy_level` | `high` | `low` / `medium` / `high` / `autonomous` (see SECURITY.md) |
| `confirm_risky_tools` | true | legacy switch; `false` auto-approves *confirm* tier only |
| `trusted_messaging` | false | send WhatsApp without the confirmation dialog |
| `trusted_recycle_delete` | false | delete files/folders **to the Recycle Bin/Trash** without the dialog (Settings → Permissions). Needs `send2trash`; permanent deletes always ask |
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

### skills & visuals
| Key | Default | Meaning |
|---|---|---|
| `skills_enabled` | all `true` | `{skill_id: bool}` for superpowers, learn, web_artifacts, marketing, finance, coding, research, writing, data_analysis, productivity, privacy_security |
| `artifacts_enabled` | true | generated pages/code/documents open in the Artifacts panel and are saved to `isha_artifacts/` |
| `inline_visuals_enabled` | true | charts / flowcharts / timelines / comparison tables drawn in the Chat view |

An enabled skill is only added to the prompt when the request matches it (or you name it: "use the
Finance skill", "/finance"); at most two per request. A disabled skill never influences a reply.

### appearance / personalization
| Key | Default | Meaning |
|---|---|---|
| `user_name` | `""` | optional name ISHA may use naturally |
| `work_category` | `""` | `product_management`, `engineering`, `human_resources`, `finance`, `marketing`, `sales`, `operations`, `data_science`, `design`, `legal`, `scientist`, `student`, `founder`, `healthcare`, `writer`, `educator`, `software_engineer` (`""` = prefer not to say) |
| `response_language` | `""` | `""` = reply in the user's language (old behaviour), or `hi`, `en`, `ru`, `ja`, `es`, `fr`, `en-IN`; also picks the TTS voice and speech-recognition language |
| `user_instructions` | `""` | user-wide instructions for every chat on this install |
| `speech_speed` | *(derived from `tts_rate`)* | `slow` (-25%), `normal` (+0%), `fast` (+25%); written only when you change it, so a custom `tts_rate` keeps working |
| `response_effort` | `medium` | `low` / `medium` / `high` / `extra` / `max` → ×0.5 / ×1 / ×1.5 / ×2 / ×3 of `max_tokens` and `agent_max_steps` (reply tokens capped at 60% of `context_length`, steps at 40). Medium = your configured values |
| `thinking_mode` | false | on coding/debugging/reasoning/planning/research/study requests (or messages over 40 words): a "work carefully and privately, verify, answer briefly" instruction, ×1.5 reply tokens, +2 agent steps and chat temperature ≤ 0.6. `<think>` blocks are stripped from display and speech |

Prompt order (chat and agent paths use the same builder): ISHA identity + safety rules → runtime
context, `capability_note`, `system_prompt` → user preferences → project instructions and file
excerpts → active skills → visual/artifact format and Thinking Mode → memory. Project instructions
win over user-wide instructions; nothing overrides the safety rules or the permission gateway.

### projects
| Key | Default | Meaning |
|---|---|---|
| `active_project` | `""` | project id; `""` = normal chat (day-by-day history). A missing project falls back to normal chat |
| `active_project_chat` | `""` | chat inside that project; `""` = a new chat is created on the first message |

### voice / performance / ui
`tts_enabled`, `tts_voice`, `tts_rate` (see `speech_speed`), `stream_tts`, `streaming`, `perf_metrics`, `temperature`,
`top_p`, `top_k`, `max_tokens`, `history_turns`, `show_agent_panel` (open Agent Center at launch),
`audit_log_enabled` (write `isha_audit.jsonl`; the in-memory log always exists).

Settings are saved atomically (temporary file + rename). Loading an old `isha_config.json` does not
write the new keys or the migrated model slots back: they exist in memory until you press **Save** or
change a setting in the UI (the pre-existing clean-up of Ollama-era keys still saves as before). See `isha_config.example.json` for every new key.

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
| `isha_projects/<id>/` | `project.json` (name, instructions, file list), `files/` (copies of attached files), `chats/*.json`, `artifacts/` |
| `isha_artifacts/` | artifacts generated in any chat (unique names, never overwritten) |

All stay on your machine.
