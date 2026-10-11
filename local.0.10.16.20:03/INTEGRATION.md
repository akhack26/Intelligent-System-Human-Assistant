# ISHA × agent-scripts integration notes

This document records the audit of **agent-scripts-main** against the existing
**ISHA** codebase and the concrete changes made. ISHA was **not** rewritten;
agent-scripts concepts were adapted only where they strengthen the local,
permission-gated architecture.

## 1. ISHA audit (existing project)

| Area | Finding |
|------|---------|
| Language / entry | Python 3.10+, `mani.py` (~16k lines) is the GUI entry point |
| GUI | PyQt5 (`ISHAWindow`, settings, skills, projects, artifacts, agent panel) |
| Models | Local GGUF via llama.cpp (native wheel → prebuilt → `LlamaServer`). Multi-slot + `ModelRouter` |
| Ollama | Explicitly dropped in config migration; not a core backend |
| Tools | `@tool` decorator, `TOOL_REGISTRY`, risk tiers, `run_tool_gated` |
| Permissions | `isha_core/permissions.py` — allow / ask / deny by risk × autonomy |
| Agent loop | `AgentWorker` PLAN→ACT→OBSERVE; deterministic `plan_request` first |
| Skills | Instruction packs in `isha_core/skill_packs/*.md` + static `REGISTRY` in `skills.py` |
| Memory | Long-term + working memory, chat history, vector memory |
| Voice | TTSWorker / MicListenerThread (modular, optional deps) |
| OS | Cross-platform helpers; Linux package managers + Trash supported |
| Tests | `tests/` for router, permissions, commands, coding, memory, integration |

**Architecture principle already present:** LLM proposes; controlled runtime decides.

## 2. agent-scripts audit

| Component | Purpose | ISHA usefulness | Decision | Reason |
|-----------|---------|-----------------|----------|--------|
| `SKILL.md` + YAML front matter | Machine routing metadata + human instructions | High | **Adapt** | Native `skill_loader.py` + optional front matter; keep existing REGISTRY |
| `scripts/validate-skills` | Validate front matter / secrets | Medium | **Adapt** | Validation inside `SkillLoader` (no Ruby) |
| `scripts/sync-skills` | Symlink skills into Codex/Claude dirs | Low | **Skip** | ISHA does not use Codex/Claude skill paths |
| `skills/browser-use` | Control signed-in Chrome via DevTools / plugins | Medium (concept) | **Concept only** | Platform/plugin-specific; ISHA keeps `webbrowser` + optional WebEngine for artifacts |
| `skills/project-structure` | Dense TS/Swift symbol maps | High | **Adapt** | `isha_core/project_map.py` (Python, read-only) |
| `skills/codex-debugging` | Codex-rs source debugging | None | **Skip** | Codex-specific |
| `skills/codex-huge-context` | Huge context workflows | Low | **Skip** | Tied to Codex / large API windows |
| `skills/maintainer-orchestrator` | Multi-agent handoff for repo maintenance | Medium (concept) | **Concept only** | ISHA prefers one orchestrator + logical modes, not many resident agents |
| `skills/github-*` | GH triage, review, cache | Low | **Skip** | Requires `gh` auth / cloud; optional later |
| `skills/frontend-design` | UI design guidance | Low | **Skip** | Overlaps existing web_artifacts skill |
| `skills/markdown-converter` | MD conversion | Low | **Skip** | Not core OS assistant |
| `skills/openai-image-gen` | OpenAI image API | None | **Skip** | Cloud dependency |
| `skills/obsidian` | Obsidian vault | Low | **Skip** | User-specific |
| `skills/mac-maintenance`, SwiftUI*, Xcode* | macOS / Apple tooling | None | **Skip** | Target OS is Zorin/Linux first |
| `skills/npm`, fleet-*, DNS, Twilio, Discord, WhatsApp variants | SaaS / infra | None / overlap | **Skip** | Not local-first; ISHA already has WhatsApp tools |
| `AGENTS.MD` hard rules | Agent safety / process rules | Medium | **Concept only** | Many rules already encoded in ISHA system prompt + permissions |
| License | Check before copy | — | **Reimplement ideas** | Prefer original ISHA code; no bulk file copy |

**License:** agent-scripts is a personal tooling repo; ideas were reimplemented rather than copied as source to avoid license/attribution ambiguity and platform coupling.

## 3. What was implemented

### New modules
- `isha_core/skill_loader.py` — discover/validate user skills under `~/.isha/skills/` (or `$XDG_DATA_HOME/isha/skills/`); supports `skill.json` + optional `SKILL.md` front matter; broken skills disable without crashing.
- `isha_core/project_map.py` — read-only project structure map (languages, entry points, key files, prompt-sized summary).

### Modified
- `isha_core/skills.py` — added **System & Storage** skill to `REGISTRY`.
- `isha_core/skill_packs/system.md` — new instruction pack.
- `isha_core/skill_packs/coding.md` — stronger “inspect before edit / minimal change / Git” guidance.
- `mani.py` — `inspect_project` tool uses `project_map` when available (fallback to previous behaviour).

### Tests
- `tests/test_skill_loader.py` — front matter, discovery, validation, system skill match, project map.

## 4. What was intentionally not done

- No dependency on Bun, Codex CLI, Claude Code, OpenAI APIs, or macOS-only tools.
- No replacement of ISHA’s skill packs with agent-scripts skills.
- No multi-LLM “agent swarm” that loads several models at once.
- No unrestricted shell; command safety remains in `isha_core/commands.py`.
- Ollama was not re-added as a required backend (GGUF/llama.cpp remains primary). A future `ModelProvider` abstraction can wrap additional backends without rewriting the GUI.

## 5. How to add a user skill

```text
~/.isha/skills/my_helper/
  skill.json          # machine metadata (recommended)
  SKILL.md            # optional: YAML front matter + Markdown instructions
```

Example `skill.json`:

```json
{
  "id": "my_helper",
  "name": "My Helper",
  "description": "Short trigger-friendly description",
  "category": "productivity",
  "version": "1.0",
  "triggers": ["my helper", "helper mode"],
  "tasks": ["PLANNING"],
  "risk_level": "safe",
  "enabled": true
}
```

Skills cannot execute arbitrary code unless you also register a gated tool in ISHA; instruction packs only affect the system prompt after relevance matching.

## 6. How to add a tool

1. Define with `@tool(name, description, schema)` in `mani.py` (or a future tools package).
2. Assign a risk in `TOOL_RISK` (`safe` / `confirm` / `critical`).
3. Implementation returns structured text/dict; raise `ToolError` on failure.
4. Never skip `run_tool_gated` / permissions.

## 7. Permissions (unchanged core)

- **safe** — usually auto-allowed (especially read-only tools).
- **confirm** — ask unless autonomy / trust flags say otherwise.
- **critical** — always ask (or deny if blocked).
- Blocked tools and dangerous shell patterns are refused regardless of model output.

## 8. Known limitations

- User skill **prompt injection** for discovered skills is not yet wired into `_compose_system_prompt` (loader is ready; GUI/settings integration is the next step).
- Full browser automation (click/type/DOM) is not implemented; only open URL + artifact WebEngine preview.
- Multi-backend `ModelProvider` (Ollama + llama.cpp) is a planned abstraction, not a full second backend in this pass.
- Hardware VRAM detection quality depends on the host drivers/`psutil`/optional GPU libs.

## 9. Recommended next steps

1. GUI: Skills page → show discovered user skills + enable toggles.
2. Wire `SkillLoader` status into Agent Center / self-check.
3. Optional Playwright-based `BrowserProvider` behind a feature flag.
4. Thin `InferenceBackend` interface with GGUF as the first implementation.
5. Git tools (`git.status`, `git.diff`) as dedicated gated tools if not already sufficient via safe shell.
6. Graphical installer packaging (already sketched under `Installer/`).
