# ISHA — local-first AGI-style desktop assistant

ISHA is a PyQt5 desktop assistant whose **brain is a local GGUF model** (llama.cpp) and whose **hands are
94 validated, permission-gated tools** that act on your PC: apps, files, search, system info, software
installs, coding projects, WhatsApp, memory and vision. It understands Hindi, Hinglish and English.

```
User ─► ISHA UI (text / voice) ─► Orchestrator ─► Model router ─► Local GGUF models
                                      │                               (general / coding / reasoning /
                                      ▼                                study / fast / vision)
                              Permission gateway ─► Tools ─► observe ─► replan ─► reply
```

Ordinary conversation, reasoning, coding, file search, file management, system information and PC
control need **no internet**. Internet is used only for things that inherently need it (opening
websites, web search, WhatsApp Web, downloading software/models).

## Quick start

1. **Python 3.10+** (Windows 10/11 or Linux). Put these files in one folder:
   `mani.py`, `isha_core/`, (optional) `icon/`.
2. **Get a model** (see [MODELS.md](MODELS.md)). Minimum: one instruct GGUF, e.g.
   `models/general/Qwen2.5-3B-Instruct-Q4_K_M.gguf`.
3. **Run:**
   ```bash
   python mani.py
   ```
   On first launch ISHA installs PyQt5 and optional packages into `.isha_runtime/` and sets up the
   llama.cpp backend *without compiling anything* (prebuilt wheel, else the official prebuilt
   `llama-server` binary). This one-time setup needs internet; afterwards it runs offline.
4. The startup self-check prints every component as **READY / DEGRADED / UNAVAILABLE**.

Optional extras: `pip install -r requirements.txt` (TTS, microphone, WhatsApp automation, OCR…).

## Things to say

| Area | Examples |
|---|---|
| PC control | "Chrome kholo", "Notepad band karo", "Volume 50 percent kar do", "WiFi status batao" |
| System | "Kitni RAM use ho rahi hai?", "Disk mein kitni space hai?", "System information batao" |
| File search | "Downloads mein PDF dhundo", "Saari MP4 files dhundo", "10 MB se badi images dhundo", "Kal modified files dhundo", "meri last wali photo" |
| Files | "Desktop par Test naam ka folder banao", then "Is file ko Documents mein move karo" |
| Cleanup | "Recycle Bin clean karo" (asks first, verifies after) |
| Software | "VS Code download karo", "Firefox uninstall karo" (official package managers only) |
| Coding | "Desktop par TestApp naam ka Python project banao", "TestApp run karo", "Is project ko run karke error fix karo" |
| Messaging | "Rahul ko WhatsApp karo ki main 10 minute late aaunga" (shows preview, sends after "Yes") |
| Memory | "Yaad rakho ki mera favourite editor VS Code hai", "memory dikhao", "bhool jao editor" |
| Models | "model 2 use karo", "auto model use karo", "coding model use karo", "kaun sa model chal raha hai?" |
| Skills | "Use the Finance skill: reconcile these two lists …", "/learn how does compound interest work" |
| Visuals | "Sales Jan 1200, Feb 1350, Mar 1100 - bar chart banao", "is process ka flowchart dikhao" |
| Vision | "Screen par kya hai?" (needs a vision model; otherwise OCR text only) |

## New in 0.6

* **Model picker** — keep three GGUF models in *Settings → Models*; pick Auto or Model 1/2/3 in the main
  bar. A manual pick always wins; Auto routes among your slots by task. See [MODELS.md](MODELS.md).
* **Skills** — 11 offline instruction packs (Superpowers, Learn, Web Artifacts Builder, Marketing,
  Finance, Coding & Debugging, Research & Fact Checking, Writing & Communication, Data Analysis &
  Spreadsheets, Productivity & Project Planning, Privacy & Security Review), each switchable in
  *Quick Settings → Skills & Visuals*. Only enabled skills that match the request are used (max two),
  and Agent Center shows which ones. These are ISHA's own local packs, not Claude Code plugins.
* **Artifacts** — generated web pages, code files and documents open in a resizable panel with
  preview, copy, save-as, reopen and close; saved under `isha_artifacts/` (and the project) without ever
  overwriting a file. HTML previews run sandboxed (no network, no file access); the embedded browser
  (`pip install PyQtWebEngine`) is optional — without it you get a script-free preview + source.
* **Inline visualizations** — bar/line/pie charts, flowcharts, timelines and comparison tables drawn
  natively in the **Chat View**, only from numbers in your messages, project files or tool results;
  anything else is flagged, and every visual has a text/table fallback.
* **Personalization** — *Quick Settings → Personalize*: your name, work, response language (Hindi,
  English, Russian, Japanese, Spanish, French, English (India) — also used for voice and speech
  recognition), instructions for ISHA, response effort (Low…Max = local token/step budget), Thinking
  Mode and speech speed.
* **Projects** — *Quick Settings → Projects*: local projects with their own instructions, attached files
  (read as data in small relevant excerpts, never run) and several chats each. "Back to normal chat"
  returns to the day-by-day history.

## Controls

* **Stop**: press **Esc** (with the ISHA bar focused) or use the STOP button in Agent Center. It stops
  the agent loop, generation, queued tools and running child processes. (The round ■ button in the bar
  and the Stop tile in Quick Settings were removed; the tile's place is now **Projects**.)
* **Model picker** (main bar): Auto / Model 1 / Model 2 / Model 3 / Configure models…; hover it for the
  model actually used last and any fallback note.
* **Quick Settings → Agent Center**: model picker, role routing (Auto/General/Coding/Reasoning/Study/
  Fast/Vision), autonomy level, live status (backend, active model and slot, task, tool, step, active
  skills, project, CPU/RAM/GPU, index), file-index controls, self-check and the activity log.
* **Quick Settings → Chat View / Artifacts / Skills & Visuals / Personalize / Projects** open the new
  windows (all resizable).

## Documentation

[ARCHITECTURE](ARCHITECTURE.md) · [TOOLS](TOOLS.md) · [MODELS](MODELS.md) ·
[CONFIGURATION](CONFIGURATION.md) · [SECURITY](SECURITY.md) · [DEVELOPMENT](DEVELOPMENT.md) ·
[TESTING](TESTING.md)

## Troubleshooting

| Symptom | Fix |
|---|---|
| "GGUF model not found" | Put a `.gguf` in `models/` or `models/general/`, or set `models.general` in `isha_config.json`. |
| Backend UNAVAILABLE | Needs internet once to fetch the prebuilt llama.cpp. Set `ISHA_LLAMA_BACKEND=server` to force the binary. |
| Model writes plans instead of acting | Use a tool-calling instruct model (Qwen2.5-Instruct 3B/7B). Avoid reasoning/base models. |
| No voice | `pip install edge-tts pygame` (online voice) or `pyttsx3` (offline). |
| WhatsApp won't send | `pip install pyautogui`; save the number: "Rahul ka number 98xxxxxxxx save karo". Log in to WhatsApp Web once. |
| File search slow on first use | The index builds in the background ~20 s after launch; until then a bounded direct scan is used. |
| Picker says "(missing)" | The slot's file moved: fix the path in Settings → Models (Auto is used meanwhile). |
| Artifact preview has no scripts | Optional: `pip install PyQtWebEngine` for the sandboxed embedded browser. |
| Reply voice doesn't speak my language | Settings → Voice & Speech shows the voice used; offline fallback needs a system voice for that language. |
| Install asks for a password (Linux) | That is the system's own polkit prompt via `pkexec`; ISHA never sees it. |
