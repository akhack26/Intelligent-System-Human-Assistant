# Changelog

## 0.6.1 — agent-scripts architecture integration (skill loader, project map)

### Added
* **`isha_core/skill_loader.py`**: dynamic discovery of user skills under `~/.isha/skills/` (or
  `$XDG_DATA_HOME/isha/skills/`) with optional `skill.json` + `SKILL.md` front matter, validation,
  and safe disable of broken skills (inspired by agent-scripts’ skill layout; no Codex/Claude deps).
* **`isha_core/project_map.py`**: read-only project structure mapper (languages, entry points, key
  files, prompt-sized summary) for the coding agent; used by the `inspect_project` tool.
* **System & Storage** skill pack (`skill_packs/system.md`) for disk/process/hardware inspection
  workflows with delete-only-after-confirm guidance.
* **`INTEGRATION.md`**: full audit matrix of agent-scripts vs ISHA and what was adapted vs skipped.
* Tests: `tests/test_skill_loader.py`.

### Changed
* `inspect_project` returns a richer structure map when `project_map` is available.
* Coding skill pack stresses inspect-before-edit, minimal diffs, and Git awareness.

## 0.6.0 — skills, visuals, personalization, projects, model picker

### Added
* **Autonomous mode** (`autonomy_level: autonomous`, Settings → Permissions or Agent Center): safe and
  confirm-tier actions run without asking; critical actions and messages to people still ask. The
  confirmation box gains **"Hamesha allow"** (adds the tool to `trusted_tools`; Clear in Settings).
* **About ISHA** details, model name **Isha_v.20**; Quick Settings trimmed to 9 tiles.
* Plain knowledge questions go straight to chat; router JSON (`chat_reply`) is never shown.
* **Three model slots + picker** (main bar and Agent Center): Auto or Model 1/2/3; manual pick overrides
  routing (also for internal model calls); Auto routes among the slots by role with the existing RAM
  check and LRU residency; no auto-discovery of a fourth model; honest missing/fallback notes.
  The models the legacy rules resolve (`models.*`, `gguf_model_path`, `code_model_path`, models/<role>/
  folders) migrate into the slots in memory; nothing is written until you save.
* **Skills & Visuals** window: 11 toggleable offline skill packs (5 requested + 6 optional), matched
  per request (max two), shown in Agent Center / activity log; Artifacts and Inline visualizations
  switches.
* **Artifacts panel**: preview (sandboxed embedded browser if PyQtWebEngine is installed, safe text
  preview otherwise), source, copy, save as, reopen saved, close; auto-saved with unique names to
  `isha_artifacts/` and the active project.
* **Chat View** with native inline charts (bar, line, pie), flowcharts, timelines and comparison tables,
  hover values, "show as table", number grounding against real data.
* **Appearance & Personalization** settings: name, work category, response language (prompt + TTS voice +
  STT), user-wide instructions, response effort (local token/step budget), Thinking Mode with a real
  effect, speech speed (Slow/Normal/Fast).
* **Projects**: create/select/rename/delete (with confirmation), project instructions, attached files
  (bounded relevant excerpts; unsupported/binary files listed, never run), several chats per project,
  back to normal chat.

### Changed
* Deleting: "X folder banao aur usa folder ko 5 second me delete karo" now really deletes the folder the
  first step created, after the delay, through the normal approval dialog. A step that asks for an
  action but matches no tool is reported as not done instead of being "answered" by the model.
  New **Settings → Permissions → Delete to Recycle Bin without asking** (off by default).
* Quick Settings: the Stop tile is gone (main-bar Stop, Agent Center STOP and Esc remain); new tiles:
  Projects, Chat View, Artifacts, Skills & Visuals, Personalize.
* Chat and agent prompts are built by one function (no duplicated blocks); the router runs before the
  prompt so the runtime context names the model actually used.
* Settings window emits only its own keys (no more overwriting settings changed elsewhere); config and
  history files are written atomically.
* Speech: Cyrillic/Japanese text is no longer dropped as "unpronounceable"; Japanese sentence ends
  stream; `<think>` blocks and code fences are filtered out of streamed speech.

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
