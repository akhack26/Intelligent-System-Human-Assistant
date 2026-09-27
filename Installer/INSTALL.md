# Installing ISHA

You do **not** need to install Python, pip packages, llama.cpp or a model yourself. The installer does it.

## Windows 10 / 11

1. Download the ISHA package and unzip it (or run `ISHA_Setup.exe` if you received one).
2. Double-click **`ISHA_Setup.cmd`**.
   * If a compatible Python (3.10–3.13, 64-bit) is missing, Setup asks, then installs **Python 3.12 for your
     user account only** — via `winget`, or the official python.org installer after checking that its digital
     signature is valid and belongs to the Python Software Foundation. No administrator rights needed.
3. The ISHA Setup window opens. Follow the screens:

| Screen | What happens |
|---|---|
| Welcome | Version; detects an existing installation (then it upgrades/repairs and keeps your data). |
| Terms | You must tick *I agree* — **Next stays disabled until you do**. |
| Privacy | Exactly which features are local and which use the internet. |
| System check | OS, CPU, RAM, storage, Python, GPU, internet, permissions: `READY` / `WARNING` / `MISSING`. |
| Location | Default `%LOCALAPPDATA%\Programs\ISHA` (see below why not *Program Files*). Desktop shortcut option. |
| Features | Voice, speech recognition, vision, file indexing (folders + exclusions), WhatsApp, PC control, coding tools, and how much control ISHA gets. |
| AI model | Minimal / Recommended / Advanced / Custom / Skip, with size, RAM and a hardware-based recommendation. Storage is checked **before** anything downloads. |
| Install | Runtime → packages → llama.cpp → model download (pause / resume / cancel, speed, ETA) → config → shortcuts. |
| Diagnostics | Checks everything ISHA needs; optional "Test model loading". |
| Finish | Launch ISHA. |

ISHA then appears in the **Start Menu** (and Desktop, if chosen), and in **Settings → Apps** for uninstalling.

**Why not `C:\Program Files`?** ISHA stores its settings, memory and models next to the program. Program
Files is read-only for normal users, so ISHA would have to run as administrator every time. A per-user
folder is safer. You can still pick any folder you can write to.

## Linux (Ubuntu, Debian, Fedora, Arch, openSUSE …)

```bash
./install.sh            # graphical installer when a desktop session exists
./install.sh --headless # text mode (servers, SSH)
```

If Python 3.10–3.13 with `venv` is missing, the script prints the exact command for your distribution
(e.g. `sudo apt-get install -y python3 python3-venv python3-pip`) and asks before running it.
Default location: `~/.local/share/isha`. A menu entry (`isha.desktop`) is created; the Desktop launcher is optional.

For speech recognition, PyAudio needs PortAudio: `sudo apt install portaudio19-dev python3-dev`
(if it is missing, only the microphone feature is unavailable — the rest installs normally).

## Choosing a model

| Choice | Downloads | Good for |
|---|---|---|
| Minimal | one general model (1.1–4.7 GB depending on your RAM) | any PC |
| Recommended | general + coding (+ reasoning, which reuses the general model below 16 GB RAM) | most users |
| Advanced | + fast model + vision (moondream2) | 16 GB+ RAM |
| Custom | pick from the catalogue, or **use an existing .gguf** you already have (used in place, not copied) | experts |
| Skip | nothing now — add later with the first-run wizard | offline installs |

The recommendation is technical: a Q4_K_M model needs roughly its file size in RAM plus room for context
and the OS — ~1.5B for 4–8 GB, 3B for 8–16 GB, 7B for 16 GB+ (or a 6 GB+ GPU). You can override it.

## Command-line install (scripts, automation)

```bash
python -m installer --headless --accept-terms --yes --preset recommended \
       --dir ~/ISHA --features voice,file_index,coding,advanced_pc --autonomy advanced
python -m installer --headless --accept-terms --custom-model general=/data/my-model.gguf --preset none
python -m installer --check                         # system check only
python -m installer --diagnose --install-dir DIR --deep
```

## Offline installation

Put an `offline/` folder next to the installer (build it with `packaging/build_offline_bundle.py`):

```
offline/wheels/     Python packages   offline/llamacpp/   llama.cpp release archive
offline/models/     GGUF files        offline/python/     python.org installer (Windows)
```

Setup finds it automatically and uses it before the internet. Models copied from it are verified exactly
like downloads.

## Uninstalling

Windows: *Settings → Apps → ISHA → Uninstall* or *Start Menu → ISHA → Uninstall ISHA*.
Linux: the *Uninstall ISHA* menu entry, or `python -m installer --uninstall --install-dir DIR`.

By default the application and runtime are removed, while **your models, settings and memory are kept**
(listed in `ISHA_USER_DATA_KEPT.txt`). Tick the boxes (or pass `--remove-models --remove-config
--remove-memory --remove-cache`, or `--remove-all`) to delete them too.

## Updating

Run the newer installer and choose the same folder. It backs up the previous `mani.py`/`isha_core` to
`backup/<version>-<date>/` (last two kept), updates packages, and **never replaces your models or settings**
(the config is merged, with a timestamped backup).
