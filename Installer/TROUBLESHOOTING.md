# Troubleshooting ISHA installation

Run diagnostics any time: `python -m installer --diagnose --install-dir <ISHA folder>` (add `--deep` to load
the model). The installer can always be re-run: it resumes and repairs without touching your data.

| Symptom | Cause | Fix |
|---|---|---|
| Setup says **Python not found** although you installed it | Only the Microsoft Store alias exists, or Python is 32-bit / older than 3.10 | Let Setup install Python 3.12, or install 64-bit Python 3.12 from python.org. |
| **"could not create the virtual environment"** (Linux) | `venv` module missing | `sudo apt install python3-venv` (Debian/Ubuntu), then re-run. |
| **PyAudio failed** | PortAudio headers missing (Linux) or no wheel for your Python | `sudo apt install portaudio19-dev python3-dev`; only the microphone feature is affected. |
| **mediapipe failed** | No wheel for Python 3.13+ yet | Use Python 3.12, or ignore (only hand gestures are affected). |
| **Model download failed** | Connection interrupted, proxy, or Hugging Face blocked | Choose *Retry*: the download resumes from where it stopped. Or *Change model* / *Skip* and add a model later. |
| **Checksum mismatch** | Corrupted or tampered download | The file was deleted automatically; *Retry*. Repeated failures usually mean a proxy altering traffic. |
| **Not enough disk space** | Model + runtime + temp space exceeds free space | *Choose another location* (another drive) or a smaller model. |
| **llama.cpp could not be installed** | GitHub unreachable | *Retry*, or put the release archive in `offline/llamacpp/`. ISHA also retries on launch. |
| **GPU acceleration unavailable** | No NVIDIA/AMD GPU or Vulkan runtime missing | Not an error: ISHA uses CPU mode. Update your graphics driver, then delete `.isha_runtime/llamacpp` and run `python -m installer --headless --accept-terms --variant vulkan --preset none --dir <folder>`. |
| Model loads but is very slow | Model too big for RAM (swapping) | Pick a smaller tier in the first-run wizard (`python -m installer --first-run --install-dir <folder>`). |
| **Vision does not work** | moondream needs the native `llama-cpp-python` backend | Set `ISHA_LLAMA_BACKEND=native` if a wheel exists for your Python, otherwise vision reports unavailable. |
| No Start Menu / menu entry | Shortcut creation failed (logged as a warning) | Re-run Setup, or start `ISHA.cmd` (Windows) / `isha.sh` (Linux) in the ISHA folder. |
| Microphone button says disabled | Speech recognition switched off during setup | Re-run the first-run wizard, or set `"stt_enabled": true` in `isha_config.json`. |
| Setup window does not appear | PyQt5 could not be installed in the setup environment | Setup falls back to text mode automatically; or run `ISHA_Setup.ps1 -Headless` / `./install.sh --headless`. |
| PowerShell blocks the script | Execution policy | Use `ISHA_Setup.cmd` (it scopes `-ExecutionPolicy Bypass` to that one process). |
| Uninstaller refuses a folder | It has no `installer_state.json`, so it may not be ISHA | Intentional safety; delete manually if you are sure. |

Logs: `isha_error.log` in the ISHA folder; installer warnings are stored in `installer_state.json → warnings`.
