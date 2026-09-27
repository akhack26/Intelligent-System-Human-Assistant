# ISHA — local-first JARVIS-style desktop assistant

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

**Normal users — use the installer** (no manual Python / pip / llama.cpp / model setup):

* **Windows:** double-click `ISHA_Setup.cmd` (or `ISHA_Setup.exe`).
* **Linux:** `./install.sh`

It checks your PC, installs Python if needed (official, signature-checked, per-user), creates ISHA's private
runtime, installs only the packages for the features you pick, sets up llama.cpp (Vulkan GPU build when
possible, CPU otherwise), downloads a model sized for your RAM with pause/resume and SHA-256 verification,
configures ISHA, creates Start Menu / app-menu shortcuts and an uninstaller. Details: [INSTALL.md](INSTALL.md),
problems: [TROUBLESHOOTING.md](TROUBLESHOOTING.md), internals: [INSTALLER.md](INSTALLER.md).

**Developers — run from source:**

1. Python 3.10–3.13. Keep `mani.py`, `isha_core/` and `installer/` together.
2. Put a GGUF model in `models/general/` (see [MODELS.md](MODELS.md)).
3. `python mani.py` — on first launch ISHA installs its packages into `.isha_runtime/` and sets up llama.cpp
   without compiling anything. If no `isha_config.json` exists yet, the first-run wizard opens
   (set `ISHA_NO_FIRST_RUN=1` to skip it).

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
| Models | "coding model use karo", "kaun sa model chal raha hai?" |
| Vision | "Screen par kya hai?" (needs a vision model; otherwise OCR text only) |

## Controls

* **Stop**: the ■ button appears in the bar while ISHA works; **Esc** does the same. It stops the agent
  loop, generation, queued tools and running child processes.
* **Quick Settings → Agent Center**: model mode (Auto/General/Coding/Reasoning/Study/Fast/Vision),
  autonomy level, live status (backend, active model, task, tool, step, CPU/RAM/GPU, index), file-index
  controls, self-check and the activity log.

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
| Install asks for a password (Linux) | That is the system's own polkit prompt via `pkexec`; ISHA never sees it. |

See also: [INSTALL.md](INSTALL.md), [INSTALLER.md](INSTALLER.md), [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
