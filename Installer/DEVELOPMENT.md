# Development Guide

## Layout

```
ISHA/
├── mani.py            entry point: GUI, backend, tool registry, agent loop (single file, preserved)
├── isha_core/         headless subsystems (no PyQt, no model) — see ARCHITECTURE.md
├── tests/             pytest suite (headless, offscreen Qt)
├── models/            your GGUF files (models/<role>/*.gguf)
└── *.md               documentation
```

`mani.py` imports `isha_core` right after the Qt imports. Keep `isha_core` free of PyQt and of
`mani.py` imports so it stays testable.

## Adding a new tool

1. Write the handler in `mani.py` (or put the logic in `isha_core/` and a thin wrapper in `mani.py`):

```python
@tool("count_words", "Count words in a text file.",
      {"file_path": {"type": "string", "required": True, "description": "Path to the file."}})
def _count_words(args: dict) -> str:
    p = _safe_resolve_path(args["file_path"])          # 'desktop/x.txt' etc. resolved via the OS
    if not p.is_file():
        raise ToolError(f"File nahi mili: {p}")          # failure = raise ToolError, never fake success
    return f"{p.name}: {len(p.read_text(errors='replace').split())} words"
```

2. Classify it: `TOOL_RISK["count_words"] = "safe"` (unlisted tools default to *confirm*). If it only
   reads, add it to `READ_ONLY_TOOLS` in `isha_core/permissions.py`.
3. Add it to a category in `TOOL_CATEGORY` (the orchestration block) and, if useful, aliases in
   `TOOL_ALIASES`.
4. Optional deterministic phrase: add a branch to `match_agent_command()`.
5. Optional approval preview: add a case to `_describe_special()`.
6. Add a test (see TESTING.md) and regenerate `TOOLS.md` (script in TESTING.md).

Rules: return a human sentence on success; raise `ToolError` on failure; verify effects (re-read the file,
re-query the state) before claiming success; long loops call `ISHA_STOP.check()`; child processes go
through `isha_core.commands.run_command` or `ISHA_STOP.track(proc)`.

## Adding a new model

See [MODELS.md](MODELS.md#adding-a-new-model-role).

## Adding a new OS integration

Put platform code behind `isha_core/system.py`:

```python
class SystemController:              # interface
    def trash_status(self): ...
    def empty_trash(self): ...
class LinuxSystemController(SystemController): ...
class WindowsSystemController(SystemController): ...
class MacSystemController(SystemController): ...
```

Add the method to the base class (returning an honest "not supported" result), implement it per OS,
and call `_SYSTEM_CONTROLLER.<method>()` from a tool. Standard folders come from
`isha_core.paths.known_folder()`; package managers from `isha_core.software`.

## Adding a software package

Add aliases to `ALIASES` and ids per manager to `CATALOG` in `isha_core/software.py` (official ids only),
plus `_bin` names used for verification.

## Installer

* New Python dependency: add it to `_OPTIONAL_PACKAGES` in `mani.py` **and** to a group in
  `installer/installer_config.json` (+ `import_names`). `test_installer_groups_cover_every_package_mani_uses`
  fails otherwise.
* New feature toggle: add it under `features` (with its dependency groups) and list its tools under
  `feature_tools` so disabling it really blocks them.
* New or updated model: edit `installer/models_manifest.json` (or ship `models_manifest.local.json`); no code
  change. Pin `sha256` when you want a specific file.
* New files that must be installed: add them to `app_files`.
* Run `pytest tests/test_installer.py` and, on a real machine, `python -m installer --headless --accept-terms
  --dir /tmp/isha-test --preset minimal` followed by `--diagnose --deep`.

## Conventions

* Tool results are short Hinglish/English sentences — they are spoken aloud.
* Never hard-code user paths; never print secrets; keep `execute_tool()` the only place a tool runs.
* `mani.py` ships with CRLF line endings (Windows); both work.
