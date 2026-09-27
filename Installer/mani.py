from __future__ import annotations  

import sys, math, random, os, json, subprocess, ctypes, re, time, tempfile, asyncio, fnmatch, webbrowser, platform, shutil, importlib.util, threading, gc, difflib, mimetypes, logging, base64, urllib.request
import atexit, importlib, socket, tarfile, zipfile, urllib.error
from datetime import datetime
from pathlib import Path

# ------------------------------------------------

_OS_NAME    = platform.system()          
_IS_WINDOWS = _OS_NAME == "Windows"
_IS_LINUX   = _OS_NAME == "Linux"
_IS_MACOS   = _OS_NAME == "Darwin"

# ------------------------------------------------
# Centralized Model Defaults (Qwen 2.5 1.5B)
# ------------------------------------------------
# ISHA runs GGUF weights directly through llama.cpp — there is no model
# server. These are only the *default file names* looked for under models/;
# the real path lives in config as "gguf_model_path".
#   8GB  RAM -> a 3B  Q4_K_M build  (~2.0 GB)
#   16GB RAM -> a 7B  Q4_K_M build  (~4.4 GB)
#   32GB RAM -> a 14B Q4_K_M build  (~8.5 GB)
DEFAULT_GGUF_NAME  = "llama-3.2-3b-instruct-q4_k_m.gguf"
DEFAULT_CHAT_MODEL = DEFAULT_GGUF_NAME
DEFAULT_ROUTER_MODEL = DEFAULT_GGUF_NAME

# ------------------------------------------------

_SESSION_TYPE = os.environ.get("XDG_SESSION_TYPE", "").lower() if _IS_LINUX else ""

# Single source of truth for "the folder ISHARUNING.py actually lives in",
# resolved from __file__ (never from cwd) so ISHA behaves identically no
# matter which directory it is launched from.
PROJECT_ROOT   = Path(__file__).resolve().parent
_RUNTIME_DIR   = PROJECT_ROOT / ".isha_runtime"
_DEPS_OK_FLAG  = _RUNTIME_DIR / ".deps_ok"
_REEXEC_GUARD  = "ISHA_BOOTSTRAPPED"  

_CORE_PACKAGES = ["PyQt5"]
_OPTIONAL_PACKAGES = [
    # llama-cpp-python ko jaan-boojh kar yahan se hataya gaya hai.
    # PyPI par uska sirf sdist hai, to plain `pip install` chupchaap ek
    # CMake/MSVC source build shuru kar deta hai aur bina Build Tools wali
    # machine par "CMAKE_C_COMPILER not set" de kar mar jaata hai.
    # Ab GGUF brain ka maalik neeche wala backend resolver hai.
    "requests", "edge-tts", "pygame", "SpeechRecognition", "PyAudio", "psutil",
    "opencv-python-headless", "mediapipe", "pyautogui", "send2trash",
    "pyttsx3", "pyperclip",
]
_WINDOWS_ONLY_PACKAGES = ["pywin32", "pycaw", "comtypes"]


_IMPORT_NAME = {
    "llama-cpp-python": "llama_cpp",
    "edge-tts": "edge_tts",
    "SpeechRecognition": "speech_recognition",
    "speechrecognition": "speech_recognition",
    "PyAudio": "pyaudio",
    "pyaudio": "pyaudio",
    "opencv-python-headless": "cv2",
    "mediapipe": "mediapipe",
    "pywin32": "win32api",
}


def _pkg_import_name(pkg_spec: str) -> str:
    base = pkg_spec.split("==")[0].split(">=")[0].strip()
    return _IMPORT_NAME.get(pkg_spec, _IMPORT_NAME.get(base, base.replace("-", "_")))


def _is_installed(pkg_spec: str) -> bool:
    try:
        return importlib.util.find_spec(_pkg_import_name(pkg_spec)) is not None
    except Exception:
        return False


def _wanted_packages() -> list:
    for fname in ["requirement.txt", "requirements.txt", "requirement.TXT", "REQUIREMENT.TXT"]:
        req_file = PROJECT_ROOT / fname
        if req_file.exists():
            try:
                lines = req_file.read_text(encoding="utf-8").splitlines()
                pkgs = [ln.split(" #")[0].split("\t#")[0].strip() for ln in lines]
                pkgs = [p for p in pkgs if p and not p.startswith("#")
                        and p.split(";")[0].strip().lower() not in ("pytest",)]
                if pkgs:
                    return pkgs
            except Exception:
                pass
    pkgs = list(_CORE_PACKAGES) + list(_OPTIONAL_PACKAGES)
    if _IS_WINDOWS:
        pkgs += _WINDOWS_ONLY_PACKAGES
    return pkgs


def _pip_install(python_exe: str, packages: list, extra_args=None) -> bool:
    if not packages:
        return True
    cmd = [python_exe, "-m", "pip", "install", "--disable-pip-version-check", "-q"] + (extra_args or []) + packages
    try:
        subprocess.run(cmd, check=True, timeout=1800)
        return True
    except Exception as e:
        print(f"[ISHA WARNING] pip install failed for {packages}: {e}")
        return False


def _fix_opencv_qt_conflict(python_exe: str, wanted: list):
    """mediapipe's own dependency list can silently pull in a *non-headless*
    OpenCV build (opencv-python / opencv-contrib-python) even when we
    explicitly asked pip for opencv-python-headless. Non-headless OpenCV
    bundles its own private copy of Qt (for imshow/highgui windows) inside
    site-packages/cv2/qt/plugins — and because it installs into the SAME
    "cv2" package folder as opencv-python-headless, whichever one gets
    written to disk last silently overwrites the other's files. If the
    non-headless one wins, importing cv2 later in this file resets
    QT_QPA_PLATFORM_PLUGIN_PATH to ITS OWN bundled (and version-mismatched)
    Qt plugins, which is exactly what causes:
        qt.qpa.plugin: Could not load the Qt platform plugin "xcb" ...
        QObject::moveToThread: Current thread is not the object's thread
    at startup. Fix: after every install, explicitly strip the non-headless
    builds and force opencv-python-headless back in as the final word, so
    the outcome doesn't depend on pip's install order.
    """
    if not any("opencv" in p.lower() or "mediapipe" in p.lower() for p in wanted):
        return
    try:
        subprocess.run(
            [python_exe, "-m", "pip", "uninstall", "-y", "-q",
             "opencv-python", "opencv-contrib-python", "opencv-python-headless"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
    except Exception:
        pass
    _pip_install(python_exe, ["opencv-python-headless"], extra_args=["--force-reinstall", "--no-deps"])


def _venv_python(venv_dir: Path) -> Path:
    bin_dir = "Scripts" if _IS_WINDOWS else "bin"
    py_name = "python.exe" if _IS_WINDOWS else "python3"
    return venv_dir / bin_dir / py_name


def _base_name(pkg_spec: str) -> str:
    return pkg_spec.split("==")[0].split(">=")[0].strip().lower()


def _install_optional_packages(python_exe: str, optional: list):
    """Install optional packages as one batch first (fast path — normally
    everything succeeds together). If that combined install fails, retry
    ONE package at a time instead of giving up on all of them: a single
    package with no wheel for this Python version (a real, currently-
    happening example: mediapipe often lags 6-12 months behind the newest
    CPython release) must not also block every other optional feature from
    installing. Each package's own try/except import guard elsewhere in this
    file already handles it not being there at runtime — this just makes
    sure that failure stays contained to that one package."""
    if not optional:
        return
    if _pip_install(python_exe, optional):
        return
    print("[ISHA] Some optional packages failed to install together — retrying one at a time "
          "so a single incompatible package doesn't block the rest...")
    for pkg in optional:
        if _pip_install(python_exe, [pkg]):
            continue
        hint = ""
        if _base_name(pkg) == "mediapipe":
            hint = (" mediapipe usually lags behind the newest Python release by several "
                    "months — hand-gesture (webcam) control needs Python 3.10–3.12 for now. "
                    "Everything else in ISHA works fine without it.")
        print(f"[ISHA] Skipping optional package '{pkg}' — the feature that depends on it "
              f"will report itself unavailable instead of ISHA failing to start.{hint}")


def _create_and_populate_venv(venv_dir: Path) -> Path | None:
    """Create venv_dir (if needed), install everything into it, return its
    python executable path, or None on failure."""
    py_exe = _venv_python(venv_dir)
    if not py_exe.exists():
        print(f"[ISHA] Setting up a local Python environment (first run only): {venv_dir}")
        try:
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, timeout=300)
        except Exception as e:
            print(f"[ISHA WARNING] Could not create virtual environment: {e}")
            if _IS_LINUX:
                print("[ISHA HINT] On Debian/Ubuntu try: sudo apt install python3-venv")
            return None
    if not py_exe.exists():
        return None
    print("[ISHA] Installing dependencies (first run only, this can take a few minutes)...")
    _pip_install(str(py_exe), ["pip", "wheel"], extra_args=["--upgrade"])

    wanted = _wanted_packages()
    core_names = {_base_name(c) for c in _CORE_PACKAGES}
    core = [p for p in wanted if _base_name(p) in core_names]
    optional = [p for p in wanted if _base_name(p) not in core_names]

    core_ok = _pip_install(str(py_exe), core)
    if not core_ok:
        print("[ISHA WARNING] Could not install core dependency (PyQt5). ISHA may not start.")

    _install_optional_packages(str(py_exe), optional)
    _fix_opencv_qt_conflict(str(py_exe), wanted)

    if core_ok:
        try:
            _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            _DEPS_OK_FLAG.write_text(datetime.now().isoformat(), encoding="utf-8")
        except Exception:
            pass
        print("[ISHA] Dependencies installed successfully.")
    return py_exe


# ------------------------------------------------------------------
# GGUF backend (llama-cpp-python) dependency management
#
# This is deliberately separate from the generic optional-package install
# above: llama-cpp-python is ISHA's actual brain, so a failure here must
# never be silently swallowed. Whatever interpreter is *actually* about to
# run the rest of this file (sys.executable, at the point this is called —
# i.e. after any .isha_runtime re-exec has already happened) is the one
# target: that is "the runtime Python ISHA runs in", dynamically detected
# instead of hard-coded to any one venv path.
# ------------------------------------------------------------------

_GGUF_BACKEND_DIAGNOSTIC = {
    "python": sys.executable,
    "python_version": platform.python_version(),
    "platform": f"{_OS_NAME} {platform.release()}",
    "architecture": platform.machine(),
    "installed": False,
    "error": "",
}


def _pip_install_captured(python_exe: str, packages: list, extra_args=None, timeout: int = 1800):
    """Same job as _pip_install, but never throws the real pip output away —
    it is captured and returned so a caller can show/log the actual reason
    for a failure instead of a bare 'returned non-zero exit status'."""
    cmd = [python_exe, "-m", "pip", "install", "--disable-pip-version-check"] + (extra_args or []) + list(packages)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
        return res.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"pip install timed out after {timeout}s (command: {' '.join(cmd)})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _check_llama_cpp_import(python_exe: str):
    """Actually try `from llama_cpp import Llama` in python_exe via a fresh
    subprocess (a subprocess sees a clean sys.path, so this is trustworthy
    even immediately after a pip install into the same interpreter that is
    still running)."""
    try:
        res = subprocess.run(
            [python_exe, "-c", "import llama_cpp; from llama_cpp import Llama"],
            capture_output=True, text=True, timeout=60,
        )
        if res.returncode == 0:
            return True, ""
        return False, (res.stderr or res.stdout or "").strip()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _in_runtime_venv() -> bool:
    """True only if the interpreter running right now IS .isha_runtime's own
    python (so we don't try to recreate/re-enter it pointlessly)."""
    try:
        return Path(sys.executable).resolve() == _venv_python(_RUNTIME_DIR).resolve()
    except Exception:
        return False


# ======================================================================
# GGUF BACKEND RESOLVER  (pehle alag isha_gguf_backend.py me tha —
# ab yahin inline hai taaki koi doosri file rakhni/khoni na pade)
#
# PROBLEM JO YEH SOLVE KARTA HAI
# ------------------------------
# `pip install llama-cpp-python` PyPI se sirf ek *source* distribution
# deta hai. Agar current Python ke liye koi prebuilt wheel nahi hai
# (jaise Python 3.14 par — koi cp314 wheel exist hi nahi karta), pip
# chupchaap CMake/MSVC se build shuru kar deta hai. Build Tools nahi
# hain to:
#       Running 'nmake' '-?' failed with: no such file or directory
#       CMAKE_C_COMPILER not set, after EnableLanguage
# ...aur poora GGUF backend dead.
#
# TEEN LAYERS, TEENO BINA KISI COMPILER KE:
#   1. llama_cpp already importable            -> wahi use karo
#   2. pip install --only-binary=:all: se
#      prebuilt wheel                          -> pip ko build karne ki
#                                                 IJAAZAT HI NAHI hai,
#                                                 wheel na mile to 1 sec
#                                                 me clean fail
#   3. llama.cpp ka OFFICIAL PREBUILT
#      llama-server binary + localhost HTTP    -> HAMESHA chalta hai
#
# Layer 3 kisi bhi Python version par same behave karti hai (3.11, 3.14,
# 3.20 — koi farak nahi) aur kabhi compile nahi karti. Wahi is fix ko
# permanent banati hai. Uska LlamaServer class llama-cpp-python ke Llama
# ka drop-in hai: wahi create_chat_completion(messages=..., stream=...)
# aur wahi OpenAI-shaped return, isliye neeche ka baaki code unchanged.
# ======================================================================


# The llama.cpp resolver now lives in isha_core/llama_runtime.py so the ISHA
# installer can run exactly the same code. Every name is re-imported here, so
# nothing below changed.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from isha_core import llama_runtime as _llama_rt  # noqa: E402
from isha_core.llama_runtime import (  # noqa: E402,F401
    LLAMACPP_DIR, _STAMP, _WHEEL_INDEXES, _GH_REPO, _GH_API, _GH_DL, _FALLBACK_TAGS, _UA,
    _VARIANT, DIAGNOSTIC, _log, _note, _try_import_llama, _pip, _try_prebuilt_wheel,
    _asset_suffix, _http_json, _http_text, _assets_of, _latest_tag, _find_asset_url,
    _server_exe, _download, _install_llamacpp, _free_port, _LIVE_SERVERS, _kill, _kill_all,
    LlamaServer, resolve_backend, ensure_backend, status_text,
)


def _ensure_gguf_backend() -> bool:
    """GGUF brain ready karo — bina kuch compile kiye.

    Asli kaam upar wale resolver me hai (3 layers: already-installed wheel ->
    prebuilt-only wheel install -> llama.cpp ka prebuilt llama-server binary).
    Layer 2 me `--only-binary=:all:` pip ko source build karne se rokta hai,
    isliye purana "Running 'nmake' '-?' failed / CMAKE_C_COMPILER not set"
    crash dobara ho hi nahi sakta; aur Layer 3 ko na compiler chahiye, na
    Visual Studio, na CMake, aur use Python version se koi matlab nahi.
    """
    global _GGUF_BACKEND_DIAGNOSTIC
    ok = ensure_backend()
    _GGUF_BACKEND_DIAGNOSTIC = {
        "python":         DIAGNOSTIC.get("python", sys.executable),
        "python_version": DIAGNOSTIC.get("python_version", platform.python_version()),
        "platform":       DIAGNOSTIC.get("platform", _OS_NAME),
        "architecture":   DIAGNOSTIC.get("architecture", platform.machine()),
        "installed":      bool(ok),
        "mode":           DIAGNOSTIC.get("mode", "none"),
        "error":          DIAGNOSTIC.get("error", ""),
    }
    if ok:
        print(f"[gguf] backend ready — mode: {DIAGNOSTIC.get('mode')}")
    else:
        print("[gguf] no GGUF backend available.\n" + status_text())
    return bool(ok)


def _force_pyqt5_qt_plugin_path():
    """Point Qt at PyQt5's OWN bundled plugins (xcb/windows/etc.), overriding
    anything another imported package (see _fix_opencv_qt_conflict above) may
    have pointed QT_QPA_PLATFORM_PLUGIN_PATH at instead. Safe to call
    multiple times; called again right before QApplication() is constructed
    in main() so it always has the final word no matter what happened during
    module import in between."""
    try:
        import PyQt5
        plugin_path = os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
        if os.path.exists(plugin_path):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = plugin_path
    except Exception:
        pass


def _ensure_environment():

    missing_core = [p for p in _CORE_PACKAGES if not _is_installed(p)]
    if not missing_core:
        _force_pyqt5_qt_plugin_path()
        # PyQt5 being present here does NOT mean llama-cpp-python is — this
        # used to return without ever checking the GGUF backend, which is
        # exactly how the runtime could end up running under a Python that
        # has PyQt5 but not llama-cpp-python. Check/install it now, into
        # this exact interpreter (the one actually about to run ISHA).
        _ensure_gguf_backend()
        return

    if os.environ.get(_REEXEC_GUARD) == "1":

        print("[ISHA WARNING] PyQt5 still not available after environment setup. "
              "Please check your internet connection and try again, or install "
              "manually: pip install PyQt5")
        return

    print("[ISHA] PyQt5 not found in current environment. Setting up automatically...")

    candidate_venvs = [
        _RUNTIME_DIR,
        PROJECT_ROOT / "myenv",
        PROJECT_ROOT / ".venv",
        PROJECT_ROOT / "venv",
        Path.home() / "myenv",
    ]
    for venv_path in candidate_venvs:
        py_executable = _venv_python(venv_path)
        if py_executable.exists():
            try:
                res = subprocess.run([str(py_executable), "-c", "import PyQt5"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                if res.returncode == 0:
                    os.environ[_REEXEC_GUARD] = "1"
                    print(f"[ISHA] Re-launching with environment: {py_executable}")
                    os.execv(str(py_executable), [str(py_executable)] + sys.argv)
            except Exception:
                pass

    
    py_exe = _create_and_populate_venv(_RUNTIME_DIR)
    if py_exe is not None and py_exe.exists():
        os.environ[_REEXEC_GUARD] = "1"
        print(f"[ISHA] Re-launching with environment: {py_exe}")
        os.execv(str(py_exe), [str(py_exe)] + sys.argv)
        return  


    print("[ISHA] Falling back to installing into the current Python interpreter...")
    pkgs = _wanted_packages()
    core_names = {_base_name(c) for c in _CORE_PACKAGES}
    core = [p for p in pkgs if _base_name(p) in core_names]
    optional = [p for p in pkgs if _base_name(p) not in core_names]
    for extra in (["--user", "--break-system-packages"], ["--user"]):
        if _pip_install(sys.executable, core, extra_args=extra):
            break
    _install_optional_packages(sys.executable, optional)
    _fix_opencv_qt_conflict(sys.executable, pkgs)
    try:
        import PyQt5  # noqa: F401
    except Exception:
        print("[ISHA WARNING] Automatic dependency installation failed.")
        print("[ISHA HINT] Please install Python 3.9+ from python.org and ensure it's on your PATH, "
              "then run this script again. If you're offline, connect to the internet once so ISHA "
              "can install its dependencies, after which it will work fully offline using its "
              "local GGUF model.")
    # This branch never re-execs (it installs straight into the current
    # interpreter), so this IS the final runtime — check the GGUF backend here.
    _ensure_gguf_backend()

_ensure_environment()

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
    QLineEdit, QLabel, QScrollArea, QFrame, QSizePolicy,
    QSystemTrayIcon, QMenu, QAction, QSlider, QListWidget, QListWidgetItem,
    QGraphicsDropShadowEffect, QPushButton, QDialog, QTextEdit,
    QSplitter, QAbstractScrollArea, QComboBox, QCheckBox, QProgressBar,
    QMessageBox, QSpinBox, QDoubleSpinBox,
)
from PyQt5.QtCore import (
    Qt, QTimer, QThread, QObject, pyqtSignal, QCoreApplication,
    QPropertyAnimation, QEasingCurve, QPointF, QRectF,
    QVariantAnimation, QPoint, QSize, QProcess,
)
from PyQt5.QtGui import (
    QPainter, QColor, QPen, QBrush,
    QLinearGradient, QRadialGradient,
    QPainterPath, QFont, QFontMetrics, QIcon, QPixmap,
    QTextOption, QImage,
)

# ------------------------------------------------
# ISHA agent core (headless subsystems: router, permissions, audit, file
# index, coding agent, software manager, memory, vision). See ARCHITECTURE.md.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from isha_core import paths as isha_paths
from isha_core import hardware as isha_hw
from isha_core import router as isha_router
from isha_core import permissions as isha_perm
from isha_core import audit as isha_audit
from isha_core import commands as isha_cmd
from isha_core import file_index as isha_index
from isha_core import nl_search as isha_search
from isha_core import software as isha_sw
from isha_core import system as isha_sys
from isha_core import coding as isha_code
from isha_core import memory as isha_mem
from isha_core import vision as isha_vision
from isha_core import config as isha_config
from isha_core.control import STOP as ISHA_STOP, Cancelled as ISHA_Cancelled


def _desktop_dir() -> Path:
    """The real Desktop folder (OneDrive / XDG aware), not a guess."""
    return isha_paths.known_folder("desktop")

# ------------------------------------------------

_ICON_DIR = Path(__file__).resolve().parent / "icon"

# ------------------------------------------------

C_NEON   = None
C_PINK   = None
C_BG     = None
C_TEXT   = None
C_DIM    = None

def _init_colors():
    global C_NEON, C_PINK, C_BG, C_TEXT, C_DIM
    if C_NEON is None:
        C_NEON   = QColor(0, 240, 255)
        C_PINK   = QColor(255, 0, 127)
        C_BG     = QColor(7, 10, 20)
        C_TEXT   = QColor(200, 230, 255)
        C_DIM    = QColor(60, 100, 140)

# ------------------------------------------------

def _init_fonts():
    QFont.insertSubstitutions("Segoe UI", [
        "Ubuntu", "Noto Sans", "Cantarell", "DejaVu Sans", "Helvetica Neue", "Arial", "sans-serif",
    ])
    QFont.insertSubstitutions("Segoe UI Semibold", [
        "Ubuntu Medium", "Noto Sans Medium", "Cantarell", "DejaVu Sans", "Arial", "sans-serif",
    ])
    QFont.insertSubstitutions("Segoe UI Emoji", [
        "Noto Color Emoji", "Noto Emoji", "Segoe UI Symbol", "sans-serif",
    ])

# ------------------------------------------------

def _popen_silent(cmd: str) -> None:
    print(f"[STUB] would run: {cmd}")

# ------------------------------------------------

def _start_native_drag(widget, event) -> bool:
    """Call from mousePressEvent. Returns True if a native OS-assisted drag
    was started (covers Windows, X11 and Wayland). If it returns False
    (very old Qt/PyQt without startSystemMove), the caller should fall back
    to the manual offset+move() dragging as before."""
    if event.button() != Qt.LeftButton:
        return False
    handle = widget.windowHandle()
    if handle is not None and hasattr(handle, "startSystemMove"):
        try:
            return bool(handle.startSystemMove())
        except Exception:
            return False
    return False

# ------------------------------------------------

try:
    import requests
    _REQUESTS_OK = True
except Exception:
    _REQUESTS_OK = False

try:
    import pygame
    _PYGAME_OK = True
except Exception:
    _PYGAME_OK = False

# ------------------------------------------------
# Text-to-Speech capability probe.
#
# The old code did `_TTS_OK = _PYGAME_OK` inside the edge_tts import, so if
# EITHER edge-tts or pygame was missing/broken, speech was silently switched
# off with no message anywhere. Speech now has three independent layers:
#   1. edge-tts (best voice)  -> needs an mp3 player
#   2. pyttsx3                -> offline, no player needed
#   3. the OS's own engine    -> SAPI / say / espeak
# and _TTS_OK is True if ANY of them can actually make sound.
# ------------------------------------------------

try:
    import edge_tts
    _EDGE_TTS_OK = True
except Exception:
    _EDGE_TTS_OK = False

try:
    import pyttsx3
    _PYTTSX3_OK = True
except Exception:
    _PYTTSX3_OK = False


def _which_first(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _detect_mp3_player():
    """Figure out how we can play the mp3 that edge-tts hands back.
    Returns (kind, argv_prefix) where kind is 'pygame' | 'powershell' | 'cmd'."""
    if _PYGAME_OK:
        return "pygame", None
    if _IS_MACOS:
        exe = _which_first("afplay")
        if exe:
            return "cmd", [exe]
    if _IS_LINUX:
        exe = _which_first("mpg123", "ffplay", "mpv", "cvlc", "vlc")
        if exe:
            base = os.path.basename(exe)
            if base == "ffplay":
                return "cmd", [exe, "-nodisp", "-autoexit", "-loglevel", "quiet"]
            if base == "mpv":
                return "cmd", [exe, "--no-video", "--really-quiet"]
            if base in ("cvlc", "vlc"):
                return "cmd", [exe, "--intf", "dummy", "--play-and-exit", "--quiet"]
            return "cmd", [exe, "-q"]
    if _IS_WINDOWS:
        # Windows can always play mp3 through System.Windows.Media.MediaPlayer
        return "powershell", None
    return None, None


_MP3_PLAYER_KIND, _MP3_PLAYER_CMD = _detect_mp3_player()


def _detect_native_tts():
    if _IS_WINDOWS:
        return True                      # SAPI ships with Windows
    if _IS_MACOS:
        return _which_first("say") is not None
    return _which_first("espeak-ng", "espeak", "spd-say") is not None


_NATIVE_TTS_OK = _detect_native_tts()

_TTS_OK = (_EDGE_TTS_OK and _MP3_PLAYER_KIND is not None) or _PYTTSX3_OK or _NATIVE_TTS_OK


def _tts_diagnostics() -> str:
    """One-line summary so a silent ISHA is debuggable from the console."""
    return (f"edge-tts={_EDGE_TTS_OK}, mp3-player={_MP3_PLAYER_KIND or 'NONE'}, "
            f"pyttsx3={_PYTTSX3_OK}, native={_NATIVE_TTS_OK}, speech={_TTS_OK}")


# ---- Making text actually speakable ----------------------------------------
# The default model is deepseek-r1, which wraps its reasoning in <think> tags.
# Feeding that (plus markdown, code fences, URLs and emoji) straight into a TTS
# engine is the other half of "speech is broken": you either hear minutes of
# reasoning read aloud, or the engine chokes on a wall of symbols.

_SPEAK_CLEAN_RULES = [
    (re.compile(r"<think\b[^>]*>.*?</think\s*>", re.S | re.I), " "),
    (re.compile(r"<think\b[^>]*>.*$", re.S | re.I), " "),      # unterminated while streaming
    (re.compile(r"</?think\s*>", re.I), " "),
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"https?://\S+|www\.\S+"), " link "),
    (re.compile(r"[\u2190-\u21FF\u2300-\u23FF\u2460-\u24FF\u25A0-\u27BF"
                r"\u27F0-\u27FF\u2B00-\u2BFF\uFE0F\u200D"
                r"\U0001F000-\U0001FAFF]"), " "),
    (re.compile(r"^\s*[*\-+]\s+", re.M), " "),
    (re.compile(r"[*_#>~|`]+"), " "),
    (re.compile(r"[ \t]*\n[ \t]*"), ". "),
    (re.compile(r"\.{2,}"), "."),
    (re.compile(r"\s+"), " "),
]


_THINK_RE = [
    re.compile(r"<think\b[^>]*>.*?</think\s*>", re.S | re.I),
    re.compile(r"<think\b[^>]*>.*$", re.S | re.I),
    re.compile(r"</?think\s*>", re.I),
]


def strip_thinking(text: str) -> str:
    """Remove a reasoning model's <think> scratchpad from user-visible output."""
    out = text or ""
    for pat in _THINK_RE:
        out = pat.sub("", out)
    return out.strip()


def _strip_for_speech(text: str) -> str:
    out = text or ""
    for pat, rep in _SPEAK_CLEAN_RULES:
        out = pat.sub(rep, out)
    out = out.strip(" .")
    # If there's nothing pronounceable left, don't bother the engine.
    if not re.search(r"[A-Za-z0-9\u0900-\u097F]", out):
        return ""
    return out.strip()


def _chunk_for_speech(text: str, limit: int = 420) -> list:
    """Short requests are far more reliable with edge-tts, and chunking also
    means speech starts sooner instead of after the whole reply is synthesised."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for sentence in re.split(r"(?<=[.!?\u0964])\s+", text):
        if not sentence:
            continue
        while len(sentence) > limit:                 # a single monster sentence
            parts.append(sentence[:limit].strip())
            sentence = sentence[limit:]
        if buf and len(buf) + len(sentence) + 1 > limit:
            parts.append(buf.strip())
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        parts.append(buf.strip())
    return [p for p in parts if p]

try:
    import speech_recognition as sr
    _STT_OK = True
except Exception:
    _STT_OK = False

try:
    from send2trash import send2trash as _send2trash_fn
    _SEND2TRASH_OK = True
except Exception:
    _SEND2TRASH_OK = False


# ======================================================================

try:
    import psutil
    _PSUTIL_OK = True
except Exception:
    _PSUTIL_OK = False

try:
    import win32gui
    import win32con
    import win32process
    import win32api
    import win32com.client   
    _WIN32_OK = True
except Exception:
    _WIN32_OK = False

def _result(success: bool, message: str, data=None) -> dict:
    return {"success": success, "message": message, "data": data}


# ------------------------------------------------------------------
# Lightweight error logging for the system/file engine. Every new method
# below wraps its OS calls in try/except and returns a normal _result(False, ...)
# to the caller either way (so a failure here never crashes the Qt event loop) —
# this just also appends a timestamped line to disk so failures aren't silently
# lost to a scrollback buffer the user never looks at.
# ------------------------------------------------------------------
_ERROR_LOG_PATH = Path(__file__).resolve().parent / "isha_error.log"
_error_logger = logging.getLogger("isha.errors")
if not _error_logger.handlers:
    try:
        _handler = logging.FileHandler(str(_ERROR_LOG_PATH), encoding="utf-8")
        _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        _error_logger.addHandler(_handler)
        _error_logger.setLevel(logging.WARNING)
        _error_logger.propagate = False
    except Exception:
        pass  # if even the log file can't be opened, methods still work — they just won't persist errors


def _log_error(context: str, exc: Exception) -> None:
    try:
        _error_logger.warning(f"{context}: {exc}")
    except Exception:
        pass  # logging must never itself raise into a caller's except block


# ------------------------------------------------------------------
# Vector Memory Engine (ChromaDB/FAISS with lightweight TF-IDF fallback)
# ------------------------------------------------------------------
class VectorMemoryEngine:
    """Long-term contextual memory layer with ChromaDB/FAISS integration,
    plus a lightweight TF-IDF / Cosine-similarity fallback vector store."""
    def __init__(self, storage_dir: Path = None):
        self.storage_dir = storage_dir or (Path(__file__).resolve().parent / "isha_memory")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.backend = "fallback"
        self._chroma_coll = None
        self._fallback_db = []
        self._db_file = self.storage_dir / "memory.json"
        self._init_backend()

    def _init_backend(self):
        try:
            import chromadb
            client = chromadb.PersistentClient(path=str(self.storage_dir / "chroma"))
            self._chroma_coll = client.get_or_create_collection("isha_memories")
            self.backend = "chromadb"
            print("[VectorMemory] Initialized with ChromaDB backend.")
            return
        except Exception:
            pass

        if self._db_file.exists():
            try:
                self._fallback_db = json.loads(self._db_file.read_text(encoding="utf-8"))
            except Exception:
                self._fallback_db = []
        print("[VectorMemory] Initialized with lightweight vector store fallback.")

    def add_memory(self, text: str, metadata: dict = None) -> None:
        if not text or not text.strip():
            return
        meta = metadata or {"timestamp": datetime.now().isoformat()}
        mem_id = f"mem_{int(time.time()*1000)}"
        if self.backend == "chromadb" and self._chroma_coll is not None:
            try:
                self._chroma_coll.add(documents=[text], metadatas=[meta], ids=[mem_id])
                return
            except Exception as e:
                print(f"[VectorMemory] ChromaDB add error: {e}")

        words = list(set(re.findall(r'\w+', text.lower())))
        self._fallback_db.append({
            "id": mem_id,
            "text": text,
            "metadata": meta,
            "tokens": words,
        })
        self._save_fallback()

    def search_memory(self, query: str, top_k: int = 3) -> list:
        if not query or not query.strip():
            return []
        if self.backend == "chromadb" and self._chroma_coll is not None:
            try:
                res = self._chroma_coll.query(query_texts=[query], n_results=top_k)
                docs = res.get("documents", [[]])[0]
                metas = res.get("metadatas", [[]])[0]
                return [{"text": doc, "metadata": meta} for doc, meta in zip(docs, metas)]
            except Exception as e:
                print(f"[VectorMemory] ChromaDB query error: {e}")

        q_tokens = set(re.findall(r'\w+', query.lower()))
        if not q_tokens:
            return []
        scored = []
        for item in self._fallback_db:
            tokens = set(item.get("tokens", []))
            intersection = q_tokens.intersection(tokens)
            if intersection:
                score = len(intersection) / (len(q_tokens) + len(tokens) - len(intersection) + 1e-5)
                scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"text": it["text"], "metadata": it["metadata"]} for sc, it in scored[:top_k]]

    def _save_fallback(self):
        try:
            self._db_file.write_text(json.dumps(self._fallback_db, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


# ------------------------------------------------------------------

class WindowsController:
    """Cross-platform OS-control primitives ISHA can use (Windows & Zorin OS / Linux).
    Every public method is synchronous and safe to run off the UI thread via OSTaskWorker."""

    KNOWN_BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "chrome", "msedge", "firefox", "brave", "opera", "chromium"}

    # ---------------- Window management ----------------
    def _require_win32(self):
        if _IS_WINDOWS and not _WIN32_OK:
            raise RuntimeError("pywin32 not installed. Install: pip install pywin32")

    def list_open_windows(self) -> dict:
        """Return every visible top-level window with a title."""
        windows = []
        if _IS_WINDOWS and _WIN32_OK:
            def _enum_handler(hwnd, _):
                if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    windows.append({"hwnd": hwnd, "title": title, "pid": pid})
            win32gui.EnumWindows(_enum_handler, None)
            return _result(True, f"{len(windows)} windows open.", windows)
        elif _IS_LINUX:
            if shutil.which("wmctrl"):
                try:
                    out = subprocess.check_output(["wmctrl", "-l", "-p"], text=True, errors="replace")
                    for line in out.strip().splitlines():
                        parts = line.split(None, 4)
                        if len(parts) >= 5:
                            wid, desktop, pid, host, title = parts
                            if title.strip():
                                windows.append({"hwnd": wid, "title": title.strip(), "pid": int(pid) if pid.isdigit() else 0})
                    return _result(True, f"{len(windows)} windows open.", windows)
                except Exception:
                    pass
            if _PSUTIL_OK:
                for proc in psutil.process_iter(['pid', 'name']):
                    try:
                        windows.append({"hwnd": proc.info['pid'], "title": proc.info['name'], "pid": proc.info['pid']})
                    except Exception:
                        pass
                return _result(True, f"{len(windows)} processes found.", windows)
        return _result(False, "Window listing is not available on this platform.")

    def get_active_window(self) -> dict:
        if _IS_WINDOWS and _WIN32_OK:
            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd) if hwnd else ""
            return _result(True, title or "(no title)", {"hwnd": hwnd, "title": title})
        elif _IS_LINUX:
            if shutil.which("xdotool"):
                try:
                    title = subprocess.check_output(["xdotool", "getactivewindow", "getwindowname"], text=True, errors="replace").strip()
                    return _result(True, title or "(no title)", {"hwnd": 0, "title": title})
                except Exception:
                    pass
            elif shutil.which("xprop"):
                try:
                    out = subprocess.check_output(["xprop", "-root", "_NET_ACTIVE_WINDOW"], text=True, errors="replace")
                    wid = out.split()[-1]
                    title_out = subprocess.check_output(["xprop", "-id", wid, "WM_NAME"], text=True, errors="replace")
                    title = title_out.split("=", 1)[-1].strip().strip('"')
                    return _result(True, title or "(no title)", {"hwnd": wid, "title": title})
                except Exception:
                    pass
        return _result(False, "Active window detection not available.")

    def _find_window(self, title_substring: str):
        """Case-insensitive partial match against open window titles."""
        needle = (title_substring or "").strip().lower()
        if not needle:
            return None
        if _IS_WINDOWS and _WIN32_OK:
            match = None
            def _enum_handler(hwnd, _):
                nonlocal match
                if match is not None:
                    return
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title and needle in title.lower():
                        match = hwnd
            win32gui.EnumWindows(_enum_handler, None)
            return match
        elif _IS_LINUX:
            if shutil.which("xdotool"):
                try:
                    out = subprocess.check_output(["xdotool", "search", "--name", needle], text=True, errors="replace")
                    wids = out.strip().splitlines()
                    if wids:
                        return wids[0]
                except Exception:
                    pass
        return None

    def minimize_window(self, title: str = "") -> dict:
        if _IS_WINDOWS and _WIN32_OK:
            hwnd = self._find_window(title) if title else win32gui.GetForegroundWindow()
            if not hwnd:
                return _result(False, f"'{title}' naam ki koi window nahi mili.")
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            return _result(True, f"'{win32gui.GetWindowText(hwnd)}' minimize kar diya.")
        elif _IS_LINUX:
            if shutil.which("xdotool"):
                try:
                    if title:
                        subprocess.run(["xdotool", "search", "--name", title, "windowminimize"], check=True)
                    else:
                        subprocess.run(["xdotool", "getactivewindow", "windowminimize"], check=True)
                    return _result(True, f"'{title or 'Active window'}' minimize kar diya.")
                except Exception as e:
                    return _result(False, f"Minimize fail: {e}")
            elif shutil.which("wmctrl") and title:
                try:
                    subprocess.run(["wmctrl", "-r", title, "-b", "add,hidden"], check=True)
                    return _result(True, f"'{title}' minimize kar diya.")
                except Exception as e:
                    return _result(False, f"Minimize fail: {e}")
        return _result(False, "Minimize functionality demands pywin32 (Windows) or xdotool/wmctrl (Linux).")

    def maximize_window(self, title: str = "") -> dict:
        if _IS_WINDOWS and _WIN32_OK:
            hwnd = self._find_window(title) if title else win32gui.GetForegroundWindow()
            if not hwnd:
                return _result(False, f"'{title}' naam ki koi window nahi mili.")
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            return _result(True, f"'{win32gui.GetWindowText(hwnd)}' maximize kar diya.")
        elif _IS_LINUX:
            if shutil.which("wmctrl"):
                try:
                    t = title if title else ":ACTIVE:"
                    subprocess.run(["wmctrl", "-r", t, "-b", "add,maximized_vert,maximized_horz"], check=True)
                    return _result(True, f"'{title or 'Active window'}' maximize kar diya.")
                except Exception as e:
                    return _result(False, f"Maximize fail: {e}")
        return _result(False, "Maximize functionality demands pywin32 (Windows) or wmctrl (Linux).")

    def close_window(self, title: str) -> dict:
        if _IS_WINDOWS and _WIN32_OK:
            hwnd = self._find_window(title)
            if not hwnd:
                return _result(False, f"'{title}' naam ki koi window nahi mili.")
            name = win32gui.GetWindowText(hwnd)
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return _result(True, f"'{name}' close kar diya.")
        elif _IS_LINUX:
            if shutil.which("wmctrl"):
                try:
                    subprocess.run(["wmctrl", "-c", title], check=True)
                    return _result(True, f"'{title}' close kar diya.")
                except Exception as e:
                    return _result(False, f"Close fail: {e}")
            elif shutil.which("xdotool"):
                try:
                    subprocess.run(["xdotool", "search", "--name", title, "windowclose"], check=True)
                    return _result(True, f"'{title}' close kar diya.")
                except Exception as e:
                    return _result(False, f"Close fail: {e}")
        return _result(False, "Close window functionality not available.")

    def switch_to_window(self, title: str) -> dict:
        """Bring a window to the foreground (like a targeted Alt-Tab)."""
        if _IS_WINDOWS and _WIN32_OK:
            hwnd = self._find_window(title)
            if not hwnd:
                return _result(False, f"'{title}' naam ki koi window nahi mili.")
            name = win32gui.GetWindowText(hwnd)
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                fg_hwnd = win32gui.GetForegroundWindow()
                fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0]
                target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
                if fg_thread != target_thread:
                    win32process.AttachThreadInput(fg_thread, target_thread, True)
                    win32gui.SetForegroundWindow(hwnd)
                    win32process.AttachThreadInput(fg_thread, target_thread, False)
                else:
                    win32gui.SetForegroundWindow(hwnd)
            except Exception as e:
                return _result(False, f"'{name}' pe switch nahi ho paaya: {e}")
            return _result(True, f"'{name}' pe switch kar diya.")
        elif _IS_LINUX:
            if shutil.which("wmctrl"):
                try:
                    subprocess.run(["wmctrl", "-a", title], check=True)
                    return _result(True, f"'{title}' pe switch kar diya.")
                except Exception as e:
                    return _result(False, f"Switch fail: {e}")
            elif shutil.which("xdotool"):
                try:
                    subprocess.run(["xdotool", "search", "--name", title, "windowactivate"], check=True)
                    return _result(True, f"'{title}' pe switch kar diya.")
                except Exception as e:
                    return _result(False, f"Switch fail: {e}")
        return _result(False, "Switch window functionality not available.")

    # ---------------- System stats ----------------
    def get_system_stats(self) -> dict:
        if not _PSUTIL_OK:
            return _result(False, "psutil install karo: pip install psutil")
        stats = {
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "ram_percent": psutil.virtual_memory().percent,
            "ram_used_mb": psutil.virtual_memory().used // (1024 ** 2),
            "ram_total_mb": psutil.virtual_memory().total // (1024 ** 2),
        }
        batt = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if batt is not None:
            stats["battery_percent"] = batt.percent
            stats["battery_plugged"] = batt.power_plugged
        else:
            stats["battery_percent"] = None
            stats["battery_plugged"] = None

        disk = psutil.disk_usage('/') if hasattr(psutil, "disk_usage") else None
        if disk is not None:
            stats["disk_percent"] = disk.percent
            stats["disk_used_gb"] = round(disk.used / (1024 ** 3), 1)
            stats["disk_total_gb"] = round(disk.total / (1024 ** 3), 1)
        else:
            stats["disk_percent"] = None

        gpu_info = self.get_gpu_usage()
        if gpu_info["success"]:
            stats["gpu_percent"] = gpu_info["data"]["gpu_percent"]
            stats["gpu_name"] = gpu_info["data"]["gpu_name"]
        else:
            stats["gpu_percent"] = None
            stats["gpu_name"] = None

        msg = f"CPU {stats['cpu_percent']:.0f}% · RAM {stats['ram_percent']:.0f}%"
        if stats["battery_percent"] is not None:
            msg += f" · Battery {stats['battery_percent']:.0f}%"
        if stats.get("disk_percent") is not None:
            msg += f" · Disk {stats['disk_percent']:.0f}%"
        if stats.get("gpu_percent") is not None:
            msg += f" · GPU {stats['gpu_percent']:.0f}%"
        return _result(True, msg, stats)

    # ---------------- GPU usage (cross-platform, NVIDIA only for now) ----------------
    def get_gpu_usage(self) -> dict:
        """Uses `nvidia-smi` since it's the one GPU query tool that ships
        identically on Windows and Linux with the NVIDIA driver — no OS branch
        needed. Non-NVIDIA GPUs (AMD/Intel) have no equally universal CLI, so
        this reports 'unavailable' for them rather than guessing."""
        if not shutil.which("nvidia-smi"):
            return _result(False, "GPU stats sirf NVIDIA GPUs par available hain (nvidia-smi nahi mila).")
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                text=True, timeout=5, stderr=subprocess.DEVNULL,
            )
            first_line = out.strip().splitlines()[0]
            name, util, mem_used, mem_total = [p.strip() for p in first_line.split(",")]
            data = {
                "gpu_name": name,
                "gpu_percent": float(util),
                "gpu_mem_used_mb": float(mem_used),
                "gpu_mem_total_mb": float(mem_total),
            }
            return _result(True, f"{name}: {util}% util", data)
        except Exception as e:
            _log_error("get_gpu_usage", e)
            return _result(False, f"GPU stats read nahi ho paaye: {e}")

    # ---------------- Wi-Fi status ----------------
    def get_wifi_status(self) -> dict:
        if _IS_WINDOWS:
            try:
                out = subprocess.check_output(
                    ["netsh", "wlan", "show", "interfaces"], text=True, errors="replace", timeout=5)
                state_m = re.search(r"^\s*State\s*:\s*(.+)$", out, re.MULTILINE)
                ssid_m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.MULTILINE)
                signal_m = re.search(r"^\s*Signal\s*:\s*(.+)$", out, re.MULTILINE)
                connected = bool(state_m and "connected" in state_m.group(1).lower())
                data = {
                    "connected": connected,
                    "ssid": ssid_m.group(1).strip() if ssid_m else None,
                    "signal": signal_m.group(1).strip() if signal_m else None,
                }
                msg = f"Wi-Fi connected to '{data['ssid']}' ({data['signal']})" if connected else "Wi-Fi disconnected hai."
                return _result(True, msg, data)
            except Exception as e:
                _log_error("get_wifi_status", e)
                return _result(False, f"Wi-Fi status check nahi ho paaya: {e}")
        elif _IS_LINUX:
            if shutil.which("nmcli"):
                try:
                    out = subprocess.check_output(
                        ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
                        text=True, timeout=5)
                    for line in out.strip().splitlines():
                        parts = line.split(":")
                        if len(parts) >= 3 and parts[0].lower() == "yes":
                            data = {"connected": True, "ssid": parts[1], "signal": f"{parts[2]}%"}
                            return _result(True, f"Wi-Fi connected to '{parts[1]}' ({parts[2]}%)", data)
                    return _result(True, "Wi-Fi disconnected hai.", {"connected": False, "ssid": None, "signal": None})
                except Exception as e:
                    _log_error("get_wifi_status", e)
                    return _result(False, f"Wi-Fi status check nahi ho paaya: {e}")
            elif shutil.which("iwconfig"):
                try:
                    out = subprocess.check_output(["iwconfig"], text=True, errors="replace",
                                                   stderr=subprocess.DEVNULL, timeout=5)
                    m = re.search(r'ESSID:"([^"]*)"', out)
                    connected = bool(m and m.group(1))
                    data = {"connected": connected, "ssid": m.group(1) if m else None, "signal": None}
                    msg = f"Wi-Fi connected to '{data['ssid']}'" if connected else "Wi-Fi disconnected hai."
                    return _result(True, msg, data)
                except Exception as e:
                    _log_error("get_wifi_status", e)
                    return _result(False, f"Wi-Fi status check nahi ho paaya: {e}")
        return _result(False, "Wi-Fi status ke liye 'nmcli' (Linux) ya netsh (Windows) chahiye.")

    # ---------------- Process listing / killing ----------------
    _PROTECTED_PROCESS_NAMES = {
        "system", "system idle process", "registry", "csrss.exe", "wininit.exe",
        "winlogon.exe", "services.exe", "lsass.exe", "smss.exe", "explorer.exe",
        "systemd", "init", "kthreadd", "dbus-daemon", "pulseaudio", "Xorg", "wayland",
    }

    def list_running_processes(self, sort_by: str = "memory", max_results: int = 40) -> dict:
        """Cross-platform process list via psutil (name, pid, cpu%, memory)."""
        if not _PSUTIL_OK:
            return _result(False, "Process listing ke liye 'pip install psutil' install karo.")
        procs = []
        try:
            for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status"]):
                try:
                    info = p.info
                    mem_mb = (info["memory_info"].rss / (1024 ** 2)) if info.get("memory_info") else 0.0
                    procs.append({
                        "pid": info["pid"],
                        "name": info["name"] or "(unknown)",
                        "cpu_percent": info.get("cpu_percent") or 0.0,
                        "memory_mb": round(mem_mb, 1),
                        "status": info.get("status", ""),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            _log_error("list_running_processes", e)
            return _result(False, f"Process list nahi mil paayi: {e}")

        key = "memory_mb" if sort_by not in ("cpu", "cpu_percent") else "cpu_percent"
        procs.sort(key=lambda x: x[key], reverse=True)
        procs = procs[:max_results]
        return _result(True, f"{len(procs)} process(es) mile.", procs)

    def kill_process(self, pid: int = None, name: str = None, force: bool = False) -> dict:
        """Terminate a process by pid or (case-insensitive) name. Refuses to
        touch known critical OS processes and the current ISHA process itself.
        Tries a graceful terminate() first; only escalates to kill() if `force`
        is set or the process doesn't respond within 3 seconds — this is the
        'safely' in 'kill unresponsive apps safely'."""
        if not _PSUTIL_OK:
            return _result(False, "Process kill ke liye 'pip install psutil' install karo.")
        if pid is None and not name:
            return _result(False, "Kill karne ke liye pid ya process name batao.")

        targets = []
        try:
            if pid is not None:
                targets = [psutil.Process(int(pid))]
            else:
                needle = name.strip().lower()
                targets = [p for p in psutil.process_iter(["pid", "name"])
                           if p.info.get("name", "").lower() == needle]
        except psutil.NoSuchProcess:
            return _result(False, f"PID {pid} ka koi process nahi mila (already band ho chuka hoga).")
        except Exception as e:
            _log_error("kill_process:lookup", e)
            return _result(False, f"Process dhoondhne me error: {e}")

        if not targets:
            return _result(False, f"'{name}' naam ka koi running process nahi mila.")

        current_pid = os.getpid()
        killed, refused, failed = [], [], []
        for proc in targets:
            try:
                pname = proc.name()
                ppid = proc.pid
            except Exception:
                continue
            if ppid == current_pid or ppid in (0, 1):
                refused.append(f"{pname} (pid {ppid}) — protected process, skip kiya.")
                continue
            if pname.lower() in self._PROTECTED_PROCESS_NAMES:
                refused.append(f"{pname} (pid {ppid}) — critical system process, skip kiya.")
                continue
            try:
                if force:
                    proc.kill()
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        proc.kill()
                killed.append(f"{pname} (pid {ppid})")
            except psutil.NoSuchProcess:
                killed.append(f"{pname} (pid {ppid}) — already band ho chuka tha.")
            except Exception as e:
                _log_error("kill_process:terminate", e)
                failed.append(f"{pname} (pid {ppid}): {e}")

        parts = []
        if killed:
            parts.append(f"Band kiya: {', '.join(killed)}.")
        if refused:
            parts.append(f"Skip kiya: {', '.join(refused)}.")
        if failed:
            parts.append(f"Fail hua: {', '.join(failed)}.")
        ok = bool(killed) and not failed
        return _result(ok or bool(killed), " ".join(parts) or "Kuch nahi hua.", {
            "killed": killed, "refused": refused, "failed": failed,
        })

    # ---------------- Power actions (sleep / restart / shutdown) ----------------
    def system_power_action(self, action: str, confirmed: bool = False) -> dict:
        """Sleep, restart, or shutdown — cross-platform. Mirrors the same
        CONFIRMATION_REQUIRED pattern as delete_file_safely: the caller
        (tool layer / UI) must get explicit user confirmation and pass
        confirmed=True, since these three actions can't be undone once
        they've started."""
        action = (action or "").strip().lower()
        if action not in ("sleep", "restart", "shutdown"):
            return _result(False, f"Unknown power action '{action}'. Use sleep, restart, ya shutdown.")
        if not confirmed:
            return _result(False, f"CONFIRMATION_REQUIRED: '{action}' requires explicit user confirmation.")

        try:
            if _IS_WINDOWS:
                cmds = {
                    "sleep":    ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                    "restart":  ["shutdown", "/r", "/t", "0"],
                    "shutdown": ["shutdown", "/s", "/t", "0"],
                }
                subprocess.run(cmds[action], check=True)
            elif _IS_LINUX:
                cmds = {
                    "sleep":    ["systemctl", "suspend"],
                    "restart":  ["systemctl", "reboot"],
                    "shutdown": ["systemctl", "poweroff"],
                }
                try:
                    subprocess.run(cmds[action], check=True)
                except Exception:
                    fallback = {
                        "sleep":    ["pm-suspend"],
                        "restart":  ["shutdown", "-r", "now"],
                        "shutdown": ["shutdown", "-h", "now"],
                    }
                    subprocess.run(fallback[action], check=True)
            else:
                return _result(False, f"'{action}' '{_OS_NAME}' par supported nahi hai.")
            verb = {"sleep": "Sleep mode me ja raha hoon", "restart": "Restart ho raha hoon",
                    "shutdown": "Shutdown ho raha hoon"}[action]
            return _result(True, f"{verb}…")
        except Exception as e:
            _log_error(f"system_power_action:{action}", e)
            return _result(False, f"'{action}' fail ho gaya: {e} (permissions check karo)")

    # ---------------- Mouse scroll (used by hand-gesture control) ----------------
    def scroll(self, direction: str, clicks: int = 1) -> dict:
        """Send a mouse-wheel event (cross-platform)."""
        clicks_val = max(1, int(clicks))
        if _IS_WINDOWS and _WIN32_OK:
            wheel_delta = 120 * clicks_val
            if direction == "down":
                wheel_delta = -wheel_delta
            elif direction != "up":
                return _result(False, f"Invalid scroll direction '{direction}'.")
            try:
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, wheel_delta, 0)
                return _result(True, f"Scrolled {direction}.")
            except Exception as e:
                return _result(False, f"Scroll fail ho gaya: {e}")
        elif _PYAUTOGUI_OK:
            try:
                amount = clicks_val * 5
                pyautogui.scroll(-amount if direction == "down" else amount)
                return _result(True, f"Scrolled {direction}.")
            except Exception as e:
                return _result(False, f"Scroll fail: {e}")
        return _result(False, "Scroll feature missing dependencies.")

    # ---------------- Browser automation ----------------
    def open_url(self, url: str) -> dict:
        if not (url.startswith("http://") or url.startswith("https://")):
            return _result(False, "Sirf http/https URLs allowed hain.")
        webbrowser.open(url)
        return _result(True, f"Browser me {url} khol diya.")

    def close_active_browser_tab(self) -> dict:
        """Close current browser tab (Ctrl+W)."""
        if _IS_WINDOWS and _WIN32_OK:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return _result(False, "Koi active window nahi mili.")
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc = psutil.Process(pid) if _PSUTIL_OK else None
                proc_name = proc.name().lower() if proc else ""
            except Exception:
                proc_name = ""
            if proc_name not in self.KNOWN_BROWSERS:
                return _result(False, "Active window ek browser nahi hai, tab close nahi kiya.")
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.15)
            VK_CONTROL, VK_W = 0x11, 0x57
            win32api.keybd_event(VK_CONTROL, 0, 0, 0)
            win32api.keybd_event(VK_W, 0, 0, 0)
            win32api.keybd_event(VK_W, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
            return _result(True, "Active browser tab close kar diya.")
        elif _PYAUTOGUI_OK:
            try:
                pyautogui.hotkey('ctrl', 'w')
                return _result(True, "Active browser tab close kar diya.")
            except Exception as e:
                return _result(False, f"Tab close fail: {e}")
        return _result(False, "Tab close functionality missing dependencies.")

    # ---------------- File-system operations ----------------
    def _safe_base_dirs(self):
        kf = isha_paths.known_folder
        return {
            "desktop": kf("desktop"),
            "documents": kf("documents"),
            "downloads": kf("downloads"),
            "home": Path.home(),
        }

    def create_folder_on_desktop(self, folder_name: str) -> dict:
        name = os.path.basename(str(folder_name).strip())  
        if not name or name in (".", ".."):
            return _result(False, "Invalid folder name.")
        target = self._safe_base_dirs()["desktop"] / name
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return _result(False, f"Folder nahi ban paaya: {e}")
        return _result(True, f"'{name}' folder Desktop par ban gaya.", str(target))

    def search_files(self, query: str, location: str = "home", max_results: int = 25) -> dict:
        """Search filenames within a whitelisted base directory."""
        bases = self._safe_base_dirs()
        base = bases.get(location, bases["home"])
        query = (query or "").strip()
        if not query:
            return _result(False, "Search query khaali hai.")
        pattern = query if any(ch in query for ch in "*?") else f"*{query}*"

        matches = []
        try:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fname in files:
                    if fnmatch.fnmatch(fname.lower(), pattern.lower()):
                        matches.append(str(Path(root) / fname))
                        if len(matches) >= max_results:
                            raise StopIteration
        except StopIteration:
            pass
        except Exception as e:
            return _result(False, f"Search fail ho gaya: {e}")

        if not matches:
            return _result(True, f"'{query}' se match karti koi file nahi mili.", [])
        return _result(True, f"{len(matches)} file(s) mili.", matches)

    # ---------------- Screen brightness ----------------
    def set_brightness(self, percent: int) -> dict:
        val = max(0, min(100, int(percent)))
        if _IS_WINDOWS:
            try:
                cmd = f"powershell (Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {val})"
                subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return _result(True, f"Brightness {val}% set kar di.")
            except Exception as e:
                return _result(False, f"Brightness change failed: {e}")
        elif _IS_LINUX:
            if shutil.which("brightnessctl"):
                try:
                    subprocess.run(["brightnessctl", "set", f"{val}%"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return _result(True, f"Brightness {val}% set kar di.")
                except Exception as e:
                    return _result(False, f"brightnessctl failed: {e}")
            elif shutil.which("xbacklight"):
                try:
                    subprocess.run(["xbacklight", "-set", str(val)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return _result(True, f"Brightness {val}% set kar di.")
                except Exception as e:
                    return _result(False, f"xbacklight failed: {e}")
            else:
                sys_backlight = Path("/sys/class/backlight")
                if sys_backlight.exists():
                    dirs = list(sys_backlight.glob("*"))
                    if dirs:
                        b_dir = dirs[0]
                        max_file = b_dir / "max_brightness"
                        cur_file = b_dir / "brightness"
                        if max_file.exists() and cur_file.exists():
                            try:
                                max_b = int(max_file.read_text().strip())
                                new_b = int(max_b * (val / 100.0))
                                cur_file.write_text(str(new_b))
                                return _result(True, f"Brightness {val}% set kar di.")
                            except Exception as e:
                                return _result(False, f"sysfs brightness write failed: {e}")
        return _result(False, "Brightness control is not supported on this device.")

    # ---------------- File Read / Write / Delete / Move ----------------
    def read_file_content(self, file_path: str, max_lines: int = 500) -> dict:
        target = Path(file_path).resolve()
        if not target.exists() or not target.is_file():
            return _result(False, f"File '{file_path}' does not exist.")
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            content = "\n".join(lines[:max_lines])
            if len(lines) > max_lines:
                content += f"\n... [{len(lines) - max_lines} lines truncated]"
            return _result(True, f"File '{target.name}' read successfully.", {"content": content, "path": str(target)})
        except Exception as e:
            return _result(False, f"Could not read file '{target.name}': {e}")

    def write_file_content(self, file_path: str, content: str) -> dict:
        target = Path(file_path).resolve()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return _result(True, f"Wrote content to '{target.name}' successfully.", {"path": str(target)})
        except Exception as e:
            return _result(False, f"Could not write file '{file_path}': {e}")

    def move_or_rename_file(self, source_path: str, dest_path: str) -> dict:
        src = Path(source_path).resolve()
        dst = Path(dest_path).resolve()
        if not src.exists():
            return _result(False, f"Source path '{source_path}' does not exist.")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return _result(True, f"Moved '{src.name}' to '{dst}'.", {"source": str(src), "destination": str(dst)})
        except Exception as e:
            return _result(False, f"Failed to move/rename file: {e}")

    def delete_file_safely(self, file_path: str, confirmed: bool = False) -> dict:
        target = Path(file_path).resolve()
        if not target.exists():
            return _result(False, f"File '{file_path}' does not exist.")
        home = Path.home().resolve()
        if target == home or target.parent == target or str(target) in ("/", "C:\\", "C:/"):
            return _result(False, "CRITICAL ERROR: Refusing to delete root or home directory!")
        if not confirmed:
            return _result(False, f"CONFIRMATION_REQUIRED: Permanent deletion of '{target}' requires explicit user confirmation.")
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return _result(True, f"Deleted '{target.name}' successfully.")
        except Exception as e:
            return _result(False, f"Failed to delete '{target.name}': {e}")

    # ---------------- Secure Terminal Execution ----------------
    def run_terminal_command(self, command: str) -> dict:
        cmd_str = (command or "").strip()
        if not cmd_str:
            return _result(False, "Command line is empty.")

        blacklisted = [
            r'rm\s+-rf\s+/', r'mkfs', r':\(\)\{\s*:\|:&\s*\};:',
            r'dd\s+if=', r'format\s+[c-z]:', r'chmod\s+-R\s+777\s+/'
        ]
        for pat in blacklisted:
            if re.search(pat, cmd_str, re.IGNORECASE):
                return _result(False, f"SECURITY GUARD: Command blocked due to high-risk pattern '{pat}'.")

        # Same structured validator as the run_terminal_command tool, so this
        # legacy entry point cannot bypass the blocklist.
        plan = isha_cmd.analyze_command(cmd_str)
        if plan.blocked:
            return _result(False, f"SECURITY GUARD: Command blocked ({plan.block_reason}).")
        res = isha_cmd.run_command(plan, timeout=60)
        if res.get("timed_out"):
            return _result(False, "Terminal command timed out after 60s.")
        if res.get("exit_code") is None:
            return _result(False, f"Execution error: {res.get('error')}")
        return _result(True, f"Executed command (exit code {res['exit_code']}).", {
            "exit_code": res["exit_code"], "output": res["output"][:4000]})


# ------------------------------------------------------------------
# File & Media Management Engine
# ------------------------------------------------------------------
class FileMediaManager:
    """Deep file/folder search, media-type recognition, and safe (trash-based)
    deletion. Every public method here is synchronous and meant to be run off
    the UI thread via OSTaskWorker, exactly like WindowsController's methods —
    that's what keeps a big Downloads-folder search from freezing the GUI."""

    _MEDIA_EXTENSIONS = {
        "image":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".tiff", ".heic"},
        "video":    {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
        "audio":    {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma"},
        "document": {".pdf", ".doc", ".docx", ".txt", ".md", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".rtf"},
        "archive":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
        "code":     {".py", ".js", ".ts", ".html", ".css", ".json", ".java", ".c", ".cpp", ".sh", ".bat"},
    }
    _SCAN_LIMIT = 50000  # safety cap so a huge/mounted drive can't hang a request indefinitely

    def _safe_base_dirs(self):
        kf = isha_paths.known_folder
        return {k: kf(k) for k in ("desktop", "documents", "downloads", "pictures",
                                     "music", "videos", "home")}

    def classify_file(self, path) -> str:
        """Returns one of: image, video, audio, document, archive, code, other."""
        ext = Path(path).suffix.lower()
        for category, exts in self._MEDIA_EXTENSIONS.items():
            if ext in exts:
                return category
        guessed, _ = mimetypes.guess_type(str(path))
        if guessed:
            top = guessed.split("/")[0]
            if top in ("image", "video", "audio"):
                return top
            if top == "text":
                return "document"
        return "other"

    def deep_search(self, query: str = "", location: str = "home", extensions=None,
                     media_type: str = None, min_size_mb: float = None, max_size_mb: float = None,
                     modified_within_days: int = None, max_results: int = 50) -> dict:
        """Recursive filename/extension/size/date search using os.scandir
        (faster than os.walk for large trees since it avoids a second stat()
        call per entry on most platforms). Runs entirely inside one
        OSTaskWorker call, so the UI thread never blocks even on a big tree."""
        bases = self._safe_base_dirs()
        base = bases.get(location, bases["home"])
        if not base.exists():
            return _result(False, f"'{location}' folder is machine par nahi mila.")

        query_norm = (query or "").strip().lower()
        ext_set = {("." + e.lstrip(".")).lower() for e in extensions} if extensions else None
        cutoff_ts = (time.time() - modified_within_days * 86400) if modified_within_days else None

        matches = []
        scanned = 0

        def _walk(dir_path: Path):
            nonlocal scanned
            try:
                with os.scandir(dir_path) as it:
                    for entry in it:
                        if len(matches) >= max_results:
                            return
                        scanned += 1
                        if scanned > self._SCAN_LIMIT:
                            return
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if not entry.name.startswith('.'):
                                    _walk(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue

                            name_lower = entry.name.lower()
                            if query_norm and query_norm not in name_lower:
                                continue
                            ext = Path(entry.name).suffix.lower()
                            if ext_set and ext not in ext_set:
                                continue
                            if media_type and self.classify_file(entry.name) != media_type:
                                continue

                            st = entry.stat(follow_symlinks=False)
                            size_mb = st.st_size / (1024 ** 2)
                            if min_size_mb is not None and size_mb < min_size_mb:
                                continue
                            if max_size_mb is not None and size_mb > max_size_mb:
                                continue
                            if cutoff_ts is not None and st.st_mtime < cutoff_ts:
                                continue

                            matches.append({
                                "path": entry.path,
                                "name": entry.name,
                                "size_mb": round(size_mb, 2),
                                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                                "type": self.classify_file(entry.name),
                            })
                        except (PermissionError, OSError):
                            continue
            except (PermissionError, OSError) as e:
                _log_error(f"deep_search:scandir:{dir_path}", e)

        try:
            _walk(base)
        except Exception as e:
            _log_error("deep_search", e)
            return _result(False, f"Search fail ho gaya: {e}")

        if not matches:
            return _result(True, "Koi matching file nahi mili.", [])
        return _result(True, f"{len(matches)} file(s) mili.", matches)

    def list_media_files(self, media_type: str, location: str = "home", max_results: int = 50) -> dict:
        """Convenience wrapper over deep_search for 'show me my videos/photos/etc'."""
        if media_type not in self._MEDIA_EXTENSIONS:
            return _result(False, f"Unknown media type '{media_type}'. Use: {', '.join(self._MEDIA_EXTENSIONS)}.")
        return self.deep_search(query="", location=location, media_type=media_type, max_results=max_results)

    def safe_delete(self, paths: list, confirmed: bool = False) -> dict:
        """Batch-delete files/folders. Uses send2trash (recoverable, cross-
        platform Recycle Bin / Trash) when available; only falls back to a
        permanent shutil/unlink delete if send2trash isn't installed, and
        says so explicitly in the result message so the user isn't surprised.
        A confirmed=True flag is REQUIRED regardless of batch size — this is
        the 'strict confirmation layer for batch deletions' requirement, and
        it also covers the single-file case for consistency with
        WindowsController.delete_file_safely."""
        if not paths:
            return _result(False, "Delete karne ke liye kam se kam ek file/folder path do.")
        if not confirmed:
            preview = "; ".join(str(p) for p in paths[:5])
            more = f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""
            return _result(False, f"CONFIRMATION_REQUIRED: Deleting {len(paths)} item(s) [{preview}{more}] "
                                   f"requires explicit user confirmation.")

        home = Path.home().resolve()
        deleted, refused, failed = [], [], []
        used_trash = _SEND2TRASH_OK

        for raw in paths:
            target = Path(raw).resolve()
            if not target.exists():
                failed.append(f"{raw} (does not exist)")
                continue
            if target == home or target.parent == target or str(target) in ("/", "C:\\", "C:/"):
                refused.append(f"{raw} — refusing to delete root/home directory.")
                continue
            try:
                if _SEND2TRASH_OK:
                    _send2trash_fn(str(target))
                else:
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                deleted.append(str(target))
            except Exception as e:
                _log_error("safe_delete", e)
                failed.append(f"{raw}: {e}")

        how = "Recycle Bin/Trash me bheja" if used_trash else "PERMANENTLY deleted (send2trash not installed)"
        parts = [f"{len(deleted)} item(s) {how}."]
        if refused:
            parts.append(f"Refused: {len(refused)}.")
        if failed:
            parts.append(f"Failed: {len(failed)}.")
        return _result(bool(deleted) and not failed, " ".join(parts), {
            "deleted": deleted, "refused": refused, "failed": failed, "used_trash": used_trash,
        })


# ------------------------------------------------------------------

class OSTaskWorker(QThread):
    # was pyqtSignal(dict): execute_tool returns a *str*, so emit() raised
    # TypeError and the user saw the error text instead of the tool result.
    task_finished = pyqtSignal(object)
    task_error    = pyqtSignal(str)

    def __init__(self, method, *args, **kwargs):
        super().__init__()
        self._method = method
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._method(*self._args, **self._kwargs)
            self.task_finished.emit(result)
        except Exception as e:
            self.task_error.emit(str(e))


# ------------------------------------------------------------------

class SystemStatsMonitor(QThread):
    stats_updated = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(self, controller: "WindowsController", interval: float = 2.0, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._interval = max(0.5, float(interval))
        self._running = False

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        while self._running:
            result = self._controller.get_system_stats()
            if result["success"]:
                self.stats_updated.emit(result["data"])
            else:
                self.error_occurred.emit(result["message"])
            # Sleep in small chunks so stop() takes effect quickly
            slept = 0.0
            while self._running and slept < self._interval:
                self.msleep(100)
                slept += 0.1

# ======================================================================

try:
    import cv2
    _CV2_OK = True
    _force_pyqt5_qt_plugin_path()   
except Exception:
    _CV2_OK = False

# MediaPipe ships two completely different hand APIs, and which one exists
# depends on the version, not on the Python version:
#
#   <= 0.10.21  mp.solutions.hands           (legacy; cp39-cp312 wheels only)
#   >= 0.10.30  mediapipe.tasks HandLandmarker (Tasks API; py3-none wheels,
#                                               so it installs on 3.13/3.14 too,
#                                               but `solutions` was deleted)
#
# ISHA supports both, and picks whichever is present. That is what makes hand
# control work on a modern Python without pinning the whole app to an old one.

mp        = None
mp_vision = None
_MPBaseOptions = None
_MP_LEGACY_OK = False
_MP_TASKS_OK  = False
_MEDIAPIPE_ERROR = ""

try:
    import mediapipe as mp
    _MP_LEGACY_OK = hasattr(mp, "solutions") and hasattr(mp.solutions, "hands")
    try:
        from mediapipe.tasks.python import vision as mp_vision
        from mediapipe.tasks.python import BaseOptions as _MPBaseOptions
        _MP_TASKS_OK = hasattr(mp_vision, "HandLandmarker")
    except Exception:
        _MP_TASKS_OK = False
    if not (_MP_LEGACY_OK or _MP_TASKS_OK):
        _MEDIAPIPE_ERROR = (
            f"mediapipe {getattr(mp, '__version__', '?')} imported but exposes neither "
            "the legacy solutions API nor the Tasks HandLandmarker. "
            "Fix: pip install -U mediapipe")
except ModuleNotFoundError:
    _MEDIAPIPE_ERROR = (
        "mediapipe installed nahi hai. Fix: pip install -U mediapipe   "
        "(0.10.30+ py3-none wheels deta hai, to Python 3.13/3.14 par bhi chal jaata hai)")
except Exception as e:
    _MEDIAPIPE_ERROR = f"mediapipe import failed: {e}"

_MEDIAPIPE_OK = _MP_LEGACY_OK or _MP_TASKS_OK

# Tasks API needs its model file on disk; ~7 MB, fetched once.
_HAND_MODEL_PATH = _RUNTIME_DIR / "hand_landmarker.task"
_HAND_MODEL_URLS = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task",
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task",
)


def hand_model_present() -> bool:
    try:
        return _HAND_MODEL_PATH.exists() and _HAND_MODEL_PATH.stat().st_size > 1_000_000
    except Exception:
        return False


def ensure_hand_model(status=None) -> str:
    """Download hand_landmarker.task once. Call from a worker thread."""
    if hand_model_present():
        return str(_HAND_MODEL_PATH)

    def say(msg):
        print(f"[Hand] {msg}")
        if status:
            try:
                status(msg)
            except Exception:
                pass

    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _HAND_MODEL_PATH.with_suffix(".part")
    last = None
    for url in _HAND_MODEL_URLS:
        try:
            say("Hand tracking model download ho raha hai (~7 MB, ek hi baar)…")
            req = urllib.request.Request(url, headers={"User-Agent": "ISHA"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            if tmp.stat().st_size < 1_000_000:
                raise RuntimeError(f"file bahut chhoti aayi ({tmp.stat().st_size} bytes)")
            tmp.replace(_HAND_MODEL_PATH)
            say("Hand model ready.")
            return str(_HAND_MODEL_PATH)
        except Exception as e:
            last = e
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
    raise RuntimeError(
        f"hand_landmarker.task download nahi ho paaya ({last}). "
        f"Manually download karke {_HAND_MODEL_PATH} par rakh do: {_HAND_MODEL_URLS[0]}")



# ------------------------------------------------------------------
# MediaPipe sidecar — the permanent fix for "no mediapipe for this Python"
#
# MediaPipe ships wheels only for Python 3.9-3.12 and runs roughly a year
# behind each new release, so on 3.13/3.14 there is nothing to install and
# waiting is not a fix. Rather than pinning the whole app to an old Python,
# ISHA keeps its own small Python 3.10-3.12 virtualenv purely for hand
# tracking and talks to it over a pipe.
#
# The sidecar stays deliberately dumb: it does camera capture + landmark
# detection and prints one JSON line per frame. Every bit of gesture logic
# (One-Euro filters, pinch thresholds, click cooldowns, mode debounce) stays
# in the main process, so nothing is duplicated and all the existing tuning
# is reused untouched.
# ------------------------------------------------------------------

_HAND_ENV_DIR    = _RUNTIME_DIR / "handenv"
_HAND_SIDECAR_PY = _RUNTIME_DIR / "isha_hand_sidecar.py"

_HAND_SIDECAR_SOURCE = r"""
# Auto-generated by ISHA. Runs inside ISHA's Python 3.10-3.12 hand env.
# Emits one JSON line per camera frame; ISHA's main process does the rest.
import sys, json, time, base64


def main() -> int:
    cam_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    preview   = (len(sys.argv) > 2 and sys.argv[2] == "1")

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    try:
        import cv2
        import mediapipe as mp
    except Exception as e:
        send({"error": "sidecar import failed: %s" % e})
        return 1
    if not hasattr(mp, "solutions"):
        send({"error": "mediapipe too new for the legacy solutions API; need 0.10.14"})
        return 1

    if sys.platform == "win32":
        cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(cam_index)
    if not cap.isOpened():
        send({"error": "webcam open nahi hua (index %d / permissions check karo)" % cam_index})
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    mp_hands = mp.solutions.hands
    hands = mp_hands.Hands(model_complexity=0, max_num_hands=1,
                           min_detection_confidence=0.6, min_tracking_confidence=0.5)
    draw = mp.solutions.drawing_utils
    send({"status": "camera ready"})

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.03)
                continue
            frame = cv2.flip(frame, 1)
            res = hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

            msg = {"lm": None}
            if res.multi_hand_landmarks:
                hand = res.multi_hand_landmarks[0]
                msg["lm"] = [[round(p.x, 5), round(p.y, 5), round(p.z, 5)]
                             for p in hand.landmark]
                if preview:
                    draw.draw_landmarks(frame, hand, mp_hands.HAND_CONNECTIONS)
            if preview:
                small = cv2.resize(frame, (320, 240))
                enc_ok, buf = cv2.imencode(".jpg", small,
                                           [int(cv2.IMWRITE_JPEG_QUALITY), 55])
                if enc_ok:
                    msg["jpg"] = base64.b64encode(buf.tobytes()).decode("ascii")
            send(msg)
            time.sleep(0.02)
    except (BrokenPipeError, KeyboardInterrupt, OSError):
        pass
    finally:
        try:
            cap.release()
        except Exception:
            pass
        try:
            hands.close()
        except Exception:
            pass
    return 0


sys.exit(main())
"""


def _hand_gesture_help(scroll_enabled: bool = False) -> str:
    parts = ["Hand control on — index finger se cursor",
             "thumb+index pinch = left click",
             "thumb+middle pinch = right click"]
    if scroll_enabled:
        parts.append("do ungli = scroll")
    return ", ".join(parts) + ". Haath neeche karo ya hatheli kholo to cursor ruk jaata hai."


def _hand_venv_python():
    p = (_HAND_ENV_DIR / ("Scripts/python.exe" if _IS_WINDOWS else "bin/python"))
    return p if p.exists() else None


_HAND_BASE_PY = "unset"      # cached; probing spawns subprocesses


def _find_hand_python(force: bool = False):
    """An interpreter in the 3.10-3.12 window, to build the venv from."""
    global _HAND_BASE_PY
    if _HAND_BASE_PY != "unset" and not force:
        return _HAND_BASE_PY

    candidates = []
    if (3, 10) <= sys.version_info[:2] <= (3, 12):
        candidates.append([sys.executable])          # already fine
    for ver in ("3.12", "3.11", "3.10"):
        exe = shutil.which(f"python{ver}")
        if exe:
            candidates.append([exe])
    if _IS_WINDOWS:
        launcher = shutil.which("py")
        if launcher:
            candidates += [[launcher, f"-{v}"] for v in ("3.12", "3.11", "3.10")]

    found = None
    for cand in candidates:
        try:
            r = subprocess.run(
                cand + ["-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=25)
            if r.returncode != 0:
                continue
            major, minor = (int(x) for x in r.stdout.strip().split(".")[:2])
            if (3, 10) <= (major, minor) <= (3, 12):
                found = cand
                break
        except Exception:
            continue
    _HAND_BASE_PY = found
    return found


def _hand_sidecar_possible() -> bool:
    return _hand_venv_python() is not None or _find_hand_python() is not None


def _hand_env_help() -> str:
    # The easy fix first: on 0.10.30+ mediapipe installs on every Python
    # version, so most people never need the sidecar at all.
    if not _MEDIAPIPE_OK:
        return ("Hand control ke liye MediaPipe chahiye. Chalao:\n"
                "    pip install -U mediapipe opencv-python-headless pyautogui\n"
                "MediaPipe 0.10.30+ har Python version par install hota hai "
                f"(aap {sys.version_info[0]}.{sys.version_info[1]} par ho), "
                "to yeh kaam kar jaana chahiye.\n"
                f"Detail: {_MEDIAPIPE_ERROR}")
    if _IS_WINDOWS:
        how = ("Python 3.12 install karo (winget install Python.Python.3.12, "
               "ya python.org se), phir hand control dobara on karo — "
               "ISHA baaki sab khud kar legi.")
    elif _IS_MACOS:
        how = ("brew install python@3.12 chalao, phir hand control dobara on karo — "
               "ISHA baaki sab khud kar legi.")
    else:
        how = ("sudo apt install python3.12 python3.12-venv chalao (ya deadsnakes PPA se), "
               "phir hand control dobara on karo — ISHA baaki sab khud kar legi.")
    return (f"Hand control ke liye Python 3.10-3.12 chahiye. Aap Python "
            f"{sys.version_info[0]}.{sys.version_info[1]} par ho aur MediaPipe ka koi build "
            f"iske liye nahi hai.\n{how}\n"
            f"ISHA ka main app isi Python par chalta rahega — sirf camera wala hissa "
            f"alag env mein chalega.")


def _hand_env_ready(py) -> bool:
    try:
        r = subprocess.run([str(py), "-c", "import mediapipe, cv2"],
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return False


def _write_hand_sidecar():
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    want = _HAND_SIDECAR_SOURCE.lstrip("\n")
    try:
        if (not _HAND_SIDECAR_PY.exists()
                or _HAND_SIDECAR_PY.read_text(encoding="utf-8") != want):
            _HAND_SIDECAR_PY.write_text(want, encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"sidecar script likha nahi ja saka: {e}")
    return _HAND_SIDECAR_PY


def ensure_hand_env(status=None):
    """Build (once) and return the path to the hand env's python.

    Takes a few minutes the very first time and nothing thereafter, so it must
    be called from a worker thread, never the UI thread."""
    def say(msg):
        print(f"[Hand] {msg}")
        if status:
            try:
                status(msg)
            except Exception:
                pass

    vpy = _hand_venv_python()
    if vpy and _hand_env_ready(vpy):
        return str(vpy)

    base = _find_hand_python()
    if not base:
        return None

    if vpy is None:
        say("Hand control env bana raha hoon (pehli baar, 2-4 min)…")
        _HAND_ENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(base + ["-m", "venv", str(_HAND_ENV_DIR)],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError("venv nahi bana: " + ((r.stderr or r.stdout or "")[-250:]).strip())
        vpy = _hand_venv_python()
        if vpy is None:
            raise RuntimeError("venv bana lekin uska python nahi mila")

    say("MediaPipe download ho raha hai (ek hi baar hoga)…")
    # headless OpenCV on purpose: the sidecar never opens a window, and the
    # non-headless build drags in its own conflicting copy of Qt.
    r = subprocess.run(
        [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "-q",
         "mediapipe==0.10.14", "opencv-python-headless", "numpy<2"],
        capture_output=True, text=True, timeout=2400)
    if r.returncode != 0:
        raise RuntimeError("pip install fail: " + ((r.stderr or r.stdout or "")[-300:]).strip())
    if not _hand_env_ready(vpy):
        raise RuntimeError("install ke baad bhi mediapipe import nahi hua")
    say("Hand env ready.")
    return str(vpy)


# ------------------------------------------------------------------

try:
    import pyautogui
    pyautogui.FAILSAFE = False   
    pyautogui.PAUSE = 0          
    _PYAUTOGUI_OK = True
except (Exception, SystemExit):
    _PYAUTOGUI_OK = False

_SCREENSHOT_DIR = Path.home() / "Pictures" / "ISHA_Screenshots"


def hand_move_mouse(screen_fx: float, screen_fy: float) -> None:
    """screen_fx/fy are 0..1 fractions of the full screen (already mapped
    from the camera's active region by the worker). Cheap + no return value
    since this fires on every tracked frame."""
    if not _PYAUTOGUI_OK:
        return
    try:
        sw, sh = pyautogui.size()
        x = int(max(0, min(1, screen_fx)) * (sw - 1))
        y = int(max(0, min(1, screen_fy)) * (sh - 1))
        pyautogui.moveTo(x, y, duration=0)
    except Exception as e:
        print(f"[HandMouse] move failed: {e}")


def hand_left_click() -> dict:
    if not _PYAUTOGUI_OK:
        return _result(False, "pyautogui install karo: pip install pyautogui")
    try:
        pyautogui.click(button="left")
        return _result(True, "Left click.")
    except Exception as e:
        return _result(False, f"Left click fail: {e}")


def hand_right_click() -> dict:
    if not _PYAUTOGUI_OK:
        return _result(False, "pyautogui install karo: pip install pyautogui")
    try:
        pyautogui.click(button="right")
        return _result(True, "Right click.")
    except Exception as e:
        return _result(False, f"Right click fail: {e}")


def hand_scroll(direction: str, amount: int = 6) -> dict:
    if not _PYAUTOGUI_OK:
        return _result(False, "pyautogui install karo: pip install pyautogui")
    try:
        pyautogui.scroll(amount if direction == "up" else -amount)
        return _result(True, f"Scrolled {direction}.")
    except Exception as e:
        return _result(False, f"Scroll fail: {e}")


def hand_take_screenshot() -> dict:
    if not _PYAUTOGUI_OK:
        return _result(False, "pyautogui install karo: pip install pyautogui")
    try:
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        fname = datetime.now().strftime("isha_%Y%m%d_%H%M%S.png")
        fpath = _SCREENSHOT_DIR / fname
        img = pyautogui.screenshot()
        img.save(str(fpath))
        return _result(True, f"Screenshot saved: {fpath}", {"path": str(fpath)})
    except Exception as e:
        hint = ""
        if _IS_LINUX:
            hint = " (Linux par 'sudo apt install scrot' try karo agar yeh fail ho raha hai.)"
        return _result(False, f"Screenshot fail: {e}{hint}")


def hand_shutdown_system() -> dict:
    """Actually powers the machine off. Callers MUST confirm with the user
    first (see the confirmation dialog in ISHAWindow._on_gesture_action) —
    this function itself does not ask, it just executes."""
    try:
        if _IS_WINDOWS:
            subprocess.run(["shutdown", "/s", "/t", "0"], check=True)
            return _result(True, "Shutting down…")
        elif _IS_LINUX:
            
            try:
                subprocess.run(["systemctl", "poweroff"], check=True)
                return _result(True, "Shutting down…")
            except Exception:
                
                subprocess.run(["shutdown", "-h", "now"], check=True)
                return _result(True, "Shutting down…")
        else:
            return _result(False, f"Shutdown '{_OS_NAME}' par supported nahi hai.")
    except Exception as e:
        return _result(False, f"Shutdown fail ho gaya: {e} (permissions check karo)")


class _OneEuroFilter:
    """Adaptive low-pass filter for noisy, real-time 1D signals (Casiez et al., 2012).
    Heavier smoothing when the tracked point is nearly still (kills the fine jitter
    MediaPipe landmarks have even on a steady hand), lighter smoothing (less lag)
    once it starts moving quickly — a trade-off a fixed-rate exponential moving
    average can't make on its own, since one smoothing factor can't be right for
    both a stationary hand and a fast swipe at the same time."""

    def __init__(self, min_cutoff: float = 1.2, beta: float = 0.02, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * max(cutoff, 1e-6))
        return 1.0 / (1.0 + tau / max(dt, 1e-6))

    def reset(self):
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None

    def filter(self, x: float, t: float) -> float:
        if self._t_prev is None:
            self._x_prev = x
            self._t_prev = t
            return x
        dt = max(t - self._t_prev, 1e-6)
        self._t_prev = t

        dx = (x - self._x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1 - a_d) * self._dx_prev
        self._dx_prev = dx_hat

        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1 - a) * self._x_prev
        self._x_prev = x_hat
        return x_hat


class HandGestureWorker(QThread):
    gesture_action = pyqtSignal(str, dict)
    frame_ready    = pyqtSignal(QImage)   
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    FRAME_DELAY_MS       = 30     

    ACTIVE_X_MIN, ACTIVE_X_MAX = 0.18, 0.82
    ACTIVE_Y_MIN, ACTIVE_Y_MAX = 0.15, 0.85
    CURSOR_SMOOTHING     = 0.45   

    PINCH_CLOSE          = 0.045  
    PINCH_OPEN           = 0.075  

    SCROLL_JITTER_GUARD  = 0.008  

    CLICK_COOLDOWN_SEC   = 0.25   # min gap between two fires of the same pinch-click (stops double-fire on tremor)
    GESTURE_STABLE_FRAMES = 2     # consecutive matching frames required before switching gesture "mode"

    def __init__(self, camera_index: int = 0, emit_preview: bool = True,
                 scroll_enabled: bool = False, parent=None):
        super().__init__(parent)
        self.camera_index = camera_index
        self.emit_preview = emit_preview
        # Two-finger scroll is opt-in; the default gesture set is mouse-only.
        self.scroll_enabled = bool(scroll_enabled)
        self._running = False

        self._scroll_prev_y = None
        self._smooth_x = None
        self._smooth_y = None
        self._left_pinched = False
        self._right_pinched = False

        # Smoothing fixes: adaptive per-axis filters instead of one fixed-rate EMA
        self._filter_x = _OneEuroFilter(min_cutoff=1.2, beta=0.02)
        self._filter_y = _OneEuroFilter(min_cutoff=1.2, beta=0.02)
        self._filter_scroll_y = _OneEuroFilter(min_cutoff=1.0, beta=0.01)
        # Gesture-mode debounce: don't switch modes on a single noisy frame
        self._pending_mode = None
        self._pending_mode_count = 0
        self._active_mode = None
        # Click-fire cooldown: stops a pinch held right at the threshold from double-firing
        self._last_left_click_ts = 0.0
        self._last_right_click_ts = 0.0

    def stop(self):
        self._running = False

    def _reset_gesture_state(self):
        self._scroll_prev_y = None
        self._smooth_x = None
        self._smooth_y = None
        self._left_pinched = False
        self._right_pinched = False
        self._filter_x.reset()
        self._filter_y.reset()
        self._filter_scroll_y.reset()
        self._pending_mode = None
        self._pending_mode_count = 0
        self._active_mode = None

    # ------------------------------------------------------------
    def run(self):
        if not _CV2_OK:
            self.error_occurred.emit("OpenCV missing. Install: pip install opencv-python")
            return
        if not _MP_LEGACY_OK:
            self.error_occurred.emit(
                "MediaPipe legacy API not usable: "
                + (_MEDIAPIPE_ERROR or "install with 'pip install -U mediapipe'"))
            return
        if not _PYAUTOGUI_OK:
            self.error_occurred.emit("pyautogui missing. Install: pip install pyautogui")
            return

        cap = None
        hands = None
        try:
            
            if _IS_WINDOWS:
                cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            else:
                cap = cv2.VideoCapture(self.camera_index)
            if not cap.isOpened():
                self.error_occurred.emit("Webcam open nahi ho paaya (index/permissions check karo).")
                return

            mp_hands = mp.solutions.hands
            hands = mp_hands.Hands(
                model_complexity=0,          
                max_num_hands=1,
                min_detection_confidence=0.6,
                min_tracking_confidence=0.5,
            )
            mp_draw = mp.solutions.drawing_utils

            self._reset_gesture_state()

            self._running = True
            self.status_changed.emit(_hand_gesture_help(self.scroll_enabled))
            while self._running:
                ok, frame = cap.read()
                if not ok:
                    self.msleep(self.FRAME_DELAY_MS)
                    continue

                frame = cv2.flip(frame, 1)  
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                results = hands.process(rgb)

                if results.multi_hand_landmarks:
                    landmarks = results.multi_hand_landmarks[0]
                    if self.emit_preview:
                        mp_draw.draw_landmarks(frame, landmarks, mp_hands.HAND_CONNECTIONS)
                    self._interpret_gesture(landmarks.landmark)
                else:
                    self._reset_gesture_state()

                if self.emit_preview:
                    self._emit_frame(frame)

                self.msleep(self.FRAME_DELAY_MS)
        except Exception as e:
            self.error_occurred.emit(f"Hand control error: {e}")
        finally:
            if cap is not None:
                cap.release()
            if hands is not None:
                hands.close()
            self.status_changed.emit("Hand control stopped.")

    # ------------------------------------------------------------
    def _emit_frame(self, bgr_frame):
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)

        self.frame_ready.emit(qimg.copy())

    def _extended_fingers(self, lm) -> int:
        """Counts extended fingers among index/middle/ring/pinky only
        (thumb skipped — its extended/curled test depends on handedness
        and is unreliable enough to just leave out of a simple gesture set)."""
        tip_pip_pairs = [(8, 6), (12, 10), (16, 14), (20, 18)]
        return sum(1 for tip, pip in tip_pip_pairs if lm[tip].y < lm[pip].y)

    @staticmethod
    def _dist(a, b) -> float:
        return math.hypot(a.x - b.x, a.y - b.y)

    def _update_cursor_and_clicks(self, lm):
        """Index fingertip drives the cursor; thumb-pinches drive clicks.
        Runs whenever the hand is in the 1-finger (pointing) pose."""
        index_tip = lm[8]

        rx = (index_tip.x - self.ACTIVE_X_MIN) / (self.ACTIVE_X_MAX - self.ACTIVE_X_MIN)
        ry = (index_tip.y - self.ACTIVE_Y_MIN) / (self.ACTIVE_Y_MAX - self.ACTIVE_Y_MIN)
        rx = max(0.0, min(1.0, rx))
        ry = max(0.0, min(1.0, ry))

        now = time.time()
        self._smooth_x = max(0.0, min(1.0, self._filter_x.filter(rx, now)))
        self._smooth_y = max(0.0, min(1.0, self._filter_y.filter(ry, now)))

        self.gesture_action.emit("mouse_move", {"fx": self._smooth_x, "fy": self._smooth_y})

        d_left = self._dist(lm[4], lm[8])
        if d_left < self.PINCH_CLOSE and not self._left_pinched:
            self._left_pinched = True
            if now - self._last_left_click_ts >= self.CLICK_COOLDOWN_SEC:
                self._last_left_click_ts = now
                self.gesture_action.emit("left_click", {})
        elif d_left > self.PINCH_OPEN:
            self._left_pinched = False

        d_right = self._dist(lm[4], lm[12])
        if d_right < self.PINCH_CLOSE and not self._right_pinched:
            self._right_pinched = True
            if now - self._last_right_click_ts >= self.CLICK_COOLDOWN_SEC:
                self._last_right_click_ts = now
                self.gesture_action.emit("right_click", {})
        elif d_right > self.PINCH_OPEN:
            self._right_pinched = False

    def _interpret_gesture(self, lm):
        raw_extended = self._extended_fingers(lm)

        # Debounce: a lone noisy frame reporting a different finger count shouldn't
        # instantly flip the active mode (e.g. cursor <-> scroll flicker). Require
        # GESTURE_STABLE_FRAMES consecutive matching reads before switching.
        if raw_extended != self._pending_mode:
            self._pending_mode = raw_extended
            self._pending_mode_count = 1
        else:
            self._pending_mode_count += 1

        if self._active_mode is None or (
            raw_extended != self._active_mode and self._pending_mode_count >= self.GESTURE_STABLE_FRAMES
        ):
            self._active_mode = raw_extended

        extended = self._active_mode

        if extended == 1:
            # Index finger up -> cursor moves, thumb pinches click.
            self._scroll_prev_y = None
            self._update_cursor_and_clicks(lm)

        elif extended == 2 and self.scroll_enabled:
            # Two fingers -> scroll wheel. Off unless explicitly enabled.
            self._filter_x.reset()
            self._filter_y.reset()
            self._smooth_x = self._smooth_y = None
            self._left_pinched = self._right_pinched = False
            smoothed_y = self._filter_scroll_y.filter(lm[0].y, time.time())
            if self._scroll_prev_y is not None:
                delta = smoothed_y - self._scroll_prev_y
                if abs(delta) > self.SCROLL_JITTER_GUARD:
                    self.gesture_action.emit(
                        "scroll", {"direction": "down" if delta > 0 else "up"})
            self._scroll_prev_y = smoothed_y

        else:
            # Any other pose (flat palm, fist, three fingers) parks the cursor.
            # Lower your hand or open your palm and nothing happens.
            self._reset_gesture_state()


# ======================================================================
# MERGED MODULE 3/3: isha_tools.py  (LLM function-calling engine)
# ======================================================================

try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    _PYCAW_OK = True
except Exception:
    _PYCAW_OK = False

_os_controller = WindowsController()
_file_media_manager = FileMediaManager()

TOOL_REGISTRY: dict = {}

# ---- Song / music playback state (shared between tools + TTS worker) ----
_song_state_lock = threading.Lock()
_current_song_sound = None      # pygame.mixer.Sound currently loaded
_current_song_channel = None    # pygame.mixer.Channel it's playing on
_current_song_name = None       # display name, for confirmations


def tool(name: str, description: str, parameters: dict = None):
    """Decorator that registers a function as an LLM-callable tool."""
    def _wrap(func):
        TOOL_REGISTRY[name] = {
            "description": description,
            "parameters": parameters or {},
            "handler": func,
        }
        return func
    return _wrap


# ------------------------------------------------------------------
# 2. Built-in tools
# ------------------------------------------------------------------
@tool("open_notepad", "Open Text Editor / Notepad.")
def _open_notepad(args: dict) -> str:
    if _IS_WINDOWS:
        subprocess.Popen(["notepad.exe"])
        return "Notepad khol diya."
    else:
        for app in ["gedit", "kate", "mousepad", "xed", "gnome-text-editor", "leafpad"]:
            if shutil.which(app):
                subprocess.Popen([app])
                return f"Text editor ({app}) khol diya."
        return "Koi text editor is system par install nahi mila (gedit/kate/mousepad try karo)."


@tool("open_calculator", "Open Calculator app.")
def _open_calculator(args: dict) -> str:
    if _IS_WINDOWS:
        subprocess.Popen(["calc.exe"])
        return "Calculator khol diya."
    else:
        for app in ["gnome-calculator", "kcalc", "galculator", "xcalc", "speedcrunch"]:
            if shutil.which(app):
                subprocess.Popen([app])
                return f"Calculator ({app}) khol diya."
        return "Koi calculator is system par install nahi mila (gnome-calculator/kcalc try karo)."


@tool(
    "open_browser",
    "Open the default web browser, optionally navigating to a URL.",
    {"url": {"type": "string", "required": False,
              "description": "Full https:// URL to open. Omit for a blank browser window."}},
)
def _open_browser(args: dict) -> str:
    url = args.get("url") or "https://www.google.com"
    if not (url.startswith("http://") or url.startswith("https://")):
        return "Sirf http/https URLs allowed hain, is request ko reject kar diya."
    webbrowser.open(url)
    return f"Browser me {url} khol diya."


@tool("open_task_manager", "Open System Monitor / Task Manager.")
def _open_task_manager(args: dict) -> str:
    if _IS_WINDOWS:
        try:
            if hasattr(os, "startfile"):
                os.startfile("taskmgr.exe")
            else:
                subprocess.Popen(["taskmgr.exe"])
        except Exception:
            windir = os.environ.get("WINDIR", r"C:\Windows")
            subprocess.Popen([os.path.join(windir, "System32", "taskmgr.exe")])
        return "Task Manager khol diya."
    else:
        for app in ["gnome-system-monitor", "ksysguard", "mate-system-monitor", "xfce4-taskmanager", "system-monitoring-center"]:
            if shutil.which(app):
                subprocess.Popen([app])
                return f"System Monitor ({app}) khol diya."
        return "Koi System Monitor is system par install nahi mila (gnome-system-monitor try karo)."


# ------------------------------------------------------------------
# Generic app / shell-location launcher
#
# The old code only had open_notepad / open_calculator / open_task_manager,
# so anything else phrased as "X kholo" had no tool to land on and the model
# apologised instead ("I do not have the capability to open the Recycle Bin").
# One generic launcher covers the whole surface.
# ------------------------------------------------------------------

# Windows "shell:" locations and protocol handlers. These are NOT .exe files,
# so shutil.which() can never find them - they have to be handed to
# explorer.exe or os.startfile as-is. The Recycle Bin lives here, which is
# exactly why it used to be unreachable.
_WIN_SHELL_TARGETS = {
    "recycle bin":      "shell:RecycleBinFolder",
    "recyclebin":       "shell:RecycleBinFolder",
    "bin":              "shell:RecycleBinFolder",
    "trash":            "shell:RecycleBinFolder",
    "dustbin":          "shell:RecycleBinFolder",
    "kachra":           "shell:RecycleBinFolder",
    "downloads":        "shell:Downloads",
    "documents":        "shell:Personal",
    "pictures":         "shell:My Pictures",
    "music":            "shell:My Music",
    "videos":           "shell:My Video",
    "desktop":          "shell:Desktop",
    "this pc":          "shell:MyComputerFolder",
    "my computer":      "shell:MyComputerFolder",
    "startup":          "shell:Startup",
    "startup folder":   "shell:Startup",
    "fonts":            "shell:Fonts",
    "printers":         "shell:PrintersFolder",
    "network":          "shell:NetworkPlacesFolder",
    "settings":         "ms-settings:",
    "windows settings": "ms-settings:",
    "bluetooth":        "ms-settings:bluetooth",
    "wifi settings":    "ms-settings:network-wifi",
    "display settings": "ms-settings:display",
    "sound settings":   "ms-settings:sound",
    "apps settings":    "ms-settings:appsfeatures",
    "installed apps":   "ms-settings:appsfeatures",
    "windows update":   "ms-settings:windowsupdate",
    "battery settings": "ms-settings:batterysaver",
}

# Friendly name -> real executable. Covers the Windows built-ins whose actual
# names nobody types (mspaint, write, taskmgr, devmgmt.msc) plus common apps.
_WIN_APP_ALIASES = {
    "notepad": "notepad.exe", "note pad": "notepad.exe", "text editor": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "ms paint": "mspaint.exe",
    "wordpad": "write.exe",
    "task manager": "taskmgr.exe", "taskmanager": "taskmgr.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe", "terminal": "cmd.exe",
    "powershell": "powershell.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "file manager": "explorer.exe", "my files": "explorer.exe",
    "control panel": "control.exe", "control": "control.exe",
    "device manager": "devmgmt.msc", "disk management": "diskmgmt.msc",
    "services": "services.msc", "event viewer": "eventvwr.msc",
    "registry editor": "regedit.exe", "regedit": "regedit.exe",
    "snipping tool": "snippingtool.exe", "screenshot tool": "snippingtool.exe",
    "character map": "charmap.exe",
    "on screen keyboard": "osk.exe",
    "magnifier": "magnify.exe",
    "system info": "msinfo32.exe", "system information": "msinfo32.exe",
    "disk cleanup": "cleanmgr.exe",
    "resource monitor": "resmon.exe", "performance monitor": "perfmon.exe",
    "remote desktop": "mstsc.exe",
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe", "brave": "brave.exe", "opera": "opera.exe",
    "vscode": "code.exe", "vs code": "code.exe", "visual studio code": "code.exe",
    "spotify": "spotify.exe", "vlc": "vlc.exe", "discord": "discord.exe",
    "steam": "steam.exe", "telegram": "telegram.exe", "whatsapp": "whatsapp.exe",
    "word": "winword.exe", "ms word": "winword.exe",
    "excel": "excel.exe", "ms excel": "excel.exe",
    "powerpoint": "powerpnt.exe", "outlook": "outlook.exe",
}

_LINUX_APP_ALIASES = {
    "notepad":      ["gedit", "kate", "mousepad", "xed", "gnome-text-editor"],
    "text editor":  ["gedit", "kate", "mousepad", "xed", "gnome-text-editor"],
    "calculator":   ["gnome-calculator", "kcalc", "galculator", "xcalc"],
    "terminal":     ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"],
    "file manager": ["nautilus", "dolphin", "thunar", "nemo", "pcmanfm"],
    "explorer":     ["nautilus", "dolphin", "thunar", "nemo"],
    "task manager": ["gnome-system-monitor", "ksysguard", "xfce4-taskmanager"],
    "settings":     ["gnome-control-center", "systemsettings5", "xfce4-settings-manager"],
    "control panel":["gnome-control-center", "systemsettings5"],
    "chrome":       ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "firefox":      ["firefox", "firefox-esr"],
    "vscode":       ["code", "codium"], "vs code": ["code", "codium"],
    "paint":        ["kolourpaint", "pinta", "gimp"],
    "vlc": ["vlc"], "spotify": ["spotify"], "discord": ["discord"],
}


def _win_start_menu_lookup(name: str):
    """Fuzzy-search the Start Menu for a matching .lnk. This is what makes
    'X kholo' work for apps we have no alias for - installed programs are
    almost always shortcutted here even when their .exe is not on PATH."""
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    needle = name.strip().lower()
    if not needle:
        return None
    best, best_score = None, 0.0
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for lnk in root.rglob("*.lnk"):
                stem = lnk.stem.lower()
                if stem == needle:
                    return lnk
                score = difflib.SequenceMatcher(None, needle, stem).ratio()
                if needle in stem:
                    score = max(score, 0.9)
                if score > best_score:
                    best, best_score = lnk, score
        except Exception:
            continue
    return best if best_score >= 0.72 else None


def _open_app_windows(name: str, raw: str) -> str:
    # 1) shell: locations and ms-settings: URIs (Recycle Bin, Settings, ...)
    target = _WIN_SHELL_TARGETS.get(name)
    if target is None:
        near = difflib.get_close_matches(name, list(_WIN_SHELL_TARGETS), n=1, cutoff=0.82)
        if near:
            target = _WIN_SHELL_TARGETS[near[0]]
    if target:
        try:
            if target.startswith("shell:"):
                subprocess.Popen(["explorer.exe", target])
            else:
                os.startfile(target)
            return f"{raw} khol diya."
        except Exception as e:
            raise ToolError(f"'{raw}' khol nahi paayi: {e}")

    # 2) known executable aliases, then the literal name
    exe = _WIN_APP_ALIASES.get(name)
    if exe is None:
        near = difflib.get_close_matches(name, list(_WIN_APP_ALIASES), n=1, cutoff=0.78)
        if near:
            exe = _WIN_APP_ALIASES[near[0]]

    candidates = []
    if exe:
        candidates.append(exe)
    literal = name if name.lower().endswith((".exe", ".msc", ".cpl")) else name + ".exe"
    if literal not in candidates:
        candidates.append(literal)

    for cand in candidates:
        if cand.lower().endswith((".msc", ".cpl")):
            try:
                os.startfile(cand)
                return f"{raw} khol diya."
            except Exception:
                continue
        path = shutil.which(cand)
        if path:
            try:
                subprocess.Popen([path])
                return f"{raw} khol diya."
            except Exception:
                pass
        try:
            os.startfile(cand)
            return f"{raw} khol diya."
        except Exception:
            pass

    # 3) Start Menu shortcut search - catches everything else installed
    lnk = _win_start_menu_lookup(name)
    if lnk:
        try:
            os.startfile(str(lnk))
            return f"{lnk.stem} khol diya."
        except Exception:
            pass

    raise ToolError(f"'{raw}' naam ka koi app is PC par nahi mila. "
                    f"Exact naam ya poora .exe path do.")


def _open_app_linux(name: str, raw: str) -> str:
    if name in ("recycle bin", "trash", "dustbin", "bin", "kachra"):
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", "trash:///"])
            return "Trash khol diya."
        raise ToolError("xdg-open nahi mila, Trash khol nahi paayi.")

    for cand in _LINUX_APP_ALIASES.get(name, []):
        if shutil.which(cand):
            subprocess.Popen([cand])
            return f"{raw} ({cand}) khol diya."

    direct = name.replace(" ", "-")
    for cand in (name, direct):
        if shutil.which(cand):
            subprocess.Popen([cand])
            return f"{raw} khol diya."

    if shutil.which("gtk-launch"):
        try:
            subprocess.Popen(["gtk-launch", direct])
            return f"{raw} khol diya."
        except Exception:
            pass
    raise ToolError(f"'{raw}' naam ka koi app is system par nahi mila.")


_OPEN_NOISE_RE = re.compile(
    r"\b(app|application|window|program|software|folder|kholo|khol|kholdo|kholna|"
    r"do|dedo|karo|kardo|kar|open|opne|start|launch|chalu|chalao|dikhao|browser|"
    r"please|plz|isha|mera|meri|my|the|ek|ko|ka|ki)\b", re.IGNORECASE)


@tool(
    "open_app",
    "Open ANY application, system window, settings page or shell location on "
    "this PC by name - for example 'recycle bin', 'notepad', 'settings', "
    "'control panel', 'device manager', 'chrome', 'vscode', 'downloads'. "
    "Use this whenever the user asks to open / launch / start / kholo "
    "something and no more specific tool fits. You CAN open apps on this "
    "machine - never say you lack the capability, call this tool instead.",
    {"app_name": {"type": "string", "required": True,
                  "description": "Name of the app, folder or window to open, as the user said it."}},
)
def _open_app(args: dict) -> str:
    raw = str(args.get("app_name") or "").strip()
    if not raw:
        raise ToolError("Kis app ko kholna hai, naam batao.")
    name = _OPEN_NOISE_RE.sub(" ", raw)
    name = re.sub(r"\s+", " ", name).strip().lower()
    if not name:
        # "browser kholo" strips down to nothing - that is the default browser.
        if "browser" in raw.lower():
            webbrowser.open("https://www.google.com")
            return "Browser khol diya."
        name = raw.lower()

    if _IS_WINDOWS:
        return _open_app_windows(name, raw)
    if _IS_MACOS:
        try:
            subprocess.Popen(["open", "-a", raw])
            return f"{raw} khol diya."
        except Exception as e:
            raise ToolError(f"'{raw}' khol nahi paayi: {e}")
    return _open_app_linux(name, raw)


_SYSTEM_CONTROLLER = isha_sys.get_controller()


@tool("empty_recycle_bin",
      "Permanently empty the Windows Recycle Bin / Linux Trash / macOS Trash. "
      "Counts items before and after, and only reports success when the bin is verified empty.")
def _empty_recycle_bin(args: dict) -> str:
    # Uses the OS-native mechanism (SHEmptyRecycleBinW / freedesktop Trash /
    # Finder) and re-queries the bin afterwards: success is never assumed.
    res = _SYSTEM_CONTROLLER.empty_trash()
    if not res.get("success"):
        raise ToolError(res.get("message") or "Recycle Bin empty nahi hui.")
    return res["message"]


@tool("recycle_bin_status", "How many items / how much space is in the Recycle Bin (Trash) right now.")
def _recycle_bin_status(args: dict) -> str:
    res = _SYSTEM_CONTROLLER.trash_status()
    if not res.get("success"):
        raise ToolError(res.get("message"))
    return res["message"]



# ------------------------------------------------------------------
# Music / song playback
# ------------------------------------------------------------------
_MUSIC_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma")
_MUSIC_SCAN_LIMIT = 20000  # safety cap so a huge Downloads folder can't hang a request


def _music_search_dirs() -> list:
    home = Path.home()
    candidates = [isha_paths.known_folder("music"), isha_paths.known_folder("downloads"),
                  isha_paths.known_folder("desktop")]
    seen, out = set(), []
    for d in candidates:
        if d.exists() and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _find_local_song(query: str):
    """Fuzzy-match `query` against audio filenames under Music/Downloads/Desktop.
    Returns a Path to the best match, or None if nothing looked close enough."""
    q_norm = re.sub(r"[^a-z0-9]+", "", query.lower())
    if not q_norm:
        return None
    best_path, best_score = None, 0.0
    scanned = 0
    for base in _music_search_dirs():
        for root, _, files in os.walk(base):
            for fname in files:
                scanned += 1
                if scanned > _MUSIC_SCAN_LIMIT:
                    return best_path if best_score >= 0.62 else None
                if not fname.lower().endswith(_MUSIC_EXTS):
                    continue
                f_norm = re.sub(r"[^a-z0-9]+", "", Path(fname).stem.lower())
                if not f_norm:
                    continue
                if q_norm in f_norm or f_norm in q_norm:
                    return Path(root) / fname
                score = difflib.SequenceMatcher(None, q_norm, f_norm).ratio()
                if score > best_score:
                    best_score, best_path = score, Path(root) / fname
    return best_path if best_score >= 0.62 else None


def _is_song_playing() -> bool:
    """Used by TTSWorker so it doesn't kill a playing song when it speaks —
    pygame.mixer.quit() shuts the whole mixer down, not just the TTS voice."""
    try:
        return (
            _current_song_channel is not None
            and pygame.mixer.get_init() is not None
            and _current_song_channel.get_busy()
        )
    except Exception:
        return False


def _stop_current_song_unlocked():
    global _current_song_sound, _current_song_channel, _current_song_name
    if _current_song_sound is not None:
        try:
            _current_song_sound.stop()
        except Exception:
            pass
    _current_song_sound = None
    _current_song_channel = None
    _current_song_name = None


@tool(
    "play_song",
    "Play a song/gaana by name. Searches the local Music, Downloads and Desktop "
    "folders first for a matching audio file; if nothing matches, opens and plays "
    "the top YouTube result instead.",
    {"query": {"type": "string", "required": True,
               "description": "Song name and/or artist to play, e.g. 'Kesariya Arijit Singh'."}},
)
def _play_song(args: dict) -> str:
    global _current_song_sound, _current_song_channel, _current_song_name
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("Konsa gaana bajau? Naam batao, jaise 'Kesariya Arijit Singh'.")

    if _PYGAME_OK:
        local_path = _find_local_song(query)
        if local_path is not None:
            try:
                with _song_state_lock:
                    _stop_current_song_unlocked()
                    if not pygame.mixer.get_init():
                        pygame.mixer.init()
                    sound = pygame.mixer.Sound(str(local_path))
                    channel = sound.play()
                    _current_song_sound = sound
                    _current_song_channel = channel
                    _current_song_name = local_path.stem
                return f"'{local_path.stem}' local file se play kar raha hoon."
            except Exception as e:
                print(f"[ISHA] Local song playback failed, falling back to YouTube: {e}")

    # Fallback: play top YouTube result in the default browser.
    video_url = None
    if _REQUESTS_OK:
        try:
            resp = requests.get(
                "https://www.youtube.com/results",
                params={"search_query": query},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=6,
            )
            m = re.search(r'"videoId":"([\w-]{11})"', resp.text)
            if m:
                video_url = f"https://www.youtube.com/watch?v={m.group(1)}"
        except Exception:
            video_url = None
    if video_url:
        webbrowser.open(video_url)
        return f"'{query}' YouTube par play kar raha hoon."
    webbrowser.open(f"https://www.youtube.com/results?search_query={query}")
    return f"'{query}' ke liye YouTube search khol diya, wahan se play kar lo."


@tool("pause_song", "Pause the locally-playing song (does not affect a YouTube browser tab).")
def _pause_song(args: dict) -> str:
    with _song_state_lock:
        if _current_song_channel is not None and _current_song_channel.get_busy():
            _current_song_channel.pause()
            return "Gaana pause kar diya."
    return "Koi gaana chal nahi raha jise pause karu."


@tool("resume_song", "Resume a locally-playing song that was paused.")
def _resume_song(args: dict) -> str:
    with _song_state_lock:
        if _current_song_channel is not None:
            _current_song_channel.unpause()
            return "Gaana resume kar diya."
    return "Koi paused gaana nahi mila."


@tool("stop_song", "Stop the song currently playing locally (does not affect a YouTube browser tab).")
def _stop_song(args: dict) -> str:
    with _song_state_lock:
        name = _current_song_name
        was_playing = _current_song_sound is not None
        _stop_current_song_unlocked()
    if was_playing:
        return f"'{name}' band kar diya."
    return "Koi local gaana chal nahi raha tha."


@tool(
    "mute_system",
    "Mute or unmute the system audio output.",
    {"muted": {"type": "boolean", "required": True,
                "description": "true to mute, false to unmute"}},
)
def _mute_system(args: dict) -> str:
    muted = bool(args["muted"])
    if _IS_WINDOWS:
        if _PYCAW_OK:
            try:
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                volume = cast(interface, POINTER(IAudioEndpointVolume))
                volume.SetMute(1 if muted else 0, None)
                return "System mute kar diya." if muted else "System unmute kar diya."
            except Exception:
                pass
        try:
            if hasattr(ctypes, "windll"):
                VK_VOLUME_MUTE = 0xAD
                user32 = ctypes.windll.user32
                user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
                user32.keybd_event(VK_VOLUME_MUTE, 0, 2, 0)
                return "Mute toggle kar diya."
        except Exception:
            pass
    else:
        if shutil.which("pactl"):
            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "System mute kar diya." if muted else "System unmute kar diya."
        elif shutil.which("amixer"):
            subprocess.run(["amixer", "-D", "pulse", "set", "Master", "mute" if muted else "unmute"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "System mute kar diya." if muted else "System unmute kar diya."
    if _PYAUTOGUI_OK:
        pyautogui.press('volumemute')
        return "Mute toggle kar diya."
    return "Mute toggle action complete."


@tool(
    "set_volume",
    "Set the system master volume to an exact percentage.",
    {"percent": {"type": "integer", "required": True,
                  "description": "Volume level 0-100"}},
)
def _set_volume(args: dict) -> str:
    percent = max(0, min(100, int(args["percent"])))
    if _IS_WINDOWS:
        if _PYCAW_OK:
            try:
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                volume = cast(interface, POINTER(IAudioEndpointVolume))
                volume.SetMasterVolumeLevelScalar(percent / 100.0, None)
                return f"Volume {percent}% set kar diya."
            except Exception as e:
                return f"Volume set nahi ho paaya: {e}"
        return "Exact volume set karne ke liye 'pip install pycaw comtypes' install karo."
    else:
        if shutil.which("pactl"):
            subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Volume {percent}% set kar diya."
        elif shutil.which("amixer"):
            subprocess.run(["amixer", "-D", "pulse", "set", "Master", f"{percent}%"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Volume {percent}% set kar diya."
        elif shutil.which("wpctl"):
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent/100:.2f}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Volume {percent}% set kar diya."
        return f"Volume {percent}% request processed."


@tool("check_cpu_usage", "Get the current CPU usage percentage.")
def _check_cpu(args: dict) -> str:
    if not _PSUTIL_OK:
        return "CPU usage check karne ke liye 'pip install psutil' install karo."
    pct = psutil.cpu_percent(interval=0.4)
    return f"CPU abhi {pct:.0f}% use ho raha hai."


@tool("check_memory_usage", "Get current RAM usage.")
def _check_memory(args: dict) -> str:
    if not _PSUTIL_OK:
        return "RAM usage check karne ke liye 'pip install psutil' install karo."
    mem = psutil.virtual_memory()
    return f"RAM {mem.percent:.0f}% use ho rahi hai ({mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB)."


@tool("check_battery", "Get current battery percentage and charging status.")
def _check_battery(args: dict) -> str:
    if not _PSUTIL_OK or not hasattr(psutil, "sensors_battery"):
        return "Battery info is device pe available nahi hai."
    batt = psutil.sensors_battery()
    if batt is None:
        return "Koi battery detect nahi hui (desktop PC ho sakta hai)."
    state = "charging" if batt.power_plugged else "on battery"
    return f"Battery {batt.percent:.0f}% hai, {state}."


@tool("get_time", "Get the current local time.")
def _get_time(args: dict) -> str:
    return f"Abhi time hai {datetime.now().strftime('%I:%M %p')}."


@tool("get_date", "Get the current date.")
def _get_date(args: dict) -> str:
    return f"Aaj ki date hai {datetime.now().strftime('%A, %d %B %Y')}."


@tool("lock_screen", "Lock the workstation.")
def _lock_screen(args: dict) -> str:
    try:
        if _IS_WINDOWS and hasattr(ctypes, "windll"):
            ctypes.windll.user32.LockWorkStation()
            return "Screen lock kar di."
        elif _IS_LINUX:
            for cmd in [["xdg-screensaver", "lock"], ["gnome-screensaver-command", "-l"], ["loginctl", "lock-session"]]:
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return "Screen lock kar di."
        return "Screen lock is platform par executed."
    except Exception as e:
        return f"Screen lock nahi ho paayi: {e}"


# ---- Window management / browser tabs / file ops (isha_os_control.py) ----
def _require_os_control():
    if _IS_WINDOWS and not _WIN32_OK:
        raise RuntimeError("pywin32 not installed. Install: pip install pywin32")


@tool(
    "minimize_window",
    "Minimize a window. If title is omitted, minimizes the current active window.",
    {"title": {"type": "string", "required": False,
                "description": "Partial/substring of the window title, e.g. 'Notepad'"}},
)
def _minimize_window(args: dict) -> str:
    _require_os_control()
    res = _os_controller.minimize_window(args.get("title", ""))
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "maximize_window",
    "Maximize a window. If title is omitted, maximizes the current active window.",
    {"title": {"type": "string", "required": False,
                "description": "Partial/substring of the window title"}},
)
def _maximize_window(args: dict) -> str:
    _require_os_control()
    res = _os_controller.maximize_window(args.get("title", ""))
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "close_window",
    "Close a window by (partial) title.",
    {"title": {"type": "string", "required": True,
                "description": "Partial/substring of the window title, e.g. 'Chrome'"}},
)
def _close_window(args: dict) -> str:
    _require_os_control()
    res = _os_controller.close_window(args["title"])
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "switch_window",
    "Switch to (bring to foreground) a window by partial title, like a targeted Alt-Tab.",
    {"title": {"type": "string", "required": True,
                "description": "Partial/substring of the window title"}},
)
def _switch_window(args: dict) -> str:
    _require_os_control()
    res = _os_controller.switch_to_window(args["title"])
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "open_url",
    "Open a specific URL in the default web browser.",
    {"url": {"type": "string", "required": True, "description": "Full https:// URL"}},
)
def _open_url(args: dict) -> str:
    
    res = _os_controller.open_url(args["url"])
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool("close_browser_tab", "Close the current tab of the active browser window (Ctrl+W).")
def _close_browser_tab(args: dict) -> str:
    _require_os_control()
    res = _os_controller.close_active_browser_tab()
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "create_desktop_folder",
    "Create a new folder on the Desktop.",
    {"folder_name": {"type": "string", "required": True, "description": "Name of the new folder"}},
)
def _create_desktop_folder(args: dict) -> str:
        
    res = _os_controller.create_folder_on_desktop(args["folder_name"])
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "search_files",
    "Search for files by name inside a whitelisted user folder (Desktop, Documents, Downloads, or full home).",
    {
        "query": {"type": "string", "required": True,
                   "description": "Filename or pattern to search for, e.g. 'invoice' or '*.pdf'"},
        "location": {"type": "string", "required": False,
                      "description": "One of: desktop, documents, downloads, home (default: home)"},
    },
)
def _search_files(args: dict) -> str:
    
    res = _os_controller.search_files(args["query"], args.get("location", "home"))
    if not res["success"]:
        raise ToolError(res["message"])
    if res["data"]:
        preview = "; ".join(res["data"][:5])
        return f"{res['message']} Examples: {preview}"
    return res["message"]


@tool("get_system_stats", "Get current CPU, RAM, Battery, Disk, and GPU usage.")
def _get_system_stats(args: dict) -> str:
    res = _os_controller.get_system_stats()
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool("get_gpu_usage", "Get current GPU utilization and VRAM usage (NVIDIA GPUs only).")
def _get_gpu_usage(args: dict) -> str:
    res = _os_controller.get_gpu_usage()
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool("get_wifi_status", "Check whether Wi-Fi is connected, and to which network.")
def _get_wifi_status(args: dict) -> str:
    res = _os_controller.get_wifi_status()
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "list_running_processes",
    "List currently running processes/apps, sorted by memory or CPU usage.",
    {
        "sort_by": {"type": "string", "required": False,
                     "description": "'memory' (default) or 'cpu'"},
        "max_results": {"type": "integer", "required": False,
                          "description": "Max processes to return (default 40)."},
    },
)
def _list_running_processes(args: dict) -> str:
    res = _os_controller.list_running_processes(args.get("sort_by", "memory"), args.get("max_results", 40))
    if not res["success"]:
        raise ToolError(res["message"])
    top = res["data"][:10]
    preview = "; ".join(f"{p['name']} ({p['memory_mb']}MB, {p['cpu_percent']}% CPU)" for p in top)
    return f"{res['message']} Top: {preview}"


@tool(
    "kill_process",
    "Terminate/kill an unresponsive process safely, by PID or by name. Refuses to touch critical OS processes.",
    {
        "pid": {"type": "integer", "required": False, "description": "Process ID to kill."},
        "name": {"type": "string", "required": False, "description": "Process name to kill, e.g. 'chrome.exe'."},
        "force": {"type": "boolean", "required": False,
                   "description": "Skip the graceful terminate step and force-kill immediately."},
    },
)
def _kill_process(args: dict) -> str:
    res = _os_controller.kill_process(args.get("pid"), args.get("name"), args.get("force", False))
    killed = (res.get("data") or {}).get("killed")
    if not res["success"] and not killed:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "system_power_action",
    "Put the system to sleep, restart it, or shut it down. ALWAYS requires the caller to have already "
    "confirmed this with the user before setting confirm=true — this action cannot be undone once started.",
    {
        "action": {"type": "string", "required": True, "description": "One of: sleep, restart, shutdown."},
        "confirm": {"type": "boolean", "required": False, "description": "Must be true; set only after explicit user confirmation."},
    },
)
def _system_power_action(args: dict) -> str:
    res = _os_controller.system_power_action(args["action"], args.get("confirm", False))
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


_vector_memory = VectorMemoryEngine()


@tool(
    "set_brightness",
    "Set the display screen brightness to an exact percentage (0-100%).",
    {"percent": {"type": "integer", "required": True, "description": "Brightness percentage (0-100)"}},
)
def _set_brightness(args: dict) -> str:
    res = _os_controller.set_brightness(args["percent"])
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "run_terminal_command",
    "Execute a cross-platform terminal or shell command safely and capture output.",
    {"command": {"type": "string", "required": True, "description": "The exact shell command line string to execute."}},
)
def _run_terminal_command(args: dict) -> str:
    # Structured path: tokenise -> blocklist -> no shell unless the command
    # really uses shell operators -> timeout -> STOP-aware. The tool itself is
    # "critical", so the user has already seen plan.describe() and approved.
    plan = isha_cmd.analyze_command(args["command"])
    ACTION_LOG.record("command", command=plan.raw, shell=plan.needs_shell, read_only=plan.read_only,
                      blocked=plan.blocked, reason=plan.block_reason)
    if plan.blocked:
        raise ToolError(f"SECURITY GUARD: command blocked ({plan.block_reason}).")
    res = isha_cmd.run_command(plan, timeout=60)
    if res.get("error") and res.get("exit_code") is None:
        raise ToolError(f"Command nahi chala: {res['error']}")
    status = "OK" if res["success"] else f"exit code {res['exit_code']}" + (" (timed out)" if res.get("timed_out") else "")
    return f"Command [{status}] Output:\n{res['output'] or '(no output)'}"


@tool(
    "read_file_content",
    "Read the text content of a file from disk.",
    {
        "file_path": {"type": "string", "required": True, "description": "Path to the file to read."},
        "max_lines": {"type": "integer", "required": False, "description": "Max lines to read (default 500)."}
    },
)
def _read_file_content(args: dict) -> str:
    res = _os_controller.read_file_content(args["file_path"], args.get("max_lines", 500))
    if not res["success"]:
        raise ToolError(res["message"])
    return res["data"]["content"]


@tool(
    "write_file_content",
    "Write or overwrite text content to a file at a specific path.",
    {
        "file_path": {"type": "string", "required": True, "description": "Destination file path."},
        "content": {"type": "string", "required": True, "description": "Text or code content to write."}
    },
)
def _write_file_content(args: dict) -> str:
    res = _os_controller.write_file_content(args["file_path"], args["content"])
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "move_or_rename_file",
    "Move or rename a file or folder.",
    {
        "source_path": {"type": "string", "required": True, "description": "Current file path."},
        "dest_path": {"type": "string", "required": True, "description": "Destination file path or name."}
    },
)
def _move_or_rename_file(args: dict) -> str:
    res = _os_controller.move_or_rename_file(args["source_path"], args["dest_path"])
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "delete_file_safely",
    "Safely delete a file or directory with explicit confirmation.",
    {
        "file_path": {"type": "string", "required": True, "description": "File or folder path to delete."},
        "confirm": {"type": "boolean", "required": False, "description": "Explicit confirmation boolean flag."}
    },
)
def _delete_file_safely(args: dict) -> str:
    res = _os_controller.delete_file_safely(args["file_path"], args.get("confirm", False))
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "advanced_file_search",
    "Deep-search files by name, extension, media type, size range, or how recently they were modified, "
    "inside a whitelisted user folder (Desktop, Documents, Downloads, Pictures, Music, Videos, or full home).",
    {
        "query": {"type": "string", "required": False, "description": "Filename substring to match, e.g. 'invoice'."},
        "location": {"type": "string", "required": False,
                      "description": "One of: desktop, documents, downloads, pictures, music, videos, home (default: home)."},
        "extensions": {"type": "string", "required": False,
                         "description": "Comma-separated extensions to match, e.g. 'pdf,docx'."},
        "media_type": {"type": "string", "required": False,
                         "description": "One of: image, video, audio, document, archive, code."},
        "min_size_mb": {"type": "integer", "required": False, "description": "Minimum file size in MB."},
        "max_size_mb": {"type": "integer", "required": False, "description": "Maximum file size in MB."},
        "modified_within_days": {"type": "integer", "required": False,
                                    "description": "Only files modified within this many days."},
    },
)
def _advanced_file_search(args: dict) -> str:
    ext_arg = args.get("extensions")
    extensions = [e.strip() for e in ext_arg.split(",") if e.strip()] if ext_arg else None
    res = _file_media_manager.deep_search(
        query=args.get("query", ""),
        location=args.get("location", "home"),
        extensions=extensions,
        media_type=args.get("media_type"),
        min_size_mb=args.get("min_size_mb"),
        max_size_mb=args.get("max_size_mb"),
        modified_within_days=args.get("modified_within_days"),
    )
    if not res["success"]:
        raise ToolError(res["message"])
    if res["data"]:
        preview = "; ".join(f"{m['name']} ({m['size_mb']}MB, {m['modified']})" for m in res["data"][:5])
        return f"{res['message']} Examples: {preview}"
    return res["message"]


@tool(
    "list_media_files",
    "List a user's photos, videos, music, or documents from a folder without needing a filename query.",
    {
        "media_type": {"type": "string", "required": True,
                         "description": "One of: image, video, audio, document, archive, code."},
        "location": {"type": "string", "required": False,
                      "description": "One of: desktop, documents, downloads, pictures, music, videos, home (default: home)."},
    },
)
def _list_media_files(args: dict) -> str:
    res = _file_media_manager.list_media_files(args["media_type"], args.get("location", "home"))
    if not res["success"]:
        raise ToolError(res["message"])
    if res["data"]:
        preview = "; ".join(m["name"] for m in res["data"][:8])
        return f"{res['message']} Examples: {preview}"
    return res["message"]


@tool(
    "batch_delete_files",
    "Safely delete one or more files/folders (sent to Recycle Bin/Trash when possible). "
    "ALWAYS requires the caller to have already confirmed this with the user before setting confirm=true.",
    {
        "file_paths": {"type": "string", "required": True,
                         "description": "Comma-separated absolute file/folder paths to delete."},
        "confirm": {"type": "boolean", "required": False,
                     "description": "Must be true; set only after explicit user confirmation."},
    },
)
def _batch_delete_files(args: dict) -> str:
    paths = [p.strip() for p in args["file_paths"].split(",") if p.strip()]
    res = _file_media_manager.safe_delete(paths, args.get("confirm", False))
    deleted = (res.get("data") or {}).get("deleted")
    if not res["success"] and not deleted:
        raise ToolError(res["message"])
    return res["message"]


@tool(
    "search_long_term_memory",
    "Search long-term vector memory for past conversation facts, preferences, or user context.",
    {"query": {"type": "string", "required": True, "description": "Search query or topic."}},
)
def _search_long_term_memory(args: dict) -> str:
    results = _vector_memory.search_memory(args["query"])
    if not results:
        return "No relevant memories found in long-term storage."
    mems = [f"- {item['text']}" for item in results]
    return "Retrieved Memories:\n" + "\n".join(mems)


@tool(
    "store_long_term_memory",
    "Store a fact, preference, or context into long-term vector memory.",
    {"fact": {"type": "string", "required": True, "description": "The fact or context to remember permanently."}},
)
def _store_long_term_memory(args: dict) -> str:
    _vector_memory.add_memory(args["fact"])
    return "Stored fact into long-term memory."


# ---- Advanced Autonomous Code Generation & Execution Tools ----

@tool(
    "create_and_save_code",
    "Automatically create a specified folder on Desktop (if provided/doesn't exist), write source code cleanly into a file with UTF-8 encoding, and return confirmation with the absolute file path.",
    {
        "file_name": {
            "type": "string",
            "required": True,
            "description": "Name of the code file to create/save (e.g., 'app.py', 'index.html', 'script.js', 'main.cpp')."
        },
        "code_content": {
            "type": "string",
            "required": True,
            "description": "The complete source code content or generated code to write into the file."
        },
        "folder_name": {
            "type": "string",
            "required": False,
            "description": "Optional folder name to create on Desktop where the file will be saved. Default is directly on Desktop."
        }
    }
)
def _create_and_save_code(args: dict) -> str:
    file_name = str(args.get("file_name", "")).strip()
    code_content = str(args.get("code_content", ""))
    folder_name = str(args.get("folder_name", "")).strip() if args.get("folder_name") else ""

    if not file_name:
        raise ToolError("File name must be specified.")

    desktop = _desktop_dir()
    if folder_name:
        target_dir = desktop / folder_name
    else:
        target_dir = desktop

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / file_name
        file_path.write_text(code_content, encoding="utf-8")
        return f"Code successfully saved to '{file_path.name}'. Absolute path: {file_path.resolve()}"
    except Exception as e:
        raise ToolError(f"Failed to write code file: {e}")


@tool(
    "write_and_save_code",
    "Alias tool for create_and_save_code. Write source code into a file inside a specified folder on Desktop.",
    {
        "file_name": {
            "type": "string",
            "required": True,
            "description": "Name of the code file to create/save (e.g., 'app.py', 'index.html')."
        },
        "code_content": {
            "type": "string",
            "required": True,
            "description": "The complete source code content."
        },
        "folder_name": {
            "type": "string",
            "required": False,
            "description": "Optional subfolder name on Desktop."
        }
    }
)
def _write_and_save_code(args: dict) -> str:
    return _create_and_save_code(args)


@tool(
    "run_code_file",
    "Search for a code file on Desktop (or by path) and execute Python scripts non-blockingly, HTML files in web browser, or executable files in background process.",
    {
        "file_name_or_path": {
            "type": "string",
            "required": True,
            "description": "Name of the file on Desktop (e.g. 'app.py', 'index.html') or full path to the file."
        }
    }
)
def _run_code_file(args: dict) -> str:
    raw_path = str(args.get("file_name_or_path", "")).strip()
    if not raw_path:
        raise ToolError("File name or path must be specified.")

    desktop = _desktop_dir()
    target_path = Path(raw_path)

    # Search logic: direct path, Desktop root, or recursive Desktop search
    if not target_path.exists():
        desk_path = desktop / raw_path
        if desk_path.exists():
            target_path = desk_path
        else:
            matched = list(desktop.rglob(raw_path))
            if not matched:
                matched = [p for p in desktop.rglob("*") if p.is_file() and p.name.lower() == raw_path.lower()]
            if matched:
                target_path = matched[0]
            else:
                raise ToolError(f"Code file '{raw_path}' not found on Desktop or specified path.")

    if not target_path.is_file():
        raise ToolError(f"Path '{target_path}' is not a valid file.")

    ext = target_path.suffix.lower()
    try:
        if ext in (".html", ".htm"):
            webbrowser.open(target_path.as_uri())
            return f"Successfully opened HTML page '{target_path.name}' in browser. Absolute Path: {target_path.resolve()}"
        elif ext == ".py":
            cmd = [sys.executable, str(target_path)]
            proc = subprocess.Popen(cmd, cwd=str(target_path.parent))
            return f"Successfully executed Python script '{target_path.name}' in background (PID: {proc.pid}). Absolute Path: {target_path.resolve()}"
        elif ext == ".js":
            node_exe = shutil.which("node")
            if not node_exe:
                raise ToolError("Node.js ('node') is not installed or not found in PATH.")
            proc = subprocess.Popen([node_exe, str(target_path)], cwd=str(target_path.parent))
            return f"Successfully executed JS script '{target_path.name}' in background (PID: {proc.pid}). Absolute Path: {target_path.resolve()}"
        elif ext == ".sh":
            if _IS_LINUX or _IS_MACOS:
                try:
                    os.chmod(target_path, 0o755)
                except Exception:
                    pass
                proc = subprocess.Popen(["/bin/bash", str(target_path)], cwd=str(target_path.parent))
            else:
                proc = subprocess.Popen([str(target_path)], cwd=str(target_path.parent), shell=True)
            return f"Successfully executed shell script '{target_path.name}' in background (PID: {proc.pid}). Absolute Path: {target_path.resolve()}"
        elif ext in (".bat", ".cmd"):
            proc = subprocess.Popen([str(target_path)], cwd=str(target_path.parent), shell=True)
            return f"Successfully executed batch script '{target_path.name}' in background (PID: {proc.pid}). Absolute Path: {target_path.resolve()}"
        else:
            if _IS_LINUX or _IS_MACOS:
                try:
                    os.chmod(target_path, 0o755)
                except Exception:
                    pass
            proc = subprocess.Popen([str(target_path)], cwd=str(target_path.parent))
            return f"Successfully executed code file '{target_path.name}' in background (PID: {proc.pid}). Absolute Path: {target_path.resolve()}"
    except Exception as e:
        raise ToolError(f"Failed to execute code file '{target_path.name}': {e}")


@tool(
    "list_and_run_desktop_code",
    "Scan Desktop for existing code files (.py, .js, .bat, .sh), match user's requested query or filename, and execute it directly in a non-blocking background process.",
    {
        "query": {
            "type": "string",
            "required": False,
            "description": "Optional file name or keyword to match among Desktop code files."
        }
    }
)
def _list_and_run_desktop_code(args: dict) -> str:
    query = str(args.get("query") or "").strip().lower()
    desktop = _desktop_dir()
    code_extensions = {".py", ".js", ".bat", ".sh", ".cmd"}

    if not desktop.exists():
        raise ToolError("Desktop directory not found.")

    candidates = [
        f for f in desktop.rglob("*")
        if f.is_file() and f.suffix.lower() in code_extensions
    ]

    if not candidates:
        return "Desktop par koi code file (.py, .js, .bat, .sh) nahi mili."

    if query:
        matched = [f for f in candidates if query in f.name.lower() or query in f.stem.lower()]
    else:
        matched = candidates

    if not matched:
        return f"Query '{query}' ke liye Desktop par koi matching code file nahi mili. Found files: {[f.name for f in candidates]}"

    if len(matched) == 1 or query:
        target = matched[0]
        return _run_code_file({"file_name_or_path": str(target.resolve())})
    else:
        file_list = ", ".join([f.name for f in matched[:5]])
        return f"Desktop par Multiple code files mili: {file_list}. Please specify which file to run."


# ------------------------------------------------------------------
# Autonomous Code-Execution Agent: generate -> run -> capture error -> ask the
# local LLM to fix -> retry, up to a bounded number of attempts. Everything
# above (create_and_save_code, run_code_file) is "do one step, tell me what
# happened" — this is the actual closed loop that keeps going on its own
# until the code runs clean or attempts run out.
# ------------------------------------------------------------------
_CODE_LANG_RUNNERS = {
    ".py": lambda path: [sys.executable, str(path)],
    ".js": lambda path: [shutil.which("node") or "node", str(path)],
    ".sh": lambda path: (["/bin/bash", str(path)] if not _IS_WINDOWS else [str(path)]),
}
_CODE_LANG_NAMES = {".py": "Python", ".js": "JavaScript (Node.js)", ".sh": "Bash"}

# Same class of catastrophic patterns run_terminal_command already blocks,
# applied here to LLM-generated code BEFORE it's ever executed. This matters
# more here than for a single manual run: this loop can execute several
# LLM-written revisions of a file unattended, so each candidate needs to
# clear the same bar a human-typed command would.
_DANGEROUS_CODE_PATTERNS = [
    r'rm\s+-rf\s+/(?!\S)', r'\bmkfs\b', r':\(\)\{\s*:\|:&\s*\};:',
    r'\bdd\s+if=', r'\bformat\s+[c-z]:', r'chmod\s+-R\s+777\s+/(?!\S)',
    r'shutil\.rmtree\(\s*[\'"]/[\'"]', r'os\.system\(\s*[\'"]rm\s+-rf\s+/[\'"]\s*\)',
]


def _code_looks_dangerous(code: str) -> str:
    """Best-effort static guard, not a sandbox and not exhaustive — just a
    floor under what this loop is allowed to run on its own."""
    for pat in _DANGEROUS_CODE_PATTERNS:
        if re.search(pat, code, re.IGNORECASE):
            return pat
    return ""


def _strip_code_fences(text: str) -> str:
    """LLMs asked for 'raw code only' still sometimes wrap it in a markdown
    fence anyway — strip that defensively rather than saving ``` into the file."""
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip()


def _decode_maybe_bytes(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val



def _run_code_capture(path: Path, timeout: int = 20) -> dict:
    """Synchronous execution capturing stdout/stderr/exit code. Unlike
    run_code_file's Popen-and-forget (fine for 'launch this app for me'),
    the debug loop has to see the result before it can decide to retry."""
    ext = path.suffix.lower()
    runner = _CODE_LANG_RUNNERS.get(ext)
    if runner is None:
        raise ToolError(f"Autonomous execution sirf .py, .js, aur .sh files ke liye supported hai (mila: '{ext}').")
    if ext == ".js" and not shutil.which("node"):
        raise ToolError("Node.js ('node') is not installed or not found in PATH.")
    cmd = runner(path)
    try:
        res = subprocess.run(cmd, cwd=str(path.parent), capture_output=True, text=True, timeout=timeout)
        return {"exit_code": res.returncode, "stdout": res.stdout[-4000:], "stderr": res.stderr[-4000:],
                "timed_out": False}
    except subprocess.TimeoutExpired as e:
        return {"exit_code": None, "stdout": _decode_maybe_bytes(e.stdout)[-4000:],
                "stderr": _decode_maybe_bytes(e.stderr)[-4000:], "timed_out": True}
    except Exception as e:
        return {"exit_code": None, "stdout": "", "stderr": str(e), "timed_out": False}


@tool(
    "autonomous_code_agent",
    "Write code for a task, run it, and if it fails automatically debug and retry using the local LLM "
    "(feeding the error back in) — up to a bounded number of attempts — until it runs successfully or "
    "attempts run out. Supports Python (.py), Node.js (.js), and Bash (.sh).",
    {
        "file_name": {"type": "string", "required": True,
                        "description": "Target file name, e.g. 'scraper.py'. The extension selects the language."},
        "task_description": {"type": "string", "required": False,
                                "description": "What the code should do. Required if code_content is omitted — "
                                                "the local LLM writes the first draft from this description."},
        "code_content": {"type": "string", "required": False,
                            "description": "An initial code draft to start from, instead of generating one."},
        "folder_name": {"type": "string", "required": False,
                          "description": "Optional subfolder on Desktop to save the file into."},
        "max_attempts": {"type": "integer", "required": False,
                            "description": "Max total run+fix attempts (default 3, hard cap 5)."},
    },
)
def _autonomous_code_agent(args: dict) -> str:
    file_name = str(args.get("file_name", "")).strip()
    if not file_name:
        raise ToolError("File name must be specified, e.g. 'scraper.py'.")
    ext = Path(file_name).suffix.lower()
    if ext not in _CODE_LANG_RUNNERS:
        raise ToolError(f"Autonomous execution sirf .py, .js, aur .sh files ke liye supported hai (mila: '{ext}').")

    task_description = str(args.get("task_description", "")).strip()
    code = str(args.get("code_content", "")).strip()
    folder_name = str(args.get("folder_name", "")).strip() if args.get("folder_name") else ""
    max_attempts = max(1, min(5, int(args.get("max_attempts", 3))))

    if not code and not task_description:
        raise ToolError("Either 'task_description' (to generate code) or 'code_content' (a starting draft) is required.")

    desktop = _desktop_dir()
    target_dir = (desktop / folder_name) if folder_name else desktop
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise ToolError(f"Could not create/access target folder: {e}")
    target_path = target_dir / file_name

    lang_name = _CODE_LANG_NAMES[ext]

    if not code:
        gen_prompt = (
            f"Write a complete, working {lang_name} script that does the following:\n{task_description}\n\n"
            f"Respond with ONLY the raw source code. No explanations, no markdown code fences, no commentary."
        )
        code = _strip_code_fences(_call_local_llm_sync(
            gen_prompt,
            system=f"You are an expert {lang_name} developer. You output only clean, complete, runnable source code.",
        ))

    log = []
    for attempt in range(1, max_attempts + 1):
        danger = _code_looks_dangerous(code)
        if danger:
            _log_error("autonomous_code_agent:dangerous_pattern", Exception(danger))
            log.append(f"Attempt {attempt}: ABORTED — generated code matched a blocked high-risk pattern. Not executed.")
            try:
                target_path.write_text(code, encoding="utf-8")
            except Exception:
                pass
            return (f"Stopped after {attempt} attempt(s) — the generated code matched a blocked, high-risk "
                    f"pattern and was NOT executed for safety. Saved to '{target_path}' for manual review.\n"
                    + "\n".join(log))

        try:
            target_path.write_text(code, encoding="utf-8")
        except Exception as e:
            raise ToolError(f"Failed to write code file: {e}")

        result = _run_code_capture(target_path, timeout=20)

        if result["timed_out"]:
            log.append(f"Attempt {attempt}: still running after 20s — treated as inconclusive, not auto-retried "
                        f"(it may be a long-running/interactive program by design).")
            return (f"Saved and ran '{target_path.name}' (attempt {attempt}/{max_attempts}) — it was still "
                    f"running after 20s, so I stopped waiting rather than assume failure. Check on it directly "
                    f"if it wasn't meant to keep running.\n" + "\n".join(log))

        if result["exit_code"] == 0 and "Traceback (most recent call last)" not in result["stderr"]:
            log.append(f"Attempt {attempt}: SUCCESS (exit code 0).")
            out_preview = (result["stdout"] or "(no output)")[:800]
            return (f"'{target_path.name}' ran successfully on attempt {attempt}/{max_attempts}. "
                    f"Absolute path: {target_path.resolve()}\nOutput:\n{out_preview}")

        err_excerpt = (result["stderr"] or f"(no stderr, exit code {result['exit_code']})")[:800]
        log.append(f"Attempt {attempt}: FAILED (exit code {result['exit_code']}). Error: {err_excerpt[:200]}")

        if attempt >= max_attempts:
            break

        fix_prompt = (
            f"This {lang_name} script failed when run:\n\n"
            f"--- CODE ---\n{code}\n\n"
            f"--- ERROR OUTPUT ---\n{err_excerpt}\n\n"
            f"Fix the bug. Respond with ONLY the complete corrected raw source code. "
            f"No explanations, no markdown code fences, no commentary."
        )
        try:
            fixed = _strip_code_fences(_call_local_llm_sync(
                fix_prompt,
                system=f"You are an expert {lang_name} debugger. You output only clean, complete, runnable source code.",
            ))
        except ToolError as e:
            log.append(f"Could not reach local LLM to auto-fix: {e}")
            break
        if fixed.strip():
            code = fixed

    return (f"Gave up after {max_attempts} attempt(s) — could not get '{target_path.name}' to run successfully. "
            f"The last (still-failing) version is saved at {target_path.resolve()} for you to review.\n"
            + "\n".join(log))


# ------------------------------------------------------------------
# 3. Prompt builder – turns the registry into instructions the LLM
# ------------------------------------------------------------------
def build_tool_system_prompt() -> str:
    tool_lines = []
    for name, spec in TOOL_REGISTRY.items():
        params = spec["parameters"]
        if params:
            param_desc = ", ".join(
                f'{p}: {pv.get("description", "")}' for p, pv in params.items()
            )
        else:
            param_desc = "none"
        tool_lines.append(f'- "{name}": {spec["description"]} (params: {param_desc})')
    tools_block = "\n".join(tool_lines)

    return f"""You are ISHA's JSON tool router.
Available tools:
{tools_block}

Respond ONLY with a single JSON object. No markdown backticks, no code block fences, no conversation, no preamble.

If a tool applies:
{{"action": "tool_call", "tool": "<tool_name>", "arguments": {{...}}}}

If no tool applies:
{{"action": "chat_reply", "tool": null, "arguments": {{}}}}

Output raw JSON ONLY."""


# ------------------------------------------------------------------
# 4. Safe JSON extraction from raw LLM text
# ------------------------------------------------------------------
def parse_router_json(raw_text: str):
    """Best-effort, defensive extraction of the router's JSON object.
    Returns a dict or None if nothing parseable was found."""
    if not raw_text:
        return None
    text = raw_text.strip()

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text, start)
            return obj
        except Exception:
            start = text.find("{", start + 1)
    return None


# Standard 21-point hand topology, for drawing the preview ourselves. The
# Tasks API dropped mp.solutions.drawing_utils along with the rest of the
# legacy namespace, so there is nothing to borrow.
_HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
)


def _draw_hand_overlay(bgr, landmarks):
    h, w = bgr.shape[:2]
    pts = [(int(p.x * w), int(p.y * h)) for p in landmarks]
    for a, b in _HAND_CONNECTIONS:
        if a < len(pts) and b < len(pts):
            cv2.line(bgr, pts[a], pts[b], (255, 190, 0), 2)
    for x, y in pts:
        cv2.circle(bgr, (x, y), 3, (0, 240, 255), -1)


class HandTasksWorker(HandGestureWorker):
    """Hand tracking on MediaPipe's Tasks API (mediapipe >= 0.10.30).

    This is the path that makes gestures work on Python 3.13/3.14 in-process,
    with no second interpreter. It produces the same 21 normalized landmarks,
    so the entire inherited gesture pipeline is reused as-is."""

    def run(self):
        if not _CV2_OK:
            self.error_occurred.emit("OpenCV missing. Install: pip install opencv-python-headless")
            return
        if not _MP_TASKS_OK:
            self.error_occurred.emit("MediaPipe Tasks API not available: " + (_MEDIAPIPE_ERROR or "?"))
            return
        if not _PYAUTOGUI_OK:
            self.error_occurred.emit("pyautogui missing. Install: pip install pyautogui")
            return

        try:
            model_path = ensure_hand_model(self.status_changed.emit)
        except Exception as e:
            self.error_occurred.emit(str(e))
            return

        cap = None
        landmarker = None
        try:
            if _IS_WINDOWS:
                cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            else:
                cap = cv2.VideoCapture(self.camera_index)
            if not cap.isOpened():
                self.error_occurred.emit("Webcam open nahi ho paaya (index/permissions check karo).")
                return
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

            options = mp_vision.HandLandmarkerOptions(
                base_options=_MPBaseOptions(model_asset_path=model_path),
                running_mode=mp_vision.RunningMode.VIDEO,
                num_hands=1,
                min_hand_detection_confidence=0.6,
                min_tracking_confidence=0.5,
            )
            landmarker = mp_vision.HandLandmarker.create_from_options(options)

            self._reset_gesture_state()
            self._running = True
            self.status_changed.emit(_hand_gesture_help(self.scroll_enabled))

            # detect_for_video demands strictly increasing timestamps, so drive
            # them off a frame counter rather than the wall clock (two frames
            # can land in the same millisecond).
            frame_no = 0
            while self._running:
                ok, frame = cap.read()
                if not ok:
                    self.msleep(self.FRAME_DELAY_MS)
                    continue
                frame_no += 1
                frame = cv2.flip(frame, 1)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = landmarker.detect_for_video(
                    mp_image, frame_no * self.FRAME_DELAY_MS)

                hands_found = getattr(result, "hand_landmarks", None) or []
                if hands_found:
                    landmarks = hands_found[0]
                    self._interpret_gesture(landmarks)
                    if self.emit_preview:
                        try:
                            _draw_hand_overlay(frame, landmarks)
                        except Exception:
                            pass
                else:
                    self._reset_gesture_state()

                if self.emit_preview:
                    self._emit_frame(frame)
                self.msleep(self.FRAME_DELAY_MS)
        except Exception as e:
            if self._running:
                self.error_occurred.emit(f"Hand control error: {e}")
        finally:
            self._running = False
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            if landmarker is not None:
                try:
                    landmarker.close()
                except Exception:
                    pass
            self.status_changed.emit("Hand control stopped.")


class _SidecarLandmark:
    """Stands in for a MediaPipe NormalizedLandmark so the inherited gesture
    code can consume sidecar data without knowing where it came from."""
    __slots__ = ("x", "y", "z")

    def __init__(self, x, y, z=0.0):
        self.x = x; self.y = y; self.z = z


class HandSidecarWorker(HandGestureWorker):
    """Hand tracking when this Python has no MediaPipe build.

    Spawns ISHA's own Python 3.10-3.12 sidecar, reads landmarks off its stdout,
    and feeds them into the exact same _interpret_gesture pipeline the
    in-process worker uses."""

    def __init__(self, camera_index: int = 0, emit_preview: bool = True,
                 scroll_enabled: bool = False, parent=None):
        super().__init__(camera_index, emit_preview, scroll_enabled, parent)
        self._proc = None

    def stop(self):
        self._running = False
        p = self._proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass

    def run(self):
        if not _PYAUTOGUI_OK:
            self.error_occurred.emit("pyautogui missing. Install: pip install pyautogui")
            return

        self.status_changed.emit("Hand env check ho raha hai…")
        try:
            py = ensure_hand_env(self.status_changed.emit)
        except Exception as e:
            self.error_occurred.emit(f"Hand env setup fail: {e}")
            return
        if not py:
            self.error_occurred.emit(_hand_env_help())
            return

        try:
            script = _write_hand_sidecar()
        except Exception as e:
            self.error_occurred.emit(str(e))
            return

        argv = [py, str(script), str(self.camera_index), "1" if self.emit_preview else "0"]
        kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "bufsize": 1,
        }
        if _IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000       # CREATE_NO_WINDOW

        self._reset_gesture_state()
        try:
            self._proc = subprocess.Popen(argv, **kwargs)
        except Exception as e:
            self.error_occurred.emit(f"Sidecar start nahi hua: {e}")
            return

        self._running = True
        try:
            while self._running:
                line = self._proc.stdout.readline()
                if not line:
                    break                       # sidecar exited
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue

                if msg.get("error"):
                    self.error_occurred.emit(f"Hand sidecar: {msg['error']}")
                    break
                if msg.get("status"):
                    self.status_changed.emit(
                        _hand_gesture_help(self.scroll_enabled)
                        if msg["status"] == "camera ready" else str(msg["status"]))
                    continue

                raw = msg.get("lm")
                if raw:
                    self._interpret_gesture([_SidecarLandmark(*p) for p in raw])
                else:
                    self._reset_gesture_state()

                jpg = msg.get("jpg")
                if jpg and self.emit_preview:
                    try:
                        img = QImage()
                        if img.loadFromData(base64.b64decode(jpg), "JPG"):
                            self.frame_ready.emit(img)
                    except Exception:
                        pass
        except Exception as e:
            if self._running:
                self.error_occurred.emit(f"Hand control error: {e}")
        finally:
            self._running = False
            p, self._proc = self._proc, None
            if p is not None:
                try:
                    p.terminate()
                    p.wait(timeout=3)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
                # Surface a startup crash instead of failing silently.
                try:
                    err = (p.stderr.read() or "").strip()
                except Exception:
                    err = ""
                if err:
                    print(f"[Hand] sidecar stderr: {err[-400:]}")
            self.status_changed.emit("Hand control stopped.")


def hand_backend_name() -> str:
    if _CV2_OK and _MP_LEGACY_OK:
        return "legacy"
    if _CV2_OK and _MP_TASKS_OK:
        return "tasks"
    if _hand_sidecar_possible():
        return "sidecar"
    return "none"


def make_hand_worker(camera_index: int = 0, emit_preview: bool = True,
                     scroll_enabled: bool = False):
    """Best available hand-tracking backend for this machine."""
    backend = hand_backend_name()
    kwargs = dict(camera_index=camera_index, emit_preview=emit_preview,
                  scroll_enabled=scroll_enabled)
    if backend == "legacy":
        return HandGestureWorker(**kwargs)
    if backend == "tasks":
        return HandTasksWorker(**kwargs)
    return HandSidecarWorker(**kwargs)


def hand_control_available() -> bool:
    return _PYAUTOGUI_OK and hand_backend_name() != "none"


# ------------------------------------------------------------------
# 5. Validation + execution — the only place a tool actually runs
# ------------------------------------------------------------------
class ToolError(Exception):
    pass


def _validate_args(name: str, schema: dict, raw_args) -> dict:
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ToolError(f"'{name}' arguments must be a JSON object.")

    clean = {}
    for pname, pspec in schema.items():
        if pname in raw_args:
            value = raw_args[pname]
            ptype = pspec["type"]
            if ptype == "integer":
                try:
                    value = int(value)
                except Exception:
                    raise ToolError(f"'{pname}' must be an integer.")
            elif ptype == "boolean":
                if isinstance(value, str):
                    value = value.strip().lower() in ("true", "1", "yes")
                value = bool(value)
            elif ptype == "string":
                value = str(value)
            clean[pname] = value
        elif pspec.get("required"):
            raise ToolError(f"Missing required argument '{pname}' for tool '{name}'.")

    unknown = set(raw_args) - set(schema)
    if unknown:
        raise ToolError(f"Unknown argument(s) for '{name}': {', '.join(unknown)}")

    return clean


def execute_tool(name: str, raw_args) -> str:
    """The single, secure entry point for running a tool.
    - `name` must exactly match a registered tool (allow-list).
    - `raw_args` is validated field-by-field against that tool's schema.
    - Never eval()/exec()s anything, never shells out with an LLM-built string.
    Raises ToolError on any problem; caller decides how to surface it.
    """
    t0 = time.time()
    resolved = resolve_tool_name(name)
    if resolved is None:
        _audit_tool(name, raw_args, False, "unknown tool", t0)
        raise ToolError(
            f"Unknown tool '{name}'. Closest available: {suggest_tools_for(name)}. "
            f"Call list_capabilities to see everything you can do."
        )
    if resolved != name:
        print(f"[tools] resolved '{name}' -> '{resolved}'")
    name = resolved
    spec = TOOL_REGISTRY[name]
    try:
        clean_args = _validate_args(name, spec["parameters"], raw_args)
    except ToolError as e:
        _audit_tool(name, raw_args, False, f"invalid arguments: {e}", t0)
        raise
    try:
        out = spec["handler"](clean_args)
    except ToolError as e:
        _audit_tool(name, clean_args, False, str(e), t0)
        raise
    except ISHA_Cancelled:
        _audit_tool(name, clean_args, False, "stopped by user", t0)
        raise ToolError("Stopped by user.")
    except Exception as e:
        _audit_tool(name, clean_args, False, f"{type(e).__name__}: {e}", t0)
        raise ToolError(f"Tool '{name}' failed: {e}")
    _audit_tool(name, clean_args, True, out, t0)
    return out


# Per-thread context set by run_tool_gated() so the audit log records who
# approved a tool run (auto / user / trusted) without changing execute_tool's
# signature, which the rest of the file depends on.
_TOOL_CTX = threading.local()


def _audit_tool(name, args, success, result, t0):
    try:
        if ACTION_LOG is None:
            return
        ACTION_LOG.record(
            "tool", tool=name, args=args if isinstance(args, dict) else {"raw": str(args)},
            success=bool(success), result=str(result)[:400],
            duration_ms=int((time.time() - t0) * 1000),
            risk=tool_risk(name) if name in TOOL_REGISTRY else "unknown",
            approval=getattr(_TOOL_CTX, "approval", "direct"),
            model=ACTIVE_MODEL.get("name", ""), task=ACTIVE_MODEL.get("task", ""))
    except Exception:
        pass


# ------------------------------------------------------------------
# 5b. Risk tiers — which tools may run unattended, and which must ask
#
# ISHA is allowed to drive the machine, so "the model picked the wrong tool"
# has to be survivable. Every tool falls into one of three tiers:
#   safe     - reversible / read-only. Runs without asking.
#   confirm  - changes or destroys something. Asks first.
#   critical - system-wide or arbitrary code execution. Asks, loudly.
# Anything not listed defaults to "confirm", so a newly added tool is
# cautious until it is deliberately marked safe.
# ------------------------------------------------------------------

TOOL_RISK = {
    # --- read-only / trivially reversible ---
    "open_notepad": "safe", "open_calculator": "safe", "open_browser": "safe",
    "open_task_manager": "safe", "open_url": "safe",
    # Opening a window is reversible - the user just closes it. Flip this to
    # "confirm" if you would rather be asked every single time.
    "open_app": "safe",
    "play_song": "safe", "pause_song": "safe", "resume_song": "safe",
    "stop_song": "safe", "mute_system": "safe", "set_volume": "safe",
    "set_brightness": "safe",
    "check_cpu_usage": "safe", "check_memory_usage": "safe",
    "check_battery": "safe", "get_time": "safe", "get_date": "safe",
    "get_system_stats": "safe", "get_gpu_usage": "safe",
    "get_wifi_status": "safe", "list_running_processes": "safe",
    "minimize_window": "safe", "maximize_window": "safe",
    "switch_window": "safe", "close_browser_tab": "safe",
    "search_files": "safe", "advanced_file_search": "safe",
    "list_media_files": "safe", "read_file_content": "safe",
    "search_long_term_memory": "safe", "store_long_term_memory": "safe",
    "create_desktop_folder": "safe",
    "lock_screen": "safe",
    "close_window": "safe",

    # --- writes or destroys ---
    "write_file_content": "confirm",
    "move_or_rename_file": "confirm",
    "delete_file_safely": "confirm",
    "batch_delete_files": "critical",
    "empty_recycle_bin": "critical",   # unrecoverable, so it asks loudly
    "kill_process": "confirm",
    "create_and_save_code": "confirm",
    "write_and_save_code": "confirm",

    # --- runs arbitrary code / hits the whole machine ---
    "run_terminal_command": "critical",
    "run_code_file": "critical",
    "list_and_run_desktop_code": "critical",
    "autonomous_code_agent": "critical",
    "system_power_action": "critical",
}

_RISK_ORDER = {"safe": 0, "confirm": 1, "critical": 2}


def tool_risk(name: str) -> str:
    """Unlisted tools are treated as 'confirm', never as 'safe'."""
    return TOOL_RISK.get(name, "confirm")


def _apply_tool_risk():
    for tname, spec in TOOL_REGISTRY.items():
        spec["risk"] = tool_risk(tname)


def describe_tool_call(name: str, args: dict) -> str:
    """Human-readable one-liner for the confirmation dialog and step log."""
    try:
        special = _describe_special(name, args or {})
        if special:
            return special
    except Exception:
        pass
    if not args:
        return name
    shown = []
    for k, v in list(args.items())[:4]:
        sv = str(v)
        if len(sv) > 70:
            sv = sv[:67] + "..."
        shown.append(f"{k}={sv}")
    return f"{name}({', '.join(shown)})"


# ------------------------------------------------------------------
# 5c. Function-calling schema (OpenAI shape)
#
# llama-cpp-python accepts a `tools` array in the OpenAI function-calling
# shape when the loaded GGUF has a tool-aware chat template. Many community
# quants do not, which is why AgentWorker keeps the JSON-in-prompt protocol as
# a fallback and flips to it automatically on the first rejection.
# ------------------------------------------------------------------

def build_tool_schemas() -> list:
    tools = []
    for tname, spec in TOOL_REGISTRY.items():
        props, required = {}, []
        for pname, pspec in (spec.get("parameters") or {}).items():
            props[pname] = {
                "type": pspec.get("type", "string"),
                "description": pspec.get("description", ""),
            }
            if pspec.get("required"):
                required.append(pname)
        tools.append({
            "type": "function",
            "function": {
                "name": tname,
                "description": spec["description"],
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        })
    return tools


def normalise_tool_calls(raw_calls) -> list:
    """Returns [{'function': {'name':..., 'arguments': {...}}}], but the
    arguments field is sometimes a JSON *string* depending on the model."""
    out = []
    for call in (raw_calls or []):
        fn = (call or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        out.append((str(name), args))
    return out



# ------------------------------------------------------------------
def _extract_folder_name(text: str):
    """Best-effort folder-name extraction for phrasings like:
       - desktop par "add" naam ka folder banake do
       - "Projects" naam se folder banado
       - add naam ka folder banado   (no quotes)
       - create a folder named Projects on desktop
       - folder banao named Projects
    """
    
    m = re.search(r'["\'“”]([^"\'“”]{1,60})["\'“”]', text)
    if m:
        return m.group(1).strip()
    
    m = re.search(r'\b([A-Za-z0-9_\-]{1,40})\s+naam(?:\s+ka|\s+ki)?\s+folder\b', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    
    m = re.search(r'\bfolder\s+(?:named|naam(?:\s+ka|\s+ki)?)\s+([A-Za-z0-9_\-]{1,40})\b', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    
    m = re.search(r'\bnamed\s+([A-Za-z0-9_\-]{1,40})\s+folder\b', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    m = re.search(r'\b([A-Za-z0-9_\-]{1,40})\s+folder\s+(?:banao|banado|create)\b', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


_SONG_STOPWORDS = {
    "gaana", "gana", "gaane", "song", "songs", "music", "bajao", "baja", "do",
    "bajado", "play", "karo", "kardo", "kar", "chalao", "chala", "chalado",
    "suna", "sunao", "suno", "ek", "koi", "please", "plz", "the", "a", "an",
    "of", "mera", "liye", "for", "me", "mujhe", "isha", "ka", "ki", "ke", "ko",
    "youtube", "yt", "par", "pe", "per", "on", "se", "wala", "wali", "lagao", "laga",
    "video", "mein", "main", "chrome", "browser",
}


def _extract_song_name(text: str) -> str:
    """Best-effort song name/artist extraction for phrasings like:
       - "Kesariya" gaana bajao / gaana bajao "Kesariya"
       - Arijit Singh ka gaana chalao
       - song play karo Tum Hi Ho
    """
    m = re.search(r'["\'“”]([^"\'“”]{1,80})["\'“”]', text)
    if m:
        return m.group(1).strip()
    words = re.findall(r"[A-Za-z0-9']+", text)
    kept = [w for w in words if w.lower() not in _SONG_STOPWORDS]
    return " ".join(kept).strip()


_COMPOUND_MARKERS = re.compile(
    r"(?:^|\s)(?:aur|and|then|phir|fir|fhir|uske\s+baad|after\s+that|also|bhi\s+kar|"
    r"iske\s+baad|saath\s+hi)(?:\s|$)", re.IGNORECASE)


_SMALLTALK_RE = re.compile(
    r"^(?:hi|hey|hy|hello+|helo+|yo|hola|namaste|namaskar|"
    r"good\s*(?:morning|afternoon|evening|night)|"
    r"kaise\s*ho|kya\s*haal|how\s*are\s*you|"
    r"thanks?|thank\s*you|shukriya|dhanyavad|"
    r"ok|okay|theek\s*hai|thik\s*hai|bye|goodbye|good\s*bye|"
    r"who\s*are\s*you|tum\s*kaun\s*ho|aap\s*kaun\s*ho|"
    r"tu\s*kaun\s*hai|what\s*are\s*you)\b",
    re.IGNORECASE)


def looks_like_smalltalk(text: str) -> bool:
    """Greetings, thanks and 'who are you' need a reply, not a tool plan."""
    t = (text or "").strip().strip("!.,?")
    if not t or len(t.split()) > 6:
        return False
    # strip a leading wake-name so "hey isha" and "isha hello" both match
    t = re.sub(r"\b(isha|jarvis)\b", "", t, flags=re.IGNORECASE).strip()
    if not t:
        return True
    return bool(_SMALLTALK_RE.match(t))


def _looks_compound(text: str) -> bool:
    """True when the message plausibly asks for more than one thing, in which
    case it belongs to the agent loop rather than the single-tool shortcut."""
    t = (text or "").strip()
    if not t:
        return False
    if len(t) > 120:
        return True
    if t.count(",") >= 1 and len(t.split()) > 6:
        return True
    return bool(_COMPOUND_MARKERS.search(t))



# Typos people actually type in a hurry. match_quick_command used to require
# the exact word, so "opne notepeda" fell straight through to the LLM, which
# then apologised instead of opening anything.
_TYPO_FIX = {
    "opne": "open", "oepn": "open", "opn": "open", "ope": "open", "opeen": "open",
    "notepeda": "notepad", "notpad": "notepad", "notepd": "notepad",
    "notepade": "notepad", "noteped": "notepad", "notpead": "notepad",
    "notepda": "notepad", "notebad": "notepad",
    "recyle": "recycle", "recylce": "recycle", "recycel": "recycle",
    "recyclebin": "recycle bin", "recylebin": "recycle bin",
    "calcualtor": "calculator", "calcultor": "calculator", "calculater": "calculator",
    "kolo": "kholo", "khlo": "kholo", "kholdo": "kholo", "kholna": "kholo",
    "chorme": "chrome", "crome": "chrome", "cromee": "chrome",
    "setings": "settings", "settngs": "settings", "seting": "settings",
    "explorar": "explorer", "explorr": "explorer",
    "taskmanger": "task manager", "taskmanager": "task manager",
    "cotrol": "control", "controll": "control",
}

# Small closed vocabulary for fuzzy repair of words we have no explicit entry
# for. Deliberately tiny - a big vocabulary here starts "correcting" real
# words like song titles and filenames.
_OPEN_VERB_VOCAB = ("open", "kholo", "start", "launch", "chalu", "chalao",
                    "notepad", "calculator", "recycle", "settings", "chrome")


def _normalize_typos(text: str) -> str:
    """Token-level typo repair for the deterministic open/launch matchers.

    Only used by the app-launch matchers - NOT by the filename or song-name
    ones, because this collapses punctuation and would turn "test.py" into
    "test py"."""
    out = []
    for w in re.findall(r"[A-Za-z0-9']+", text):
        lw = w.lower()
        if lw in _TYPO_FIX:
            out.append(_TYPO_FIX[lw])
            continue
        if len(lw) >= 5:
            near = difflib.get_close_matches(lw, _OPEN_VERB_VOCAB, n=1, cutoff=0.82)
            if near:
                out.append(near[0])
                continue
        out.append(lw)
    return " ".join(out)


def match_quick_command(text: str):
    """Return (tool_name, args_dict) for common, unambiguous commands,
    or None if the message should go through the normal LLM router."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    # Typo-repaired copy, used only by the app-launch matchers below.
    norm = _normalize_typos(t)

    # --- Run user's Desktop code ---
    if "mera wala code run" in low or "desktop code run" in low or low == "run my code":
        return ("list_and_run_desktop_code", {"query": ""})

    # --- Run specific code file ---
    m_run = re.search(r'\b(run|execute|chalao|chalayein)\s+(?:code\s+|script\s+|file\s+)?([A-Za-z0-9_\-]+\.(?:py|js|sh|bat|cmd))\b', low)
    if m_run:
        return ("run_code_file", {"file_name_or_path": m_run.group(2)})

    # --- Create a folder on the Desktop ---
    if "folder" in low and re.search(r'bana|create|bnao', low):
        name = _extract_folder_name(t)
        if name:
            return ("create_desktop_folder", {"folder_name": name})
        return None  

    # Widened from \b(khol|open|...)\b to \w* suffixes so "kholo", "kholdo",
    # "chalu karo" and "launch" all count, and matched against `norm` so the
    # typo'd spellings land too.
    _open_verb = re.compile(r'\b(khol\w*|open|start\w*|chalu\w*|launch\w*|chala\w*)\b')

    # --- Open Notepad ---
    if "notepad" in norm and _open_verb.search(norm):
        return ("open_notepad", {})

    # --- Open Calculator ---
    if re.search(r'\bcalculator|calc\b', norm) and _open_verb.search(norm):
        return ("open_calculator", {})

    # --- Open Task Manager ---
    if "task manager" in norm and _open_verb.search(norm):
        return ("open_task_manager", {})

    # --- Open Recycle Bin ---
    if re.search(r'\b(recycle bin|trash|dustbin|kachra)\b', norm):
        if re.search(r'\b(empty|khaali|khali|clear|clean|saaf|saf|safai|delete|hata\w*|mita\w*)\b', norm):
            return ("empty_recycle_bin", {})
        if re.search(r'\b(kitn\w*|how much|status|size|check)\b', norm):
            return ("recycle_bin_status", {})
        return ("open_app", {"app_name": "recycle bin"})

    # --- Lock screen ---
    if re.search(r'\block\b', low) and re.search(r'screen|pc\b|computer|system|laptop', low):
        return ("lock_screen", {})

    # --- Mute / unmute ---
    if re.search(r'\bunmute\b', low):
        return ("mute_system", {"muted": False})
    if re.search(r'\bmute\b', low) and "volume" not in low:
        return ("mute_system", {"muted": True})


    m = re.search(r'volume\D{0,10}(\d{1,3})\s*%?', low)
    if m:
        return ("set_volume", {"percent": int(m.group(1))})


    if re.search(r'\bcpu\b', low):
        return ("check_cpu_usage", {})
    if re.search(r'\bram\b|\bmemory\b', low):
        return ("check_memory_usage", {})
    if "battery" in low:
        return ("check_battery", {})

    # --- Pause / resume / stop a song (check before "play", since these can overlap) ---
    if re.search(r'\b(gaana|gana|song|music)\b', low) or _current_song_name:
        if re.search(r'\bpause\b', low):
            return ("pause_song", {})
        if re.search(r'\b(resume|unpause|wapas\s*chalao)\b', low):
            return ("resume_song", {})
        if re.search(r'\b(band|stop|ruko|roko)\b', low):
            return ("stop_song", {})

    # --- Play a song ---
    if re.search(r'\b(gaana|gana|gaane|song|songs|music)\b', low) and \
       re.search(r'\b(bajao|baja\s*do|bajado|play|chalao|chala\s*do|chalado|sunao|suna\s*do)\b', low):
        query = _extract_song_name(t)
        if query:
            return ("play_song", {"query": query})
        return None  # ambiguous ("gaana bajao" with no name) — let the LLM ask which song

    # --- Open ANY other app / window / folder ---
    # Deliberately last: every specific matcher above wins first, and whatever
    # is left that is phrased as an open request goes to the generic launcher
    # instead of dying in the LLM as "I can't open applications".
    m_open = re.search(
        r'^(?:isha\s+)?(?:please\s+)?(?:open|start|launch|chalu)\s+'
        r'(.{2,40}?)'
        r'(?:\s+(?:ko|kholo|khol\w*|karo|kar|do|dijiye|plz|please))*$', norm)
    if not m_open:
        m_open = re.search(
            r'^(?:isha\s+)?(.{2,40}?)\s+(?:ko\s+)?'
            r'(?:khol\w*|open(?:\s*kar\w*)?|start(?:\s*kar\w*)?|'
            r'launch(?:\s*kar\w*)?|chalu(?:\s*kar\w*)?)'
            r'(?:\s+(?:do|dijiye|please|plz))?$', norm)
    if m_open:
        target = m_open.group(1).strip(" .!?,")
        target = re.sub(r'^(the|ek|mera|meri|my)\s+', '', target).strip()
        if target and target not in ("it", "this", "that", "yeh", "woh", "isko", "usko"):
            return ("open_app", {"app_name": target})

    return None



# ======================================================================
# ISHA COMMAND PLANNER v3  —  "bolo, aur kaam ho jaaye"
#
# Pehle ka problem: ek compound command ("folder banao aur chrome me site
# kholo fir youtube par gaana chalao") seedha chhote 3B model ke paas jaata
# tha. Model tool CALL karne ke bajaye plan LIKH deta tha ("main open_app tool
# ka upyog karunga...") aur agent loop us likhe hue plan ko hi final jawab maan
# leta tha — koi kaam nahi hota tha, upar se jhootha "Result:" bhi aata tha.
#
# Ab flow yeh hai:
#   1. split_command()  — command ko alag-alag kaamon me todo (aur/fir/phir/then/,)
#   2. match_task()     — har kaam ko bina LLM ke seedha tool se jodo
#   3. PlanExecutorWorker — har tool ko order me chalao; jo kaam samajh na aaye
#      sirf USKE liye LLM se decision lo (temperature 0, JSON only)
#   4. Report sirf ASLI tool results se banti hai — model kuch invent nahi kar sakta
# ======================================================================

import html as _html
import urllib.parse as _urlparse

_BROWSER_ALIASES = {
    "chrome": "chrome", "google chrome": "chrome", "crome": "chrome",
    "chorme": "chrome", "cromee": "chrome",
    "edge": "edge", "microsoft edge": "edge",
    "firefox": "firefox", "brave": "brave",
}
_BROWSER_RE = re.compile(
    r"\b(google\s+chrome|chrome|crome|chorme|cromee|microsoft\s+edge|edge|firefox|brave)\b",
    re.IGNORECASE)


def _normalize_browser(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    return _BROWSER_ALIASES.get(n, "")


def _find_browser_exe(name: str):
    """Chrome/Edge/Firefox/Brave ka asli executable dhoondo (Win/Linux/macOS)."""
    name = _normalize_browser(name)
    if not name:
        return None
    if _IS_WINDOWS:
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        table = {
            "chrome":  [pf + r"\Google\Chrome\Application\chrome.exe",
                        pf86 + r"\Google\Chrome\Application\chrome.exe",
                        local + r"\Google\Chrome\Application\chrome.exe"],
            "edge":    [pf86 + r"\Microsoft\Edge\Application\msedge.exe",
                        pf + r"\Microsoft\Edge\Application\msedge.exe"],
            "brave":   [pf + r"\BraveSoftware\Brave-Browser\Application\brave.exe",
                        local + r"\BraveSoftware\Brave-Browser\Application\brave.exe"],
            "firefox": [pf + r"\Mozilla Firefox\firefox.exe",
                        pf86 + r"\Mozilla Firefox\firefox.exe"],
        }
        for p in table.get(name, []):
            if p and os.path.isfile(p):
                return p
        exe = {"chrome": "chrome.exe", "edge": "msedge.exe",
               "brave": "brave.exe", "firefox": "firefox.exe"}[name]
        return shutil.which(exe)
    if _IS_MACOS:
        app = {"chrome": "Google Chrome", "edge": "Microsoft Edge",
               "brave": "Brave Browser", "firefox": "Firefox"}[name]
        return f"mac:{app}" if Path(f"/Applications/{app}.app").exists() else None
    linux = {
        "chrome":  ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
        "edge":    ["microsoft-edge", "microsoft-edge-stable"],
        "brave":   ["brave-browser", "brave"],
        "firefox": ["firefox", "firefox-esr"],
    }
    for c in linux[name]:
        p = shutil.which(c)
        if p:
            return p
    return None


def open_url_in_browser(url: str, browser: str = "") -> str:
    """URL kholo — maanga hua browser mile to usme, warna default browser me.
    Return: jis browser me khula uska display naam."""
    b = _normalize_browser(browser)
    if b:
        exe = _find_browser_exe(b)
        if exe:
            try:
                if exe.startswith("mac:"):
                    subprocess.Popen(["open", "-a", exe[4:], url])
                else:
                    subprocess.Popen([exe, url])
                return {"chrome": "Chrome", "edge": "Edge",
                        "firefox": "Firefox", "brave": "Brave"}[b]
            except Exception as e:
                _log_error(f"open_url_in_browser:{b}", e)
    webbrowser.open(url)
    return "browser"


_KNOWN_SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "github": "https://github.com",
    "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com",
    "whatsapp": "https://web.whatsapp.com", "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai", "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com", "linkedin": "https://www.linkedin.com",
    "twitter": "https://x.com", "x": "https://x.com",
    "wikipedia": "https://www.wikipedia.org", "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com", "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com", "reddit": "https://www.reddit.com",
    "google drive": "https://drive.google.com", "drive": "https://drive.google.com",
    "maps": "https://maps.google.com", "google maps": "https://maps.google.com",
}

_UA_BROWSER = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0 Safari/537.36"),
               "Accept-Language": "en-US,en;q=0.9"}


def resolve_website(query: str):
    """Naam/topic se asli website ka URL nikalo. Yahi ISHA ka 'khud decide
    karna' hai: 'elegoo giga' bolo to wo search karke pehli asli site kholti hai.
    Returns (url, how)  how = direct | known | search | search_page"""
    q = (query or "").strip().strip("\"'")
    if not q:
        return "https://www.google.com", "known"
    if re.match(r"^https?://", q, re.I):
        return q, "direct"
    if re.match(r"^[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/\S*)?$", q, re.I):
        return "https://" + q, "direct"
    key = q.lower()
    if key in _KNOWN_SITES:
        return _KNOWN_SITES[key], "known"
    if _REQUESTS_OK:
        try:
            r = requests.get("https://html.duckduckgo.com/html/", params={"q": q},
                             headers=_UA_BROWSER, timeout=6)
            for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"', r.text):
                href = _html.unescape(m.group(1))
                if "uddg=" in href:
                    qs = _urlparse.parse_qs(_urlparse.urlparse(href).query)
                    href = _urlparse.unquote(qs.get("uddg", [""])[0])
                if href.startswith("http") and "duckduckgo.com" not in href:
                    return href, "search"
        except Exception as e:
            _log_error("resolve_website", e)
    return "https://www.google.com/search?q=" + _urlparse.quote_plus(q), "search_page"


def _youtube_top_video(query: str):
    if not _REQUESTS_OK:
        return None
    try:
        resp = requests.get("https://www.youtube.com/results",
                            params={"search_query": query},
                            headers=_UA_BROWSER, timeout=8)
        m = (re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', resp.text)
             or re.search(r'"videoId":"([\w-]{11})"', resp.text))
        return m.group(1) if m else None
    except Exception as e:
        _log_error("youtube_top_video", e)
        return None


_BROWSER_PARAM = {"type": "string", "required": False,
                  "description": "Optional: chrome, edge, firefox or brave. Omit for default browser."}


@tool(
    "open_website",
    "Open a website in a browser. Give a URL OR just a name/topic (e.g. 'elegoo giga', "
    "'arduino docs') — ISHA finds the most relevant real site by itself and opens it.",
    {"target": {"type": "string", "required": True,
                "description": "URL, domain, or name of the site/product/topic."},
     "browser": _BROWSER_PARAM},
)
def _open_website(args: dict) -> str:
    target = str(args.get("target") or "").strip()
    if not target:
        raise ToolError("Kaunsi site kholni hai, naam batao.")
    url, how = resolve_website(target)
    where = open_url_in_browser(url, args.get("browser", ""))
    if how == "search_page":
        return f"'{target}' ki exact site nahi mili, {where} me uska Google search khol diya."
    return f"{where} me '{target}' ki site khol di ({url})."


@tool(
    "web_search",
    "Search something on Google in a browser.",
    {"query": {"type": "string", "required": True, "description": "What to search."},
     "browser": _BROWSER_PARAM},
)
def _web_search(args: dict) -> str:
    q = str(args.get("query") or "").strip()
    if not q:
        raise ToolError("Kya search karna hai?")
    url = "https://www.google.com/search?q=" + _urlparse.quote_plus(q)
    where = open_url_in_browser(url, args.get("browser", ""))
    return f"{where} me '{q}' search kar diya."


@tool(
    "youtube_play",
    "Play a song or video on YouTube directly (opens the top result, not just the "
    "search page). Use this whenever the user mentions YouTube.",
    {"query": {"type": "string", "required": True, "description": "Song / video name."},
     "browser": _BROWSER_PARAM},
)
def _youtube_play(args: dict) -> str:
    q = str(args.get("query") or "").strip()
    if not q:
        raise ToolError("YouTube par kya chalana hai, naam batao.")
    vid = _youtube_top_video(q)
    if vid:
        where = open_url_in_browser(f"https://www.youtube.com/watch?v={vid}&autoplay=1",
                                    args.get("browser", ""))
        return f"'{q}' YouTube par chala diya ({where})."
    where = open_url_in_browser(
        "https://www.youtube.com/results?search_query=" + _urlparse.quote_plus(q),
        args.get("browser", ""))
    return f"'{q}' ka YouTube search khol diya ({where}) — internet slow hai to pehla video khud chalao."


TOOL_RISK.update({"open_website": "safe", "web_search": "safe", "youtube_play": "safe"})


# ======================================================================
# WHATSAPP MESSAGING
#
# Pehle ISHA ke paas WhatsApp par message bhejne ka koi tool tha hi nahi:
# "rahul ko whatsapp karo" bolne par sirf WhatsApp app/site khul jaata tha.
# Ye block asli bhejna karta hai:
#
#   1. Contact ka naam  -> whatsapp_contacts.json se number nikalo.
#   2. Number mila      -> WhatsApp Desktop (whatsapp://send) ya WhatsApp Web
#                          (web.whatsapp.com/send) me chat + message pre-fill,
#                          window focus karo, Enter dabao.
#   3. Number nahi mila -> WhatsApp Desktop me naam search karke chat kholo
#                          aur message paste karo. Galat insaan ko na chala
#                          jaaye, isliye Enter tabhi dabta hai jab config me
#                          "whatsapp_autosend_by_name": true ho.
#
# Config keys (isha_config.json, sab optional):
#   whatsapp_country_code     "91"    10-digit number ke aage lagta hai
#   whatsapp_mode             "auto"  auto | desktop | web
#   whatsapp_desktop_wait     8       Desktop chat khulne ka max intezaar (sec)
#   whatsapp_web_wait         25      WhatsApp Web load hone ka intezaar (sec)
#   whatsapp_autosend_by_name false   naam-search wale raaste me khud Enter dabaye?
# ======================================================================

_WA_CONTACTS_FILE = PROJECT_ROOT / "whatsapp_contacts.json"
_WA_LOCK = threading.Lock()


def _wa_cfg(key: str, default):
    try:
        return load_config().get(key, default)
    except Exception:
        return default


def _wa_load_contacts() -> dict:
    try:
        if _WA_CONTACTS_FILE.exists():
            data = json.loads(_WA_CONTACTS_FILE.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        _log_error("whatsapp:load_contacts", e)
    return {}


def _wa_save_contacts(contacts: dict) -> None:
    _WA_CONTACTS_FILE.write_text(
        json.dumps(contacts, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")


def _wa_normalize_phone(raw: str):
    """'+91 98765-43210' / '098765 43210' / '9876543210' -> '919876543210'.
    Galat lage to None."""
    s = str(raw or "").strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits = digits[2:]
    cc = re.sub(r"\D", "", str(_wa_cfg("whatsapp_country_code", "91"))) or "91"
    if len(digits) == 11 and digits.startswith("0"):
        digits = cc + digits[1:]
    elif len(digits) == 10 and not s.startswith("+"):
        digits = cc + digits
    if 11 <= len(digits) <= 15:
        return digits
    return None


def _wa_looks_like_phone(text: str) -> bool:
    t = re.sub(r"[\s\-()]", "", str(text or ""))
    return bool(re.fullmatch(r"\+?\d{8,15}", t))


def _wa_lookup(name: str):
    """Naam -> (saved_naam, phone) ya None. Exact, phir milta-julta naam."""
    contacts = _wa_load_contacts()
    if not contacts:
        return None
    key = re.sub(r"\s+", " ", str(name or "").strip().lower())
    lowered = {k.lower(): k for k in contacts}
    if key in lowered:
        k = lowered[key]
        return k, contacts[k]
    close = difflib.get_close_matches(key, list(lowered), n=1, cutoff=0.8)
    if close:
        k = lowered[close[0]]
        return k, contacts[k]
    partial = [k for low, k in lowered.items() if key and (key in low.split() or low.startswith(key))]
    if len(partial) == 1:
        return partial[0], contacts[partial[0]]
    return None


def _wa_desktop_available() -> bool:
    if _IS_WINDOWS:
        try:
            import winreg
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "whatsapp"))
            return True
        except Exception:
            return False
    if _IS_MACOS:
        return Path("/Applications/WhatsApp.app").exists()
    return False            # Linux par official desktop app nahi hai -> Web


def _wa_open_uri(uri: str) -> None:
    if _IS_WINDOWS:
        os.startfile(uri)                       # noqa: protocol handler
    elif _IS_MACOS:
        subprocess.Popen(["open", uri])
    else:
        subprocess.Popen(["xdg-open", uri])


def _wa_focus(title: str, timeout: float) -> bool:
    """Title me `title` wali window ko aage lao. Mil gayi to True."""
    end = time.time() + max(1.0, float(timeout))
    while time.time() < end:
        try:
            if _os_controller.switch_to_window(title).get("success"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _wa_key(*keys) -> bool:
    """Ek key ya hotkey dabao: _wa_key('enter'), _wa_key('ctrl', 'v')."""
    if _PYAUTOGUI_OK:
        try:
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
            return True
        except Exception as e:
            _log_error("whatsapp:key", e)
    if _IS_WINDOWS and _WIN32_OK:
        vk = {"enter": 0x0D, "ctrl": 0x11, "v": 0x56, "f": 0x46, "esc": 0x1B, "down": 0x28}
        try:
            codes = [vk[k] for k in keys]
        except KeyError:
            return False
        for c in codes:
            win32api.keybd_event(c, 0, 0, 0)
        for c in reversed(codes):
            win32api.keybd_event(c, 0, win32con.KEYEVENTF_KEYUP, 0)
        return True
    return False


def _wa_paste_text(text: str) -> bool:
    """Clipboard ke through paste — Hindi/emoji bhi sahi jaata hai, jo
    pyautogui.write() nahi kar paata."""
    try:
        TOOL_REGISTRY["clipboard_write"]["handler"]({"text": text})
    except Exception as e:
        _log_error("whatsapp:clipboard", e)
        return False
    time.sleep(0.2)
    return _wa_key("command" if _IS_MACOS else "ctrl", "v")


def _wa_send_via_desktop(phone: str, message: str) -> str:
    uri = f"whatsapp://send?phone={phone}&text={_urlparse.quote(message, safe='')}"
    _wa_open_uri(uri)
    wait = float(_wa_cfg("whatsapp_desktop_wait", 8))
    time.sleep(1.5)
    if not _wa_focus("WhatsApp", wait):
        return ("WhatsApp Desktop me chat khol di aur message likh diya, par window "
                "focus nahi hui — ek baar Enter khud daba do.")
    time.sleep(1.5)                     # message box bharne do
    if not _wa_key("enter"):
        return "Chat khul gayi aur message likha hai, Enter khud dabao (pyautogui nahi mila)."
    return None


def _wa_send_via_web(phone: str, message: str, browser: str = "") -> str:
    url = f"https://web.whatsapp.com/send?phone={phone}&text={_urlparse.quote(message, safe='')}"
    where = open_url_in_browser(url, browser)
    wait = float(_wa_cfg("whatsapp_web_wait", 25))
    # WhatsApp Web ko chat load karne me time lagta hai; title "WhatsApp" hote hi
    # bhi message box turant ready nahi hota.
    time.sleep(min(wait, 8.0))
    focused = _wa_focus("WhatsApp", max(2.0, wait - 8.0))
    if not focused:
        return (f"{where} me WhatsApp Web khol diya aur message likh diya. Agar QR code "
                f"dikh raha hai to pehle phone se scan karo, phir Enter dabao.")
    time.sleep(3.0)
    if not _wa_key("enter"):
        return f"{where} me chat khul gayi, Enter khud dabao (pyautogui nahi mila)."
    return None


def _wa_send_by_name_search(name: str, message: str) -> str:
    """Number nahi pata -> WhatsApp Desktop me naam search karke chat kholo."""
    _wa_open_uri("whatsapp:")
    if not _wa_focus("WhatsApp", float(_wa_cfg("whatsapp_desktop_wait", 8))):
        raise ToolError("WhatsApp Desktop khula to, par window focus nahi ho paayi.")
    time.sleep(1.2)
    mod = "command" if _IS_MACOS else "ctrl"
    _wa_key("esc")                      # purana search/popup band
    time.sleep(0.3)
    _wa_key(mod, "f")                   # chat search
    time.sleep(0.6)
    _wa_paste_text(name)
    time.sleep(1.8)                     # search results aane do
    _wa_key("enter")                    # pehla result kholo
    time.sleep(1.2)
    _wa_paste_text(message)
    if bool(_wa_cfg("whatsapp_autosend_by_name", False)):
        time.sleep(0.5)
        _wa_key("enter")
        return (f"WhatsApp par '{name}' search karke message bhej diya. Number save kar do "
                f"taaki agli baar pakka sahi insaan ko jaaye.")
    return (f"WhatsApp me '{name}' ki chat khol ke message likh diya hai — sahi chat hai "
            f"to Enter daba do. '{name}' ka number save kar do (\"{name} ka number "
            f"98xxxxxxxx save karo\") to agli baar main seedha bhej dungi.")


@tool(
    "send_whatsapp_message",
    "Send a WhatsApp message to a contact. `contact` is a saved contact name "
    "(e.g. 'Rahul', 'Mummy') or a phone number. Use this whenever the user asks "
    "to message / text / WhatsApp someone.",
    {"contact": {"type": "string", "required": False,
                 "description": "Saved contact name or phone number."},
     "message": {"type": "string", "required": False,
                 "description": "Exact text to send."},
     "browser": _BROWSER_PARAM},
)
def _send_whatsapp_message(args: dict) -> str:
    contact = str(args.get("contact") or "").strip().strip("\"'")
    message = str(args.get("message") or "").strip()
    if not contact:
        raise ToolError("Kisko WhatsApp message bhejna hai? Naam ya number batao.")
    if not message:
        raise ToolError(f"{contact} ko kya message bhejna hai?")
    if not _PYAUTOGUI_OK and not (_IS_WINDOWS and _WIN32_OK):
        raise ToolError("Keyboard control nahi hai — pip install pyautogui karo.")

    if _wa_looks_like_phone(contact):
        label, phone = contact, _wa_normalize_phone(contact)
        if not phone:
            raise ToolError(f"'{contact}' sahi phone number nahi lag raha.")
    else:
        hit = _wa_lookup(contact)
        label, phone = (hit[0], _wa_normalize_phone(hit[1])) if hit else (contact, None)

    mode = str(_wa_cfg("whatsapp_mode", "auto")).lower()
    desktop = _wa_desktop_available() and mode in ("auto", "desktop")

    with _WA_LOCK:                      # do sends ek saath keyboard na chheen le
        if phone:
            if desktop:
                note = _wa_send_via_desktop(phone, message)
            else:
                note = _wa_send_via_web(phone, message, args.get("browser", ""))
            return note or f"WhatsApp par {label} ko message bhej diya: \"{message}\""
        if desktop:
            return _wa_send_by_name_search(contact, message)
    raise ToolError(
        f"'{contact}' ka number saved nahi hai aur WhatsApp Desktop nahi mila. "
        f"Bolo \"{contact} ka number 98xxxxxxxx save karo\", phir dobara bhejo.")


@tool(
    "save_whatsapp_contact",
    "Save (or update) a contact's phone number for WhatsApp messaging.",
    {"name": {"type": "string", "required": True, "description": "Contact name, e.g. 'Rahul'."},
     "phone": {"type": "string", "required": True,
               "description": "Phone number, with or without country code."}},
)
def _save_whatsapp_contact(args: dict) -> str:
    name = re.sub(r"\s+", " ", str(args.get("name") or "")).strip(" \"'.,")
    phone = _wa_normalize_phone(args.get("phone"))
    if not name:
        raise ToolError("Contact ka naam batao.")
    if not phone:
        raise ToolError(f"'{args.get('phone')}' sahi phone number nahi lag raha.")
    contacts = _wa_load_contacts()
    for k in list(contacts):            # same naam alag case me ho to replace
        if k.lower() == name.lower():
            del contacts[k]
    contacts[name] = phone
    _wa_save_contacts(contacts)
    return f"{name} ka WhatsApp number +{phone} save kar diya."


@tool("list_whatsapp_contacts", "List contacts saved for WhatsApp messaging.")
def _list_whatsapp_contacts(args: dict) -> str:
    contacts = _wa_load_contacts()
    if not contacts:
        return "Abhi koi WhatsApp contact saved nahi hai."
    return "Saved WhatsApp contacts: " + ", ".join(
        f"{k} (+{v})" for k, v in sorted(contacts.items(), key=lambda kv: kv[0].lower()))


TOOL_RISK.update({"send_whatsapp_message": "confirm",
                  "save_whatsapp_contact": "safe",
                  "list_whatsapp_contacts": "safe"})


# ---- Hinglish/English command -> WhatsApp tool ------------------------------

_WA_WORD_RE = re.compile(r"\b(?:whats\s*app|watsapp|whatsap|watsap|whatapp|व्हाट्सएप)\b", re.I)
_WA_SEND_RE = re.compile(
    r"\b(message|msg|massage|mesej|messej|text|bhej\w*|send|likh\w*|bol\w*|reply)\b"
    r"|\bko\b.*\b(?:whats\s*app|watsapp)\s+kar\w*", re.I)
_WA_PLACE_RE = re.compile(
    r"\b(?:on|par|pe|per|pr|me|mein|se)?\s*(?:whats\s*app|watsapp|whatsap|watsap|whatapp|व्हाट्सएप)"
    r"\s*(?:par|pe|per|pr|me|mein|on|pr)?\b", re.I)
_WA_VERB_TAIL_RE = re.compile(
    r"\s*\b(?:(?:ye|yeh|ek|ik)\s+)?(?:(?:message|msg|massage|text)\s+)?"
    r"(?:bhej\w*|send|likh\w*|bol\w*|kar\w*|de\s*do|dena|do|dijiye|please|plz)"
    r"(?:\s+(?:do|de|dena|dijiye|karo|kar\s*do|please|plz))*\s*$", re.I)
_WA_VERB_HEAD_RE = re.compile(
    r"^\s*(?:(?:ek|ik)\s+)?(?:message|msg|massage|text|mesej|messej)?\s*"
    r"(?:bhej\w*|send\w*|kar\w*|likh\w*)?(?:\s+(?:do|de|dena|dijiye))?\s*", re.I)
_WA_CONNECTOR_RE = re.compile(r"\s+(?:ki|ke|that|saying|likho|likh\s*do|bolo)\s+|\s*[:\-–]\s+", re.I)
_WA_NAME_NOISE_RE = re.compile(
    r"^(?:isha|jarvis|please|plz|zara|jara|mere|meri|mera|apne|apni|my)\s+", re.I)


def _wa_strip_quotes(t: str) -> str:
    return t.strip().strip("\"'“”‘’ ").strip()


def parse_whatsapp_command(text: str):
    """'rahul ko whatsapp par message karo ki main late aaunga'
         -> ('send_whatsapp_message', {'contact': 'rahul', 'message': 'main late aaunga'})
       'rahul ka number 9876543210 save karo'
         -> ('save_whatsapp_contact', {...})
       Jo WhatsApp ka command nahi hai -> None."""
    s = (text or "").strip(" .!?")
    if not s:
        return None

    # --- contact save ---
    m = re.search(r"^(?:isha\s+)?(.+?)\s+(?:ka|ki|ke)\s+(?:whats\s*app\s+)?(?:number|no\.?|nambar|namber|num)"
                  r"\s+(\+?[\d][\d\s\-]{7,}\d)\s+(?:save|add|jod|yaad|store|likh)\w*", s, re.I)
    if m:
        name = _WA_NAME_NOISE_RE.sub("", m.group(1)).strip()
        return ("save_whatsapp_contact", {"name": name, "phone": m.group(2)})
    m = re.search(r"\bsave\s+(\+?[\d][\d\s\-]{7,}\d)\s+as\s+(.+)$", s, re.I)
    if m:
        return ("save_whatsapp_contact", {"name": m.group(2).strip(), "phone": m.group(1)})

    if re.search(r"\b(whats\s*app|watsapp)\s+(contacts?|contact\s+list)\b", s, re.I) and \
       re.search(r"\b(dikhao|batao|list|show)\b", s, re.I):
        return ("list_whatsapp_contacts", {})

    if not _WA_WORD_RE.search(s) or not _WA_SEND_RE.search(s):
        return None

    # Pehle quoted message nikal lo, taaki usme ke "ko/ki" parse na bigaade.
    quoted = re.search(r"[\"“']([^\"”']{1,1000})[\"”']", s)
    msg = quoted.group(1).strip() if quoted else ""
    body = s[:quoted.start()] + " " + s[quoted.end():] if quoted else s
    body = re.sub(r"\s+", " ", _WA_PLACE_RE.sub(" ", body)).strip()

    contact, rest = "", ""
    # Hinglish: "<naam> ko ..."
    m = re.search(r"^(.*?)\s*\bko\b\s*(.*)$", body, re.I)
    if m and m.group(1).strip():
        contact, rest = m.group(1).strip(), m.group(2).strip()
        # "whatsapp par message karo rahul ko ..." -> naam se pehle ke verbs hatao
        contact = _WA_VERB_HEAD_RE.sub("", contact).strip()
    else:
        # English: "send (a) message to <name> saying ...", "message <name> ..."
        m = re.search(r"\bto\s+(.+?)(?:\s+(?:saying|that)\s+|\s*[:\-–]\s+|$)(.*)$", body, re.I) or \
            re.search(r"^(?:message|msg|text)\s+(\S+(?:\s+\S+)?)\s+(?:saying|that)\s+(.*)$", body, re.I)
        if m:
            contact, rest = m.group(1).strip(), m.group(2).strip()
            if not msg and rest:
                msg = rest
                rest = ""

    contact = _WA_NAME_NOISE_RE.sub("", _wa_strip_quotes(contact)).strip(" ,.")
    if not msg and rest:
        parts = _WA_CONNECTOR_RE.split(rest, maxsplit=1)
        if len(parts) == 2:
            msg = parts[1]
            msg = _WA_VERB_TAIL_RE.sub("", msg) if re.search(
                r"\b(bhej|send)\w*(\s+(do|de|dijiye))?\s*$", msg, re.I) else msg
        else:
            msg = _WA_VERB_HEAD_RE.sub("", rest)
            msg = _WA_VERB_TAIL_RE.sub("", msg)
    msg = _wa_strip_quotes(msg)

    # Naam me bache hue command-shabd ("message", "send") ho to hata do.
    contact = re.sub(r"\b(message|msg|text|send|bhejo|karo)\b", " ", contact, flags=re.I)
    contact = re.sub(r"\s+", " ", contact).strip(" ,.")
    if len(contact.split()) > 4:        # itna lamba naam = parse galat hua
        contact = ""

    args = {}
    if contact:
        args["contact"] = contact
    if msg:
        args["message"] = msg
    return ("send_whatsapp_message", args)


# ---- splitting a compound command into single tasks ------------------------

_SPLIT_RE = re.compile(
    r"\s*(?:[,;]|\baur\s+(?:phir|fir|fhir)\b|\buske\s+baad\b|\biske\s+baad\b|"
    r"\bafter\s+that\b|\bsaath\s+hi\b|\bthen\b|\bphir\b|\bfir\b|\bfhir\b|"
    r"\baur\b|\band\b)\s*",
    re.IGNORECASE)

_ACTION_VERB_RE = re.compile(
    r"\b(khol\w*|open\w*|bana\w*|bnao|create|play|baja\w*|chala\w*|lagao|laga\s*do|"
    r"sunao|suna\s*do|search|dhund\w*|dhoond\w*|khojo|band|close|batao|bata\s*do|"
    r"check|set|start|launch|chalu|kar\w*|do|dijiye|delete|hata\w*|mute|unmute|"
    r"lock|minimize|maximize|screenshot|le\s*lo|run|execute|likh\w*|write|copy|paste)\b",
    re.IGNORECASE)


def split_command(text: str) -> list:
    """'X banao aur Y kholo fir Z chalao' -> ['X banao', 'Y kholo', 'Z chalao'].

    Jis tukde me koi action verb nahi (jaise 'Tom and Jerry' ka 'Tom'), use
    agle tukde se jod dete hain — Hinglish me verb aakhir me aata hai."""
    parts = [p.strip(" .!?") for p in _SPLIT_RE.split(text or "") if p and p.strip(" .!?")]
    if len(parts) <= 1:
        return parts
    merged, carry = [], ""
    for p in parts:
        piece = f"{carry} {p}".strip() if carry else p
        if _ACTION_VERB_RE.search(piece):
            merged.append(piece)
            carry = ""
        else:
            carry = piece
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]} {carry}"
        else:
            merged.append(carry)
    return merged


# ---- deterministic single-task matcher --------------------------------------

_SITE_WORD_RE = re.compile(r"\b(site|sait|side|website|web\s*site|webpage|web\s*page|portal)\b", re.I)
_OPEN_WORD_RE = re.compile(r"\b(khol\w*|open\w*|visit|jao|chala\w*|dikhao|launch)\b", re.I)
_YT_RE = re.compile(r"\b(youtube|you\s*tube|yt|utube)\b", re.I)
_CMD_NOISE_RE = re.compile(
    r"\b(me|mein|main|mai|pe|par|per|on|in|ko|ki|ka|ke|se|wali|wala|vali|official|the|"
    r"khol\w*|open\w*|karo|kardo|kar\s*do|kar|do|dijiye|please|plz|isha|jarvis|visit|jao|"
    r"chala\w*|lagao|laga|dikhao|launch|start|zara|jara|ek|mera|meri|my|"
    r"play|baja\w*|sunao|suna|song|songs|gaana|gana|gaane|music|video|videos)\b",
    re.I)


def _clean_target(text: str) -> str:
    t = _BROWSER_RE.sub(" ", text)
    t = _YT_RE.sub(" ", t)
    t = _SITE_WORD_RE.sub(" ", t)
    t = _CMD_NOISE_RE.sub(" ", t)
    t = re.sub(r"[\"'“”]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" ,.!?-")


def match_task(segment: str):
    """Ek single kaam -> (tool_name, args), ya None agar pakka pata nahi."""
    s = (segment or "").strip(" ,.!?")
    if not s:
        return None
    low = s.lower()
    bm = _BROWSER_RE.search(low)
    browser = _normalize_browser(bm.group(1)) if bm else ""
    b_arg = {"browser": browser} if browser else {}

    # 0) WhatsApp — "rahul ko whatsapp par hello bhejo"
    wa = parse_whatsapp_command(s)
    if wa is not None:
        return wa

    # 0b) memory, file search, software, projects, model control, system info
    agent_hit = match_agent_command(s)
    if agent_hit is not None:
        return agent_hit

    # 1) YouTube — "youtube par rehbara song play karo"
    if _YT_RE.search(low):
        q = _clean_target(s)
        if q:
            return ("youtube_play", {"query": q, **b_arg})
        return ("open_website", {"target": "youtube", **b_arg})

    # 2) Explicit URL / domain
    m = re.search(r"(https?://\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|in|org|net|io|ai|dev|co|edu|gov|app)(?:/\S*)?)",
                  s, re.I)
    if m and (browser or _OPEN_WORD_RE.search(low)):
        return ("open_website", {"target": m.group(1), **b_arg})

    # 3) Google search — "chrome me arduino tutorial search karo"
    if re.search(r"\b(search|dhund\w*|dhoond\w*|khojo|google\s+kar\w*)\b", low) and \
       not re.search(r"\b(file|files|folder)\b", low):
        q = re.sub(r"\b(search|dhund\w*|dhoond\w*|khojo|google)\b", " ", s, flags=re.I)
        q = _clean_target(q)
        if q:
            return ("web_search", {"query": q, **b_arg})

    # 4) "chrome me elegoo giga ki site open karo" / "X ki website kholo"
    if _SITE_WORD_RE.search(low) and (browser or _OPEN_WORD_RE.search(low)):
        target = _clean_target(s)
        if target:
            return ("open_website", {"target": target, **b_arg})

    # 5) "chrome me X kholo" — browser + open verb + koi naam jo app nahi hai
    if browser and _OPEN_WORD_RE.search(low):
        target = _clean_target(s)
        if target:
            return ("open_website", {"target": target, **b_arg})
        return ("open_app", {"app_name": browser})

    # 6) "chrome band karo" / "close notepad"
    m = re.search(r"^(?:isha\s+)?(.{2,30}?)\s+(?:ko\s+)?(?:band|close)\s*(?:kar\w*|do)?$", low) or \
        re.search(r"^(?:close|quit|exit)\s+(.{2,30})$", low)
    if m:
        name = m.group(1).strip()
        if name not in ("it", "this", "yeh", "isko", "sab", "sabko") and \
           not re.search(r"\b(gaana|gana|song|music|volume|awaaz)\b", name):
            return ("close_application", {"app_name": name})
    # 7) Purana quick matcher (folder, notepad, volume, song, open_app ...)
    quick = match_quick_command(s)
    if quick is not None:
        return quick

    # 8) Chhote system sawaal
    if re.search(r"\b(time|samay|baje)\b", low) and not re.search(r"\btimer\b", low):
        return ("get_time", {})
    if re.search(r"\b(date|tarikh|tareekh)\b", low):
        return ("get_date", {})
    if re.search(r"screen\s*shot", low):
        return ("take_screenshot", {})
    if re.search(r"\b(wifi|wi-fi)\b", low):
        return ("get_wifi_status", {})
    if re.search(r"\b(storage|disk\s*space|space\s+kitna|free\s+space)\b", low):
        return ("get_storage_info", {})

    return None


def plan_request(text: str) -> list:
    """Poori command -> ordered steps [{'segment', 'tool', 'args'}].
    tool=None ka matlab: is hisse ke liye LLM decide karega."""
    wa = parse_whatsapp_command(text)
    if wa is not None:
        # Message ke andar "aur", "phir", comma aa sakte hain — ise todna nahi.
        return [{"segment": text, "tool": wa[0], "args": dict(wa[1])}]
    steps = []
    # Working memory carries the browser across turns: "Chrome kholo" ...
    # (next message) "ab GitHub kholo" -> GitHub opens in Chrome.
    recent = (time.time() - WORKING.updated) < 900
    last_browser = WORKING.last_browser if recent else ""
    for seg in (split_command(text) or [text]):
        hit = match_task(seg)
        tool_name = hit[0] if hit else None
        args = dict(hit[1]) if hit else {}
        if tool_name == "open_app" and last_browser:
            site = re.sub(r"^(ab|phir|fir|ab\s+ye)\s+", "", str(args.get("app_name", "")).lower()).strip()
            if site in _KNOWN_SITES and site not in ("whatsapp", "spotify"):
                tool_name, args = "open_website", {"target": site}
        # Context yaad rakho: "chrome kholo, phir github kholo" -> github bhi Chrome me.
        if tool_name in ("open_website", "web_search", "youtube_play"):
            if args.get("browser"):
                last_browser = args["browser"]
            elif last_browser:
                args["browser"] = last_browser
        elif tool_name == "open_app" and _normalize_browser(args.get("app_name", "")):
            last_browser = _normalize_browser(args["app_name"])
        steps.append({"segment": seg, "tool": tool_name, "args": args})
    return steps


# Model jab tool call ke bajaye "main X tool use karunga / Result: ho gaya"
# likhe, use pakadne ke liye. Aisa text kabhi user ko success bata kar nahi
# dikhaya jayega.
_FAKE_ACTION_RE = re.compile(
    r"(tool\s+ka\s+(?:upyog|use|istemal)|\bupyog\s+kar\w*|command\s+execute|"
    r"execute\s+kar\w*|\bkarunga\b|\bkarungi\b|\bkaruga\b|\bResult\s*:|"
    r"I\s+will\s+(?:open|create|use|call|play|run)|I(?:'ll| will)\s+now|"
    r"let me\s+(?:open|create|call|use)|\bban\s+gaya\s+hai\b|\bkhul\s+gay\w*\b|"
    r"open\s+ho\s+gay\w*|play\s+ho\s+raha|chal\s+raha\s+hai|\bho\s+gaya\s+hai\b)",
    re.IGNORECASE)


def looks_like_fake_action(text: str) -> bool:
    """True jab model ne kaam KIYA nahi, sirf uske baare me LIKHA."""
    t = text or ""
    if _FAKE_ACTION_RE.search(t):
        return True
    return any(re.search(rf"\b{re.escape(n)}\b", t) for n in TOOL_REGISTRY)


# ======================================================================

# ======================================================================
# ISHA AGENT CORE v2
#
# Purpose of this block (added on top of the existing tool layer, nothing
# below it was removed):
#   1. A capability registry so the model can *discover* what it can do
#      instead of us hard-coding another if/elif chain for every phrase.
#   2. A tool-name resolver, so a model that asks for "clean_recycle_bin"
#      or "empty_trash" still lands on empty_recycle_bin() instead of
#      getting "no such tool" and giving up.
#   3. Self-diagnostics, so ISHA knows on startup what is actually
#      available on this machine.
#   4. The OS capabilities the old tool list was missing: clipboard,
#      screenshots, folders, directory listing, app closing, storage,
#      network, keyboard automation.
# ======================================================================

# ---- 1. Capability taxonomy -------------------------------------------------
# Every tool gets a category. The category list is what goes into the system
# prompt as a compact "here is the shape of your body" summary; the full JSON
# schema still goes through the model's native `tools` array.

TOOL_CATEGORY = {
    "apps":     ("open_notepad", "open_calculator", "open_browser", "open_task_manager",
                 "open_app", "close_application", "open_folder"),
    "windows":  ("minimize_window", "maximize_window", "close_window", "switch_window",
                 "list_open_windows", "lock_screen"),
    "files":    ("search_files", "advanced_file_search", "list_media_files",
                 "read_file_content", "write_file_content", "move_or_rename_file",
                 "delete_file_safely", "batch_delete_files", "create_desktop_folder",
                 "create_folder", "list_directory", "empty_recycle_bin"),
    "system":   ("check_cpu_usage", "check_memory_usage", "check_battery",
                 "get_system_stats", "get_gpu_usage", "get_storage_info",
                 "list_running_processes", "kill_process", "system_power_action",
                 "isha_self_check"),
    "network":  ("get_wifi_status", "get_network_info", "open_url", "close_browser_tab"),
    "media":    ("play_song", "pause_song", "resume_song", "stop_song",
                 "mute_system", "set_volume"),
    "display":  ("set_brightness", "take_screenshot"),
    "input":    ("type_text", "press_hotkey", "clipboard_read", "clipboard_write"),
    "memory":   ("search_long_term_memory", "store_long_term_memory"),
    "code":     ("create_and_save_code", "write_and_save_code", "run_code_file",
                 "list_and_run_desktop_code", "autonomous_code_agent",
                 "run_terminal_command"),
    "meta":     ("get_time", "get_date", "list_capabilities"),
    "comms":    ("send_whatsapp_message", "save_whatsapp_contact", "list_whatsapp_contacts"),
}

# Intent words -> canonical tool. This is NOT phrase matching for the user's
# message; it is a repair table for when the *model* invents a plausible tool
# name. Small models do this constantly and the old code just failed.
TOOL_ALIASES = {
    "whatsapp": "send_whatsapp_message", "send_whatsapp": "send_whatsapp_message",
    "whatsapp_message": "send_whatsapp_message", "send_message": "send_whatsapp_message",
    "message_contact": "send_whatsapp_message", "send_text": "send_whatsapp_message",
    "whatsapp_send": "send_whatsapp_message", "send_wa_message": "send_whatsapp_message",
    "add_contact": "save_whatsapp_contact", "save_contact": "save_whatsapp_contact",
    "list_contacts": "list_whatsapp_contacts",
    "clean_recycle_bin": "empty_recycle_bin", "empty_trash": "empty_recycle_bin",
    "clear_recycle_bin": "empty_recycle_bin", "recycle_bin": "empty_recycle_bin",
    "delete_recycle_bin": "empty_recycle_bin", "empty_bin": "empty_recycle_bin",
    "open_application": "open_app", "launch_app": "open_app", "start_app": "open_app",
    "run_app": "open_app", "open_program": "open_app", "launch_application": "open_app",
    "close_app": "close_application", "quit_app": "close_application",
    "terminate_app": "close_application", "exit_application": "close_application",
    "make_folder": "create_folder", "mkdir": "create_folder",
    "create_directory": "create_folder", "new_folder": "create_folder",
    "list_files": "list_directory", "ls": "list_directory", "dir": "list_directory",
    "find_files": "advanced_file_search", "search_file": "advanced_file_search",
    "file_search": "advanced_file_search", "find_file": "advanced_file_search",
    "get_ram": "check_memory_usage", "memory": "check_memory_usage",
    "ram_usage": "check_memory_usage", "check_ram": "check_memory_usage",
    "cpu": "check_cpu_usage", "cpu_usage": "check_cpu_usage",
    "disk_usage": "get_storage_info", "storage": "get_storage_info",
    "check_storage": "get_storage_info", "free_space": "get_storage_info",
    "disk_space": "get_storage_info", "get_disk_info": "get_storage_info",
    "screenshot": "take_screenshot", "capture_screen": "take_screenshot",
    "screen_capture": "take_screenshot",
    "copy_to_clipboard": "clipboard_write", "set_clipboard": "clipboard_write",
    "get_clipboard": "clipboard_read", "paste": "clipboard_read",
    "volume": "set_volume", "set_system_volume": "set_volume",
    "brightness": "set_brightness",
    "shutdown": "system_power_action", "restart": "system_power_action",
    "reboot": "system_power_action", "sleep": "system_power_action",
    "network": "get_network_info", "wifi": "get_wifi_status",
    "ip_address": "get_network_info",
    "processes": "list_running_processes", "task_list": "list_running_processes",
    "kill": "kill_process", "end_process": "kill_process",
    "minimize": "minimize_window", "maximize": "maximize_window",
    "windows": "list_open_windows", "active_windows": "list_open_windows",
    "open_website": "open_url", "browse": "open_url", "goto_url": "open_url",
    "remember": "store_long_term_memory", "recall": "search_long_term_memory",
    "what_can_you_do": "list_capabilities", "capabilities": "list_capabilities",
    "self_check": "isha_self_check", "diagnostics": "isha_self_check",
    "shell": "run_terminal_command", "cmd": "run_terminal_command",
    "powershell": "run_terminal_command", "execute_command": "run_terminal_command",
}


def resolve_tool_name(name: str):
    """Map whatever the model asked for onto a real registered tool.

    Order: exact -> normalised -> alias table -> fuzzy match against both the
    registry and the alias keys. Returns None only when nothing is close, and
    the caller then hands the model the valid list instead of a dead end.
    """
    raw = (name or "").strip()
    if not raw:
        return None
    if raw in TOOL_REGISTRY:
        return raw

    norm = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    if norm in TOOL_REGISTRY:
        return norm
    if norm in TOOL_ALIASES and TOOL_ALIASES[norm] in TOOL_REGISTRY:
        return TOOL_ALIASES[norm]

    # functions.open_app / tools.open_app / open_app() style wrappers
    tail = norm.split("_")[-1] if "_" in norm else norm
    for candidate in (norm.replace("isha_", ""), norm.replace("tool_", ""), tail):
        if candidate in TOOL_REGISTRY:
            return candidate
        if candidate in TOOL_ALIASES and TOOL_ALIASES[candidate] in TOOL_REGISTRY:
            return TOOL_ALIASES[candidate]

    pool = list(TOOL_REGISTRY) + list(TOOL_ALIASES)
    near = difflib.get_close_matches(norm, pool, n=1, cutoff=0.72)
    if near:
        hit = near[0]
        hit = TOOL_ALIASES.get(hit, hit)
        if hit in TOOL_REGISTRY:
            return hit

    # last resort: shared-token overlap ("make_a_folder_on_desktop")
    tokens = set(t for t in norm.split("_") if len(t) > 2)
    best, best_score = None, 0
    for tname in TOOL_REGISTRY:
        score = len(tokens & set(tname.split("_")))
        if score > best_score:
            best, best_score = tname, score
    return best if best_score >= 2 else None


def suggest_tools_for(name: str, limit: int = 6) -> str:
    """Human/model-readable 'did you mean' list for an unresolvable call."""
    norm = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    near = difflib.get_close_matches(norm, list(TOOL_REGISTRY), n=limit, cutoff=0.4)
    if not near:
        near = list(TOOL_REGISTRY)[:limit]
    return ", ".join(near)


def capability_summary() -> str:
    """One compact line per category — this is what the model reads to decide
    whether a capability plausibly exists before it gives up on a request."""
    lines = []
    try:
        blocked = set(load_config().get("blocked_tools") or [])
    except Exception:
        blocked = set()
    for cat, names in TOOL_CATEGORY.items():
        live = [n for n in names if n in TOOL_REGISTRY and n not in blocked]
        if live:
            lines.append(f"  {cat}: {', '.join(live)}")
    extra = [n for n in TOOL_REGISTRY if n not in blocked
             if not any(n in names for names in TOOL_CATEGORY.values())]
    if extra:
        lines.append(f"  other: {', '.join(sorted(extra))}")
    return "\n".join(lines)


# ---- 2. Controlled OS execution gateway ------------------------------------
# The model never gets a raw shell. Every new capability below goes through one
# of these two helpers, which are the only places that touch PowerShell, and
# both take a fixed script with parameters rather than a model-authored string.

def _ps_run(script: str, timeout: int = 25) -> dict:
    """Run a *fixed, internally authored* PowerShell script. Never called with
    text that came from the model."""
    if not _IS_WINDOWS:
        return _result(False, "PowerShell is Windows-only.")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and not out:
            return _result(False, err or f"PowerShell exit {proc.returncode}")
        return _result(True, out or "OK", {"stdout": out, "stderr": err})
    except subprocess.TimeoutExpired:
        return _result(False, "PowerShell command timed out.")
    except Exception as e:
        return _result(False, f"PowerShell error: {e}")


def _safe_resolve_path(raw: str) -> Path:
    """Expand ~, %USERPROFILE%, and friendly names like 'desktop'/'downloads'
    into a real path. Keeps the model from having to guess absolute paths."""
    try:
        return isha_paths.resolve_user_path(raw)
    except ValueError as e:
        raise ToolError(str(e))


# ---- 3. New OS capability tools --------------------------------------------

@tool("list_capabilities",
      "List everything ISHA can actually do on this machine, grouped by category. "
      "Call this when unsure whether a capability exists before telling the user no.",
      {"category": {"type": "string", "required": False,
                    "description": "Optional filter: apps, windows, files, system, "
                                   "network, media, display, input, memory, code, meta."}})
def _list_capabilities(args: dict) -> str:
    cat = (args.get("category") or "").strip().lower()
    cat = globals().get("_CATEGORY_ALIASES", {}).get(cat, cat)
    if cat and cat in TOOL_CATEGORY:
        blocked = set(load_config().get("blocked_tools") or [])
        live = [n for n in TOOL_CATEGORY[cat] if n in TOOL_REGISTRY and n not in blocked]
        return f"{cat}: " + (", ".join(live) or "nothing available")
    return "ISHA capabilities:\n" + capability_summary()


@tool("isha_self_check",
      "Run ISHA's self-diagnostics: which subsystems, models and libraries are "
      "actually available right now.")
def _isha_self_check(args: dict) -> str:
    return format_self_diagnostics(run_self_diagnostics())


@tool("get_storage_info",
      "Get disk / storage usage for every drive: total, used and free space.")
def _get_storage_info(args: dict) -> str:
    if not _PSUTIL_OK:
        raise ToolError("psutil missing — pip install psutil")
    rows = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        gb = 1024 ** 3
        rows.append(f"{part.device or part.mountpoint}: "
                    f"{u.free / gb:.1f} GB free of {u.total / gb:.1f} GB "
                    f"({u.percent:.0f}% used)")
    if not rows:
        raise ToolError("Koi drive read nahi hui.")
    return "Storage:\n" + "\n".join(rows)


@tool("get_network_info",
      "Get network details: hostname, local IP addresses, and per-interface status.")
def _get_network_info(args: dict) -> str:
    lines = [f"Hostname: {platform.node()}"]
    try:
        import socket
        lines.append(f"Primary IP: {socket.gethostbyname(socket.gethostname())}")
    except Exception:
        pass
    if _PSUTIL_OK:
        try:
            stats = psutil.net_if_stats()
            for iface, addrs in psutil.net_if_addrs().items():
                ips = [a.address for a in addrs if getattr(a, "family", None).__str__().endswith("AF_INET")]
                up = "up" if (iface in stats and stats[iface].isup) else "down"
                if ips:
                    lines.append(f"{iface} ({up}): {', '.join(ips)}")
        except Exception as e:
            lines.append(f"[interface enumeration failed: {e}]")
    return "\n".join(lines)


@tool("list_open_windows",
      "List the titles of all currently open application windows.")
def _list_open_windows_tool(args: dict) -> str:
    res = _os_controller.list_open_windows()
    if not res["success"]:
        raise ToolError(res["message"])
    wins = (res.get("data") or {}).get("windows") or []
    if not wins:
        return "Koi open window nahi mila."
    titles = [w.get("title", "") if isinstance(w, dict) else str(w) for w in wins]
    titles = [t for t in titles if t.strip()][:30]
    return "Open windows:\n" + "\n".join(f"- {t}" for t in titles)


@tool("create_folder",
      "Create a folder anywhere the user can write. Accepts friendly locations "
      "like 'desktop', 'downloads/reports' or a full absolute path.",
      {"path": {"type": "string", "required": True,
                "description": "Parent location: 'desktop', 'downloads', or an absolute path."},
       "folder_name": {"type": "string", "required": True,
                       "description": "Name of the new folder."}})
def _create_folder(args: dict) -> str:
    parent = _safe_resolve_path(args["path"])
    name = str(args["folder_name"]).strip().strip('"').strip("'")
    if not name or any(ch in name for ch in '\\/:*?"<>|'):
        raise ToolError(f"'{name}' folder name ke liye valid nahi hai.")
    if not parent.exists():
        raise ToolError(f"Location nahi mila: {parent}")
    target = parent / name
    if target.exists():
        return f"Folder pehle se maujood hai: {target}"
    try:
        target.mkdir(parents=True)
    except Exception as e:
        raise ToolError(f"Folder nahi bana: {e}")
    return f"Folder bana diya: {target}"


@tool("list_directory",
      "List the contents of a folder, newest first. Use this before acting on "
      "files so paths are never guessed.",
      {"path": {"type": "string", "required": True,
                "description": "'desktop', 'downloads', or an absolute folder path."},
       "extension": {"type": "string", "required": False,
                     "description": "Optional filter, e.g. 'pdf' or '.png'."},
       "max_results": {"type": "integer", "required": False,
                       "description": "Cap on entries returned (default 40)."}})
def _list_directory(args: dict) -> str:
    folder = _safe_resolve_path(args["path"])
    if not folder.is_dir():
        raise ToolError(f"'{folder}' koi folder nahi hai.")
    ext = (args.get("extension") or "").strip().lstrip(".").lower()
    limit = max(1, min(int(args.get("max_results") or 40), 200))
    try:
        entries = list(folder.iterdir())
    except PermissionError:
        raise ToolError(f"'{folder}' read karne ki permission nahi hai.")
    if ext:
        entries = [p for p in entries if p.suffix.lower().lstrip(".") == ext]
    entries.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if not entries:
        return f"{folder} mein" + (f" .{ext}" if ext else "") + " kuch nahi mila."
    rows = []
    for p in entries[:limit]:
        try:
            kind = "DIR " if p.is_dir() else f"{p.stat().st_size / 1024:.0f}KB"
            when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            kind, when = "?", "?"
        rows.append(f"- {p.name}  [{kind}, {when}]")
    more = f"\n(+{len(entries) - limit} more)" if len(entries) > limit else ""
    return f"{folder} ({len(entries)} items):\n" + "\n".join(rows) + more


@tool("open_folder",
      "Open a folder in the file manager (Explorer / Finder / Nautilus).",
      {"path": {"type": "string", "required": True,
                "description": "'desktop', 'downloads', or an absolute folder path."}})
def _open_folder(args: dict) -> str:
    folder = _safe_resolve_path(args["path"])
    if not folder.exists():
        raise ToolError(f"Path nahi mila: {folder}")
    try:
        if _IS_WINDOWS:
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as e:
        raise ToolError(f"Folder open nahi hua: {e}")
    return f"Khol diya: {folder}"


@tool("close_application",
      "Close a running application by name (gracefully first). Use this for "
      "'Chrome band karo' style requests.",
      {"app_name": {"type": "string", "required": True,
                    "description": "App or process name, e.g. 'chrome', 'notepad', 'spotify'."}})
def _close_application(args: dict) -> str:
    name = str(args["app_name"]).strip().lower().replace(".exe", "")
    if not name:
        raise ToolError("App name khaali hai.")
    # Try a polite window close first so unsaved work gets a prompt.
    closed_windows = 0
    try:
        res = _os_controller.close_window(name)
        if res.get("success"):
            closed_windows += 1
    except Exception:
        pass
    if not _PSUTIL_OK:
        if closed_windows:
            return f"'{name}' ka window band kar diya."
        raise ToolError("psutil missing — pip install psutil")
    killed = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pname = (proc.info.get("name") or "").lower().replace(".exe", "")
            if pname == name or (len(name) > 3 and name in pname):
                proc.terminate()
                killed.append(f"{proc.info['name']} (pid {proc.info['pid']})")
        except Exception:
            continue
    if not killed and not closed_windows:
        raise ToolError(f"'{name}' naam ka koi running application nahi mila.")
    if not killed:
        return f"'{name}' ka window band kar diya."
    return f"Band kar diya: {', '.join(killed[:6])}" + \
           (f" (+{len(killed) - 6} aur)" if len(killed) > 6 else "")


@tool("take_screenshot",
      "Capture the current screen and save it as a PNG file.")
def _take_screenshot(args: dict) -> str:
    res = hand_take_screenshot()
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool("clipboard_read", "Read the current text contents of the system clipboard.")
def _clipboard_read(args: dict) -> str:
    try:
        import pyperclip
        text = pyperclip.paste()
        return f"Clipboard: {text[:1500]}" if text else "Clipboard khaali hai."
    except Exception:
        pass
    if _IS_WINDOWS:
        res = _ps_run("Get-Clipboard")
        if res["success"]:
            out = (res.get("data") or {}).get("stdout", "")
            return f"Clipboard: {out[:1500]}" if out.strip() else "Clipboard khaali hai."
        raise ToolError(res["message"])
    raise ToolError("Clipboard read nahi ho paaya — pip install pyperclip")


@tool("clipboard_write", "Put text onto the system clipboard.",
      {"text": {"type": "string", "required": True,
                "description": "Text to copy to the clipboard."}})
def _clipboard_write(args: dict) -> str:
    text = str(args["text"])
    try:
        import pyperclip
        pyperclip.copy(text)
        return f"Clipboard par copy kar diya ({len(text)} chars)."
    except Exception:
        pass
    if _IS_WINDOWS:
        # Pipe via stdin so the text is never interpolated into a script string.
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "$input | Set-Clipboard"],
                           input=text, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return f"Clipboard par copy kar diya ({len(text)} chars)."
        except Exception as e:
            raise ToolError(f"Clipboard write fail: {e}")
    raise ToolError("Clipboard write nahi ho paaya — pip install pyperclip")


@tool("type_text",
      "Type text into whatever window currently has focus, as if from the keyboard.",
      {"text": {"type": "string", "required": True, "description": "Text to type."}})
def _type_text(args: dict) -> str:
    if not _PYAUTOGUI_OK:
        raise ToolError("pyautogui missing — pip install pyautogui")
    text = str(args["text"])
    if len(text) > 2000:
        raise ToolError("Text bahut lamba hai (2000 chars limit).")
    try:
        pyautogui.typewrite(text, interval=0.01)
    except Exception as e:
        raise ToolError(f"Typing fail: {e}")
    return f"Type kar diya ({len(text)} chars)."


_ALLOWED_HOTKEYS = {
    "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+a", "ctrl+s",
    "ctrl+f", "ctrl+n", "ctrl+t", "ctrl+w", "ctrl+shift+t", "ctrl+p",
    "alt+tab", "win", "win+d", "win+e", "win+l", "enter", "esc", "tab",
    "f5", "f11", "pageup", "pagedown", "home", "end",
}


@tool("press_hotkey",
      "Press a keyboard shortcut in the focused window. Only common, reversible "
      "shortcuts are allowed.",
      {"keys": {"type": "string", "required": True,
                "description": "Shortcut like 'ctrl+s', 'alt+tab', 'win+e', 'f5'."}})
def _press_hotkey(args: dict) -> str:
    if not _PYAUTOGUI_OK:
        raise ToolError("pyautogui missing — pip install pyautogui")
    combo = str(args["keys"]).strip().lower().replace(" ", "").replace("-", "+")
    combo = combo.replace("windows", "win").replace("control", "ctrl")
    if combo not in _ALLOWED_HOTKEYS:
        raise ToolError(f"'{combo}' allowed shortcuts mein nahi hai. "
                        f"Allowed: {', '.join(sorted(_ALLOWED_HOTKEYS))}")
    try:
        pyautogui.hotkey(*combo.split("+"))
    except Exception as e:
        raise ToolError(f"Hotkey fail: {e}")
    return f"Press kar diya: {combo}"


# ---- 4. Risk tiers for the new tools ---------------------------------------
# Anything unlisted already defaults to "confirm"; these are the deliberate
# decisions. Read-only and trivially reversible actions stay unattended so the
# safe path is not buried under confirmation prompts.
TOOL_RISK.update({
    "list_capabilities":  "safe",
    "isha_self_check":    "safe",
    "get_storage_info":   "safe",
    "get_network_info":   "safe",
    "list_open_windows":  "safe",
    "list_directory":     "safe",
    "open_folder":        "safe",
    "create_folder":      "safe",
    "take_screenshot":    "safe",
    "clipboard_read":     "safe",
    "clipboard_write":    "safe",
    "close_application":  "confirm",   # can lose unsaved work
    "type_text":          "confirm",   # goes into an unknown focused window
    "press_hotkey":       "confirm",
})


# ---- 5. Self-diagnostics ----------------------------------------------------

def run_self_diagnostics(cfg: dict = None) -> dict:
    """What is actually working on this machine, right now.

    Runs at startup and is exposed as a tool, so when the user asks
    "kya tum screenshot le sakti ho?" the answer comes from a probe rather
    than from the model's imagination.
    """
    cfg = cfg or {}
    model_path = resolve_gguf_path(cfg)
    report = {
        "os": f"{_OS_NAME} {platform.release()} ({platform.machine()})",
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
        "runtime_python": sys.executable,
        "tools_registered": len(TOOL_REGISTRY),
        "llama_cpp": bool(_LLAMA_CPP_OK),
        "llama_cpp_error": ("" if _LLAMA_CPP_OK else
                             (_GGUF_BACKEND_DIAGNOSTIC.get("error") or _LLAMA_CPP_ERR)),
        "model_path": str(model_path),
        "model_present": model_path.is_file(),
        "model_size_gb": 0.0,
        "model_loaded": GGUF.is_loaded(),
        "available_models": [p.name for p in find_gguf_models()],
        "threads": _auto_threads(),
        "gpu_layers": int(cfg.get("gpu_layers", 0) or 0),
        "subsystems": {},
        "missing": [],
    }
    if report["model_present"]:
        try:
            report["model_size_gb"] = round(model_path.stat().st_size / (1024 ** 3), 2)
        except Exception:
            pass

    if not _LLAMA_CPP_OK:
        report["missing"].append("llama-cpp-python (pip install llama-cpp-python)")
    if not report["model_present"]:
        report["missing"].append(f"GGUF model not found at {model_path}")
    if report["model_present"] and _model_is_unsuitable(model_path.name):
        report["missing"].append(
            f"'{model_path.name}' is a reasoning/base model — tool calling will misbehave")

    subs = report["subsystems"]
    subs["text-to-speech"] = bool(_TTS_OK)
    subs["microphone"] = bool(_STT_OK)
    subs["system-control (psutil)"] = bool(_PSUTIL_OK)
    subs["screen/keyboard (pyautogui)"] = bool(_PYAUTOGUI_OK)
    subs["windows-api (pywin32)"] = bool(globals().get("_WIN32_OK", _IS_WINDOWS))
    subs["audio-mixer (pycaw)"] = bool(globals().get("_PYCAW_OK", False))
    subs["hand-gestures"] = hand_backend_name() != "none"
    subs["long-term-memory"] = _vector_memory is not None
    try:
        import cv2 as _cv2  # noqa: F401
        subs["camera-stack (opencv)"] = True
    except Exception:
        subs["camera-stack (opencv)"] = False

    for label, ok in subs.items():
        if not ok:
            report["missing"].append(label)
    try:
        report["status"] = _component_status(cfg, report)
    except Exception as e:  # diagnostics must never break startup
        report["status"] = {"diagnostics": ("DEGRADED", f"status probe failed: {e}")}
    return report


def _component_status(cfg: dict, rep: dict) -> dict:
    """READY / DEGRADED / UNAVAILABLE per component, from real probes only."""
    R, D, U = "READY", "DEGRADED", "UNAVAILABLE"
    st = {}
    hw = _hw(0)
    st["Python"] = (R, rep["python"])
    st["OS"] = (R, rep["os"])
    st["CPU"] = (R, f"{hw.cpu_physical or '?'} cores, {hw.cpu_name or 'unknown'}")
    if hw.ram_total_gb is None:
        st["RAM"] = (D, "unknown (psutil missing)")
    else:
        st["RAM"] = (R if hw.ram_available_gb > 2 else D,
                     f"{hw.ram_available_gb:.1f} GB free of {hw.ram_total_gb:.1f} GB")
    st["GPU"] = (R, ", ".join(g.name for g in hw.gpus)) if hw.gpus else (D, "none detected - CPU inference")
    st["VRAM"] = (R, f"{hw.total_vram_gb:.1f} GB") if hw.total_vram_gb else (D, "not detectable")
    reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
    roles = reg.available_roles()
    broken = [r["role"] for r in reg.describe() if r["status"] == "MISSING FILE"]
    if not roles:
        st["Model files"] = (U, f"no .gguf found (put one in {MODELS_DIR})")
    else:
        st["Model files"] = (D if broken else R, "roles: " + ", ".join(roles)
                             + (f"; missing file for: {', '.join(broken)}" if broken else ""))
    st["Model backend"] = (R, f"llama.cpp ({_LLAMA_MODE})") if _LLAMA_CPP_OK else         (U, rep.get("llama_cpp_error") or "llama.cpp backend not available")
    st["TTS"] = (D, "disabled in settings") if not cfg.get("tts_enabled", True) else \
        ((R, _tts_diagnostics()) if _TTS_OK else (U, "no TTS engine"))
    st["STT"] = (D, "disabled in settings") if not cfg.get("stt_enabled", True) else \
        ((R, "SpeechRecognition") if _STT_OK else (U, "pip install SpeechRecognition PyAudio"))
    try:
        import pyaudio  # noqa: F401
        st["Microphone"] = (R, "PyAudio present") if _STT_OK else (D, "PyAudio present, STT missing")
    except Exception:
        st["Microphone"] = (U, "PyAudio not installed")
    st["Camera"] = (R, "OpenCV present (device not opened)") if subs_ok(rep, "camera-stack (opencv)") \
        else (U, "opencv-python not installed")
    idx = get_file_index(create=False)
    if not bool(cfg.get("file_index_enabled", True)):
        st["File search"] = (D, "index disabled - direct scan only")
    elif idx is not None and idx.ready:
        st["File search"] = (R, f"index ready, {idx.count()} entries")
    else:
        st["File search"] = (D, "index building / not built yet - direct scan fallback")
    st["Memory"] = (R, f"{len(LONG_MEMORY.items())} facts, vector store: "
                       f"{getattr(_vector_memory, 'backend', 'none')}")
    st["Tool registry"] = (R, f"{len(TOOL_REGISTRY)} tools") if TOOL_REGISTRY else (U, "empty")
    wa_desktop = _wa_desktop_available()
    keys_ok = _PYAUTOGUI_OK or (_IS_WINDOWS and _WIN32_OK)
    if not keys_ok:
        st["WhatsApp"] = (U, "keyboard automation missing (pip install pyautogui)")
    else:
        st["WhatsApp"] = (R if wa_desktop else D,
                          "WhatsApp Desktop found" if wa_desktop else "WhatsApp Web via browser (needs QR login)")
    st["Browser automation"] = (R, "webbrowser + keyboard") if keys_ok else (D, "open URLs only")
    st["System control"] = (R, "psutil") if _PSUTIL_OK else (D, "psutil missing")
    managers = isha_sw.detect_managers()
    st["Software install"] = (R, ", ".join(managers)) if managers else (U, "no package manager found")
    vm, vp = _vision_paths()
    v = isha_vision.status(vm, vp, _LLAMA_MODE)
    st["Vision"] = (v["state"], "local vision model" if v["model"] else
                    (v["model_reason"] + ("; OCR available" if v["ocr"] else "; OCR: " + v["ocr_reason"])))
    return st


def subs_ok(rep: dict, key: str) -> bool:
    return bool((rep.get("subsystems") or {}).get(key))


def format_self_diagnostics(rep: dict) -> str:
    lines = [
        f"OS: {rep['os']}",
        f"Python: {rep['python']}",
        f"Runtime Python: {rep.get('runtime_python', sys.executable)}",
        f"Tools registered: {rep['tools_registered']}",
        f"llama-cpp-python: {'ok' if rep['llama_cpp'] else 'MISSING'}"
        + (f" — {rep['llama_cpp_error']}" if not rep['llama_cpp'] and rep.get('llama_cpp_error') else ""),
        f"GGUF model: {Path(rep['model_path']).name}"
        + (f" ({rep['model_size_gb']} GB)" if rep["model_present"] else "  — NOT FOUND"),
        f"Inference: {rep['threads']} threads, {rep['gpu_layers']} GPU layers",
    ]
    if rep["available_models"]:
        lines.append(f"Models found: {', '.join(rep['available_models'][:6])}")
    if rep.get("status"):
        lines.append("Components:")
        for name, (state, detail) in rep["status"].items():
            lines.append(f"  {state:<11} {name}: {detail}")
    else:
        ok = [k for k, v in rep["subsystems"].items() if v]
        bad = [k for k, v in rep["subsystems"].items() if not v]
        if ok:
            lines.append("Working: " + ", ".join(ok))
        if bad:
            lines.append("Unavailable: " + ", ".join(bad))
    return "\n".join(lines)


def print_self_diagnostics(cfg: dict = None) -> dict:
    rep = run_self_diagnostics(cfg)
    print("\n[ISHA self-check]")
    for line in format_self_diagnostics(rep).splitlines():
        print(f"  {line}")
    if rep["missing"]:
        print("  Needs attention:")
        for m in rep["missing"][:10]:
            print(f"    - {m}")
    print("")
    return rep


# ======================================================================
# ISHA AGENT CORE v3 — orchestration layer
#
#   User -> intent -> planner (plan_request / match_agent_command)
#        -> model router (pick_cfg_for)  -> tool selector
#        -> permission gateway (run_tool_gated) -> execute_tool -> observation
#
# Everything below builds on the v2 registry above; nothing was removed.
# The heavy lifting lives in the headless isha_core package.
# ======================================================================

ISHA_DATA_DIR = Path(os.environ.get("ISHA_DATA_DIR") or PROJECT_ROOT)
try:
    ISHA_DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    ISHA_DATA_DIR = PROJECT_ROOT

ACTION_LOG = isha_audit.ActionLog(ISHA_DATA_DIR / "isha_audit.jsonl")
ACTIVE_MODEL = {"name": "", "role": "", "task": "", "mode": "auto", "notes": ""}
WORKING = isha_mem.WorkingMemory()
LONG_MEMORY = isha_mem.LongTermMemory(ISHA_DATA_DIR / "isha_long_memory.json")
_FILE_INDEX = None
_FILE_INDEX_LOCK = threading.Lock()
_HW_CACHE = {"info": None, "at": 0.0}


def _cfg() -> dict:
    try:
        return load_config()
    except Exception:
        return isha_config.merge_defaults(dict(DEFAULT_CONFIG))


def _hw(max_age: float = 5.0):
    now = time.time()
    if _HW_CACHE["info"] is None or now - _HW_CACHE["at"] > max_age:
        _HW_CACHE["info"] = isha_hw.probe()
        _HW_CACHE["at"] = now
    return _HW_CACHE["info"]


def _index_roots(cfg: dict) -> list:
    roots = [str(r) for r in (cfg.get("file_index_roots") or []) if str(r).strip()]
    return roots or [str(Path.home())]


def get_file_index(create: bool = True):
    global _FILE_INDEX
    with _FILE_INDEX_LOCK:
        if _FILE_INDEX is None and create:
            cfg = _cfg()
            if not bool(cfg.get("file_index_enabled", True)):
                return None
            try:
                _FILE_INDEX = isha_index.FileIndex(ISHA_DATA_DIR / "isha_file_index.db",
                                                   _index_roots(cfg), cfg.get("file_index_excludes"))
            except Exception as e:
                _log_error("file_index:init", e)
                return None
        return _FILE_INDEX


def close_file_index():
    """Stop indexing and release the SQLite handle (app shutdown)."""
    global _FILE_INDEX
    with _FILE_INDEX_LOCK:
        idx, _FILE_INDEX = _FILE_INDEX, None
    if idx is not None:
        idx.close()


def get_search_engine():
    cfg = _cfg()
    return isha_search.FileSearchEngine(get_file_index(), _index_roots(cfg), cfg.get("file_index_excludes"))


# ---- approval previews ------------------------------------------------------
def _describe_special(name: str, args: dict) -> str:
    if name == "send_whatsapp_message":
        who = args.get("contact") or "?"
        return f"{who} ko WhatsApp par ye message bhejna hai:\n\"{args.get('message', '')}\"\nSend karun?"
    if name in ("install_software", "uninstall_software", "update_software"):
        action = {"install_software": "install", "uninstall_software": "uninstall",
                  "update_software": "update"}[name]
        sp = isha_sw.plan(action, str(args.get("name", "")),
                          preferred=str(_cfg().get("preferred_package_manager") or ""))
        return sp.describe() + (f"\nCommand: {' '.join(sp.argv)}" if sp.argv else "")
    if name == "run_terminal_command":
        return "Terminal command " + isha_cmd.analyze_command(str(args.get("command", ""))).describe()
    if name == "empty_recycle_bin":
        st = _SYSTEM_CONTROLLER.trash_status()
        return (st.get("message", "Recycle Bin") + " Sab kuch PERMANENTLY delete ho jayega. Empty karun?")
    if name == "delete_file_safely":
        return f"Yeh permanently delete hoga:\n{args.get('file_path')}"
    if name == "batch_delete_files":
        return "Yeh items Recycle Bin/Trash me jayenge:\n" + str(args.get("file_paths", "")).replace(",", "\n")
    if name == "write_file_content":
        p = str(args.get("file_path", ""))
        exists = False
        try:
            exists = _safe_resolve_path(p).exists()
        except Exception:
            pass
        return f"{'OVERWRITE' if exists else 'Create'} file: {p} ({len(str(args.get('content', '')))} chars)"
    if name in ("run_project", "fix_code_file"):
        target = args.get("project") or args.get("file_path")
        n = args.get("max_attempts") or _cfg().get("max_auto_fix_attempts", 3)
        fix = "" if args.get("auto_fix") is False else f", error aaye to {n} baar tak auto-fix"
        return f"Code run karna hai: {target}{fix}. (Generated code - aap trust karte ho?)"
    if name == "system_power_action":
        return f"System {args.get('action')} karna hai - unsaved kaam kho sakta hai."
    if name == "move_or_rename_file":
        return f"Move/rename:\n{args.get('source_path')}\n-> {args.get('dest_path')}"
    return ""


# ---- permission gateway -----------------------------------------------------
# Tools whose handler additionally wants confirm=true. When the USER approved
# in the dialog, that IS the confirmation — the old code made the model pass
# it too, so an approved delete could still refuse to run.
_CONFIRM_ARG_TOOLS = {"delete_file_safely": "confirm", "batch_delete_files": "confirm",
                      "system_power_action": "confirm"}


def run_tool_gated(name: str, args: dict, ask=None, cfg: dict = None, pre_approved: bool = False):
    """Validation -> risk -> permission -> execute -> audit -> working memory.

    ask(tool, args, risk) -> True (approved) / False (denied) / None (timeout).
    Returns (ok, text, info). Text starts with ERROR / DENIED / CANCELLED on
    failure, which the agent prompt already knows how to treat.
    """
    cfg = cfg if cfg is not None else _cfg()
    resolved = resolve_tool_name(name)
    if resolved is None:
        return False, (f"ERROR: no tool named '{name}'. Closest matches: {suggest_tools_for(name)}. "
                       f"Call list_capabilities to see every capability."), {}
    name, args = resolved, dict(args or {})
    risk = tool_risk(name)
    if pre_approved:
        decision = isha_perm.Decision(isha_perm.ALLOW, risk, "approved by user")
        approval = "user"
    else:
        decision = isha_perm.decide(name, risk, cfg)
        approval = "auto"
        if decision.action == isha_perm.DENY:
            ACTION_LOG.record("approval", tool=name, args=args, risk=risk, approval="blocked",
                              reason=decision.reason)
            return False, f"DENIED: {decision.reason}. Do not retry.", {"risk": risk, "approval": "blocked"}
        if decision.action == isha_perm.ASK:
            if ask is None:
                return False, "DENIED: this action needs the user's approval.", {"risk": risk}
            ACTION_LOG.set_current(step=f"waiting for approval: {name}")
            answer = ask(name, args, risk)
            if answer is None:
                ACTION_LOG.record("approval", tool=name, args=args, risk=risk, approval="timeout")
                return False, ("CANCELLED: the user did not respond in time. Do not retry; "
                               "say the action was cancelled."), {"risk": risk, "approval": "timeout"}
            if not answer:
                ACTION_LOG.record("approval", tool=name, args=args, risk=risk, approval="denied")
                return False, ("DENIED: the user refused this action. Do NOT retry it or attempt a "
                               "workaround. Stop and tell them it was cancelled."), \
                    {"risk": risk, "approval": "denied"}
            approval = "user"
        elif risk != "safe":
            approval = "trusted"
    if ISHA_STOP.stopped:
        return False, "CANCELLED: stopped by user.", {"risk": risk, "approval": approval}
    if approval in ("user", "trusted") and name in _CONFIRM_ARG_TOOLS:
        args[_CONFIRM_ARG_TOOLS[name]] = True
    if approval == "user":
        ACTION_LOG.record("approval", tool=name, args=args, risk=risk, approval="approved")
    _TOOL_CTX.approval = approval
    ACTION_LOG.set_current(tool=name, step=describe_tool_call(name, args)[:80])
    try:
        out, ok = str(execute_tool(name, args)), True
    except ToolError as e:
        out, ok = f"ERROR: {e}", False
    except Exception as e:  # noqa: BLE001
        out, ok = f"ERROR: {type(e).__name__}: {e}", False
    finally:
        _TOOL_CTX.approval = "direct"
    if ok:
        try:
            WORKING.note_tool(name, args, out)
        except Exception:
            pass
    return ok, out, {"risk": risk, "approval": approval}


# ---- LLM helper with role routing (used inside tools) -----------------------
def _llm_for_role(role: str, prompt: str, system: str = "", max_tokens: int = 1200) -> str:
    cfg = _cfg()
    if not _LLAMA_CPP_OK:
        raise ToolError("Local model backend available nahi hai.")
    routed = dict(cfg)
    try:
        reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
        for r in isha_router.FALLBACK.get(role, [role, "general"]):
            e = reg.get(r)
            if e:
                routed["gguf_model_path"] = str(e.path)
                break
    except Exception as e:
        _log_error("llm_for_role", e)
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    opts = {"temperature": 0.2, "top_p": 0.9, "max_tokens": max_tokens}
    try:
        data = GGUF.complete(msgs, opts, routed)
    except GGUFUnavailable as e:
        raise ToolError(str(e))
    text = strip_thinking((data.get("message") or {}).get("content", ""))
    if not text.strip():
        raise ToolError("Local model se khaali response mila.")
    return text


def _code_fixer(code: str, error: str, language: str, path: str) -> str:
    ISHA_STOP.check()
    prompt = (f"This {language} file ({Path(path).name}) fails when run.\n\n--- CODE ---\n{code}\n\n"
              f"--- ERROR ---\n{error[-2500:]}\n\nFix the bug. Keep the program's purpose and behaviour. "
              f"Do not add shell commands, file deletion or network access. Respond with ONLY the complete "
              f"corrected source code, no explanation.")
    return _llm_for_role("coding", prompt, system=f"You are an expert {language} debugger. Output only code.",
                         max_tokens=2048)


# ---- new tools --------------------------------------------------------------
@tool("find_files",
      "Fast local file search in natural language (Hindi/Hinglish/English): name, extension, "
      "media type, size, date modified, folder. Uses the local file index when ready, else scans. "
      "Examples: 'Downloads mein PDF', '10 MB se badi videos', 'kal modify hui files', 'calculator.py on desktop'.",
      {"query": {"type": "string", "required": True, "description": "What to find, in the user's own words."},
       "limit": {"type": "integer", "required": False, "description": "Max results (default 20)."}})
def _find_files(args: dict) -> str:
    q = str(args.get("query") or "").strip()
    if not q:
        raise ToolError("Kya dhundna hai?")
    limit = max(1, min(int(args.get("limit") or 20), 100))
    res = get_search_engine().search_text(q, limit=limit, stop_flag=lambda: ISHA_STOP.stopped)
    return isha_search.format_results(res)


@tool("file_index_status", "Show the local file-search index status (entries, last update, state).")
def _file_index_status(args: dict) -> str:
    idx = get_file_index()
    if idx is None:
        return "File index band hai (settings: file_index_enabled) - search direct scan se hogi."
    s = idx.status()
    return (f"File index: {s['state']}, {s['entries']} entries, last complete: {s['last_complete'] or 'never'}"
            f"{' (paused)' if s['paused'] else ''}. Roots: {', '.join(s['roots'])}. DB stays local: {s['db']}")


@tool("rebuild_file_index", "Rebuild the local file-search index from scratch in the background.")
def _rebuild_file_index(args: dict) -> str:
    idx = get_file_index()
    if idx is None:
        raise ToolError("File index disabled hai.")
    idx.rebuild(background=True)
    return "File index rebuild background me shuru kar diya. Tab tak search direct scan se chalegi."


@tool("pause_file_index", "Pause or resume background file indexing.",
      {"pause": {"type": "boolean", "required": True, "description": "true = pause, false = resume."}})
def _pause_file_index(args: dict) -> str:
    idx = get_file_index()
    if idx is None:
        raise ToolError("File index disabled hai.")
    if args.get("pause"):
        idx.pause()
        return "File indexing pause kar di."
    idx.resume()
    if idx.state == "idle" and not idx.ready:
        idx.start_background()
    return "File indexing resume kar di."


@tool("copy_file", "Copy a file or folder to a new location. Never overwrites an existing target.",
      {"source_path": {"type": "string", "required": True, "description": "File/folder to copy."},
       "dest_path": {"type": "string", "required": True,
                     "description": "Destination folder (e.g. 'documents') or full new path."}})
def _copy_file(args: dict) -> str:
    src = _safe_resolve_path(args["source_path"])
    dst = _safe_resolve_path(args["dest_path"])
    if not src.exists():
        raise ToolError(f"Source nahi mila: {src}")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"Target pehle se hai, overwrite nahi karungi: {dst}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    except Exception as e:
        raise ToolError(f"Copy fail: {e}")
    if not dst.exists():
        raise ToolError("Copy ke baad target nahi mila.")
    return f"Copy ho gaya: {dst}"


@tool("create_file", "Create a NEW text file (refuses to overwrite; use write_file_content to overwrite).",
      {"path": {"type": "string", "required": True, "description": "e.g. 'desktop/notes.txt' or absolute path."},
       "content": {"type": "string", "required": False, "description": "Initial text content."}})
def _create_file(args: dict) -> str:
    p = _safe_resolve_path(args["path"])
    if p.exists():
        raise ToolError(f"File pehle se hai: {p}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(args.get("content") or ""), encoding="utf-8")
    except Exception as e:
        raise ToolError(f"File nahi bani: {e}")
    return f"File bana di: {p}"


def _software_tool(action: str, name: str) -> str:
    cfg = _cfg()
    sp = isha_sw.plan(action, name, preferred=str(cfg.get("preferred_package_manager") or ""))
    if not sp.ok and not sp.manual_command:
        raise ToolError(sp.error or f"'{name}' ka {action} plan nahi ban paaya.")
    ACTION_LOG.record("command", tool=f"{action}_software", argv=sp.argv, manager=sp.manager)
    res = isha_sw.execute(sp)
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool("install_software", "Install (or 'download') an application using the OS's official package manager "
      "(winget/choco/scoop on Windows, apt/dnf/pacman/flatpak/snap on Linux). Verifies after install.",
      {"name": {"type": "string", "required": True, "description": "App name, e.g. 'firefox', 'vs code', 'vlc'."}})
def _install_software(args: dict) -> str:
    return _software_tool("install", args["name"])


@tool("uninstall_software", "Uninstall an application via the official package manager, then verify it is gone.",
      {"name": {"type": "string", "required": True, "description": "App name."}})
def _uninstall_software(args: dict) -> str:
    return _software_tool("uninstall", args["name"])


@tool("update_software", "Update/upgrade an installed application via the official package manager.",
      {"name": {"type": "string", "required": True, "description": "App name."}})
def _update_software(args: dict) -> str:
    return _software_tool("update", args["name"])


@tool("search_software", "Search the package manager for an app name and list candidate packages.",
      {"name": {"type": "string", "required": True, "description": "App name to look up."}})
def _search_software(args: dict) -> str:
    res = isha_sw.search(args["name"])
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


def _resolve_project_dir(project: str) -> Path:
    p = (project or "").strip().strip("\"'")
    cands = []
    if p:
        try:
            cands.append(_safe_resolve_path(p))
        except ToolError:
            pass
        cands.append(_desktop_dir() / p)
        try:
            cands.append(_desktop_dir() / isha_code.safe_name(p))
        except ValueError:
            pass
    if WORKING.last_project:
        cands.append(Path(WORKING.last_project))
    for c in cands:
        if c.is_dir() or c.is_file():
            return c
    if p:
        # case-insensitive match on the Desktop, then the file index
        try:
            for child in _desktop_dir().iterdir():
                if child.is_dir() and child.name.lower() == p.lower():
                    return child
        except Exception:
            pass
        idx = get_file_index(create=False)
        if idx is not None and idx.ready:
            hits = idx.search(isha_index.SearchQuery(name_terms=[p], kind="dir", limit=5))
            exact = [h for h in hits if h["name"].lower() == p.lower()]
            if exact:
                return Path(exact[0]["path"])
    raise ToolError(f"'{project}' naam ka project nahi mila (Desktop aur last project dekhe).")


def _generate_project_files(kind: str, name: str, description: str):
    """Ask the coding model for the files; None if unavailable or unusable."""
    if not _LLAMA_CPP_OK:
        return None
    files = {}
    try:
        if kind == "web":
            for fname, what in (("index.html", "the complete index.html (link style.css and script.js)"),
                                ("style.css", "the complete style.css"),
                                ("script.js", "the complete script.js (no eval, no network calls)")):
                ISHA_STOP.check()
                prior = "\n\n".join(f"--- {k} ---\n{v}" for k, v in files.items())
                code = isha_code.strip_fences(_llm_for_role(
                    "coding", f"Project '{name}': {description}\n\n{prior}\n\nWrite {what}. Output only code.",
                    system="You are an expert front-end developer. Output only the requested file's code.",
                    max_tokens=2048))
                if len(code.strip()) < 20:
                    return None
                files[fname] = code
            return isha_code.ProjectSpec(name, "web", files, "index.html", source="model", description=description)
        code = isha_code.strip_fences(_llm_for_role(
            "coding", f"Write a complete, runnable Python 3 program for: {description}\n"
                      f"Use only the standard library. If it needs user input, read it with input(). "
                      f"Output only the code for main.py.",
            system="You are an expert Python developer. Output only code.", max_tokens=2048))
        if len(code.strip()) < 20:
            return None
        return isha_code.ProjectSpec(name, "python", {"main.py": code, "README.md": f"# {name}\n\n{description}\n"},
                                     "main.py", source="model", description=description)
    except (ToolError, ISHA_Cancelled) as e:
        _log_error("generate_project", e)
        return None


@tool("create_code_project",
      "Create a new coding project folder with real files (Python or HTML/CSS/JS). Uses the local coding "
      "model when available and falls back to a working template. Never overwrites an existing project.",
      {"project_name": {"type": "string", "required": True, "description": "Folder name, e.g. 'MyApp'."},
       "project_type": {"type": "string", "required": False, "description": "'python' (default) or 'web'."},
       "description": {"type": "string", "required": False, "description": "What the program should do."},
       "location": {"type": "string", "required": False, "description": "Parent folder (default 'desktop')."}})
def _create_code_project(args: dict) -> str:
    name = str(args.get("project_name") or "").strip()
    kind = str(args.get("project_type") or "python").strip().lower()
    kind = "web" if kind in ("web", "html", "website", "html/css/js", "javascript", "js") else "python"
    desc = str(args.get("description") or "").strip()
    base = _safe_resolve_path(args.get("location") or "desktop")
    try:
        isha_code.safe_name(name)
    except ValueError as e:
        raise ToolError(str(e))
    template = isha_code.choose_template(kind, name, desc)
    wants_custom = bool(desc) and not re.search(r"calc|hisab|hisaab", desc.lower() + name.lower()) and \
        len(re.sub(r"\b(project|website|web|python|html|css|js|bana\w*|create|desktop|par|naam|ka|ek|do)\b",
                   "", desc.lower()).split()) > 2
    spec = (_generate_project_files(kind, name, desc) if wants_custom else None) or template
    res = isha_code.write_project(base, spec)
    if not res["success"]:
        raise ToolError(res["message"])
    WORKING.last_project = res["path"]
    note = "" if spec.source == "model" else (
        " (template se banaya - local coding model available nahi tha)" if wants_custom else " (template)")
    return f"{res['message']}{note}. Entry: {Path(res['entry']).name}"


@tool("run_project",
      "Run a project or code file and verify it works. Python/JS/Bash are executed with a timeout; on error "
      "the local coding model fixes the code and retries (bounded). HTML opens in the browser.",
      {"project": {"type": "string", "required": False,
                   "description": "Project folder name on Desktop, a path, or empty for the last project."},
       "auto_fix": {"type": "boolean", "required": False, "description": "Fix errors automatically (default true)."},
       "max_attempts": {"type": "integer", "required": False, "description": "Run+fix attempts (default from settings)."},
       "open_window": {"type": "boolean", "required": False,
                       "description": "Also launch interactive/GUI programs for the user (default true)."}})
def _run_project(args: dict) -> str:
    cfg = _cfg()
    target = _resolve_project_dir(str(args.get("project") or ""))
    entry = target if target.is_file() else isha_code.find_entry(target)
    if entry is None:
        raise ToolError(f"'{target}' me chalane layak entry file (main.py / index.html ...) nahi mili.")
    WORKING.last_project = str(target if target.is_dir() else target.parent)
    if entry.suffix.lower() in (".html", ".htm"):
        webbrowser.open(entry.resolve().as_uri())
        return f"'{entry.name}' browser me khol diya: {entry}"
    meta = isha_code.load_meta(entry.parent)
    code = entry.read_text(encoding="utf-8", errors="replace")
    attempts = int(args.get("max_attempts") or cfg.get("max_auto_fix_attempts", 3))
    fixer = _code_fixer if (args.get("auto_fix", True) and _LLAMA_CPP_OK) else None
    gui = isha_code.is_gui_or_server(code)
    res = isha_code.fix_loop(entry, fixer=fixer, max_attempts=attempts,
                             timeout=6 if gui else int(cfg.get("code_run_timeout", 20)),
                             stdin_text=meta.get("test_input") or ("" if re.search(r"\binput\s*\(", code) else None),
                             on_step=lambda s: ACTION_LOG.set_current(step=s))
    ACTION_LOG.record("code_run", path=str(entry), status=res.status, attempts=res.attempts, log=res.log)
    msg = res.message()
    if not res.success:
        if fixer is None and res.status == "failed":
            msg += "\n(Auto-fix ke liye local coding model load hona chahiye.)"
        raise ToolError(msg)
    interactive = gui or bool(re.search(r"\binput\s*\(|sys\.stdin", code))
    if interactive and args.get("open_window", True) and (_IS_WINDOWS or gui or shutil.which("x-terminal-emulator")
                                                           or shutil.which("gnome-terminal")):
        launched = isha_code.launch_detached(entry)
        msg += "\n" + launched["message"]
    return msg


@tool("fix_code_file", "Run an existing code file; if it errors, read the error, fix the code with the local "
      "coding model and re-run, up to the configured number of attempts.",
      {"file_path": {"type": "string", "required": True, "description": "Path or Desktop file name, e.g. 'app.py'."},
       "max_attempts": {"type": "integer", "required": False, "description": "Attempts (default from settings)."},
       "test_input": {"type": "string", "required": False, "description": "Optional stdin to feed the program."}})
def _fix_code_file(args: dict) -> str:
    raw = str(args.get("file_path") or "").strip()
    p = None
    try:
        p = _safe_resolve_path(raw)
    except ToolError:
        pass
    if p is None or not p.is_file():
        cand = list(_desktop_dir().rglob(raw))[:1] if raw else []
        p = cand[0] if cand else None
    if p is None or not p.is_file():
        raise ToolError(f"File nahi mili: {raw}")
    if not _LLAMA_CPP_OK:
        raise ToolError("Bug fix ke liye local coding model chahiye, jo abhi load nahi hai.")
    cfg = _cfg()
    res = isha_code.fix_loop(p, fixer=_code_fixer,
                             max_attempts=int(args.get("max_attempts") or cfg.get("max_auto_fix_attempts", 3)),
                             timeout=int(cfg.get("code_run_timeout", 20)),
                             stdin_text=args.get("test_input"), on_step=lambda s: ACTION_LOG.set_current(step=s))
    ACTION_LOG.record("code_run", path=str(p), status=res.status, attempts=res.attempts, log=res.log)
    if not res.success:
        raise ToolError(res.message())
    return res.message()


@tool("inspect_project", "List the files of a project folder and its entry point, without running anything.",
      {"project": {"type": "string", "required": False, "description": "Project name/path (default: last project)."}})
def _inspect_project(args: dict) -> str:
    d = _resolve_project_dir(str(args.get("project") or ""))
    if d.is_file():
        d = d.parent
    files = [str(f.relative_to(d)) for f in sorted(d.rglob("*")) if f.is_file()
             and not any(part.startswith(".") for part in f.relative_to(d).parts)][:40]
    entry = isha_code.find_entry(d)
    return f"Project {d}\nEntry: {entry.name if entry else 'unknown'}\nFiles: " + ", ".join(files)


@tool("remember_fact", "Remember something the user EXPLICITLY asked to remember (preference, project, folder...). "
      "Stored locally. Refuses passwords/PINs/OTPs.",
      {"fact": {"type": "string", "required": True, "description": "The fact, in the user's words."},
       "category": {"type": "string", "required": False,
                    "description": "preference, project, folder, app, person or fact."}})
def _remember_fact(args: dict) -> str:
    res = LONG_MEMORY.remember(str(args.get("fact") or ""), str(args.get("category") or ""))
    if not res["success"]:
        raise ToolError(res["message"])
    try:
        _vector_memory.add_memory(res["item"]["text"] if res.get("item") else str(args["fact"]))
    except Exception:
        pass
    return res["message"]


@tool("forget_memory", "Forget a remembered fact (or 'all').",
      {"query": {"type": "string", "required": True, "description": "Words from the fact, or 'all'."}})
def _forget_memory(args: dict) -> str:
    res = LONG_MEMORY.forget(str(args.get("query") or ""))
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool("show_memory", "Show everything ISHA has been asked to remember.")
def _show_memory(args: dict) -> str:
    return LONG_MEMORY.format()


@tool("recall_conversation", "Recall what was discussed on a given day from local chat history.",
      {"day": {"type": "string", "required": False, "description": "'today', 'yesterday' or YYYY-MM-DD (default yesterday)."},
       "query": {"type": "string", "required": False, "description": "Optional topic words to filter by."}})
def _recall_conversation(args: dict) -> str:
    day = str(args.get("day") or "yesterday").strip().lower()
    from datetime import timedelta as _td
    if day in ("today", "aaj"):
        key = datetime.now().strftime("%Y-%m-%d")
    elif day in ("yesterday", "kal"):
        key = (datetime.now() - _td(days=1)).strftime("%Y-%m-%d")
    else:
        key = day
    entries = load_history().get(key) or []
    q = set(re.findall(r"\w+", str(args.get("query") or "").lower()))
    if q:
        entries = [e for e in entries if q & set(re.findall(r"\w+", e.get("text", "").lower()))]
    if not entries:
        return f"{key} ki koi baat-cheet history me nahi mili."
    lines = [f"{e.get('time', '')} {e.get('role')}: {str(e.get('text', ''))[:160]}" for e in entries[-12:]]
    return f"{key} ki baat-cheet (last {len(lines)}):\n" + "\n".join(lines)


@tool("get_hardware_info", "CPU, RAM, GPU/VRAM, disk and OS details of this PC.")
def _get_hardware_info(args: dict) -> str:
    return _hw(0).summary()


@tool("get_system_info", "Full system information: OS, hardware, storage, network, battery, ISHA models.")
def _get_system_info(args: dict) -> str:
    parts = [_hw(0).summary()]
    for fn in (_get_storage_info, _check_battery):
        try:
            parts.append(fn({}))
        except Exception:
            pass
    try:
        parts.append(_get_model_status({}))
    except Exception:
        pass
    return "\n".join(parts)


@tool("get_model_status", "Which local models are installed per role, which is active/loaded, and the backend.")
def _get_model_status(args: dict) -> str:
    cfg = _cfg()
    reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
    rows = []
    for r in reg.describe():
        extra = f" ({r['size_gb']} GB, {r['source']})" if r.get("path") else ""
        rows.append(f"  {r['role']}: {Path(r['path']).name if r['path'] else '-'} [{r['status']}]{extra}")
    loaded = ", ".join(GGUF.loaded_names()) or "none"
    return (f"Mode: {cfg.get('model_mode', 'auto')} | Backend: {_LLAMA_MODE} "
            f"({'ok' if _LLAMA_CPP_OK else 'UNAVAILABLE'}) | Loaded: {loaded} | "
            f"Last used: {ACTIVE_MODEL.get('name') or '-'} for {ACTIVE_MODEL.get('task') or '-'}\n"
            + "\n".join(rows))


@tool("switch_model", "Switch ISHA's model selection: auto, general, coding, reasoning, study, fast or vision.",
      {"mode": {"type": "string", "required": True, "description": "auto | general | coding | reasoning | study | fast | vision"}})
def _switch_model(args: dict) -> str:
    mode = str(args.get("mode") or "").strip().lower()
    mode = {"code": "coding", "coder": "coding", "reason": "reasoning", "chat": "general", "normal": "general",
            "automatic": "auto", "padhai": "study"}.get(mode, mode)
    if mode not in isha_router.MANUAL_MODES:
        raise ToolError(f"'{mode}' valid mode nahi hai. Options: {', '.join(isha_router.MANUAL_MODES)}")
    cfg = load_config()
    reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
    if mode != "auto" and reg.get(mode) is None:
        fb = next((r for r in isha_router.FALLBACK.get(mode, []) if reg.get(r)), None)
        note = f" '{mode}' model install nahi hai" + (f", isliye '{fb}' model use hoga." if fb else
                                                       " aur koi fallback bhi nahi hai.")
    else:
        note = ""
    cfg["model_mode"] = mode
    save_config(cfg)
    ACTION_LOG.record("model_mode", mode=mode)
    return f"Model mode ab '{mode}' hai.{note}"


def _vision_paths():
    cfg = _cfg()
    reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
    e = reg.get("vision")
    return (e.path if e else None), reg.vision_projector()


def _analyze_image(path: Path, question: str) -> str:
    model, proj = _vision_paths()
    st = isha_vision.status(model, proj, _LLAMA_MODE)
    if st["model"]:
        try:
            return isha_vision.describe_with_model(path, question or "Describe this image briefly.", model, proj,
                                                   n_threads=_auto_threads())
        except Exception as e:
            _log_error("vision:model", e)
            if not st["ocr"]:
                raise ToolError(f"Vision model fail hua: {e}")
    if st["ocr"]:
        text = isha_vision.ocr(path)
        head = "Vision model available nahi hai, isliye sirf text padha (OCR)"
        return f"{head}:\n{text[:2500] or '(koi text nahi mila)'}"
    raise ToolError("Image dekhne ki capability abhi nahi hai: " + st["model_reason"] +
                    "; OCR bhi nahi: " + st["ocr_reason"])


@tool("describe_image", "Look at an image file with the local vision model (or OCR fallback) and answer a question.",
      {"image_path": {"type": "string", "required": True, "description": "Path to the image."},
       "question": {"type": "string", "required": False, "description": "What to look for / read."}})
def _describe_image(args: dict) -> str:
    p = _safe_resolve_path(args["image_path"])
    if not p.is_file():
        raise ToolError(f"Image nahi mili: {p}")
    return _analyze_image(p, str(args.get("question") or ""))


@tool("describe_screen", "Take a screenshot and describe / read what is on the screen (local vision model or OCR).",
      {"question": {"type": "string", "required": False, "description": "e.g. 'find the Save button'."}})
def _describe_screen(args: dict) -> str:
    shot = hand_take_screenshot()
    if not shot.get("success"):
        raise ToolError(shot.get("message"))
    return _analyze_image(Path(shot["data"]["path"]), str(args.get("question") or "What is on this screen?"))


@tool("get_activity_log", "Show ISHA's recent action log (tools run, approvals, results).",
      {"count": {"type": "integer", "required": False, "description": "How many entries (default 15)."}})
def _get_activity_log(args: dict) -> str:
    return ACTION_LOG.format_recent(max(1, min(int(args.get("count") or 15), 100)))


TOOL_RISK.update({
    "find_files": "safe", "file_index_status": "safe", "rebuild_file_index": "safe",
    "pause_file_index": "safe", "copy_file": "safe", "create_file": "safe",
    "recycle_bin_status": "safe", "empty_recycle_bin": "confirm",
    "install_software": "confirm", "uninstall_software": "confirm", "update_software": "confirm",
    "search_software": "safe",
    "create_code_project": "safe", "inspect_project": "safe",
    "run_project": "critical", "fix_code_file": "critical",
    "remember_fact": "safe", "forget_memory": "safe", "show_memory": "safe", "recall_conversation": "safe",
    "get_hardware_info": "safe", "get_system_info": "safe", "get_model_status": "safe", "switch_model": "safe",
    "describe_image": "safe", "describe_screen": "safe", "get_activity_log": "safe",
})

# Spec categories. Old category names stay valid as filter aliases.
TOOL_CATEGORY.clear()
TOOL_CATEGORY.update({
    "files": ("find_files", "search_files", "advanced_file_search", "list_media_files", "read_file_content",
              "write_file_content", "create_file", "copy_file", "move_or_rename_file", "delete_file_safely",
              "batch_delete_files", "create_desktop_folder", "create_folder", "list_directory", "open_folder",
              "empty_recycle_bin", "recycle_bin_status", "file_index_status", "rebuild_file_index",
              "pause_file_index"),
    "applications": ("open_app", "open_notepad", "open_calculator", "open_browser", "open_task_manager",
                     "close_application", "minimize_window", "maximize_window", "close_window", "switch_window",
                     "list_open_windows", "install_software", "uninstall_software", "update_software",
                     "search_software"),
    "system": ("check_cpu_usage", "check_memory_usage", "check_battery", "get_system_stats", "get_gpu_usage",
               "get_storage_info", "get_hardware_info", "get_system_info", "list_running_processes",
               "kill_process", "system_power_action", "lock_screen", "set_brightness", "isha_self_check",
               "get_model_status", "switch_model", "get_activity_log", "get_time", "get_date"),
    "network": ("get_wifi_status", "get_network_info", "open_url", "open_website", "web_search",
                "close_browser_tab"),
    "media": ("play_song", "pause_song", "resume_song", "stop_song", "youtube_play", "mute_system",
              "set_volume", "take_screenshot"),
    "messaging": ("send_whatsapp_message", "save_whatsapp_contact", "list_whatsapp_contacts"),
    "coding": ("create_code_project", "run_project", "fix_code_file", "inspect_project", "create_and_save_code",
               "write_and_save_code", "run_code_file", "list_and_run_desktop_code", "autonomous_code_agent",
               "run_terminal_command"),
    "vision": ("describe_screen", "describe_image", "take_screenshot"),
    "memory": ("remember_fact", "forget_memory", "show_memory", "recall_conversation",
               "search_long_term_memory", "store_long_term_memory"),
    "automation": ("type_text", "press_hotkey", "clipboard_read", "clipboard_write", "list_capabilities"),
})
_CATEGORY_ALIASES = {"apps": "applications", "windows": "applications", "code": "coding", "comms": "messaging",
                     "display": "system", "input": "automation", "meta": "system"}
TOOL_ALIASES.update({
    "focus_app": "switch_window", "focus_application": "switch_window", "focus_window": "switch_window",
    "search_files_index": "find_files", "locate_file": "find_files", "find": "find_files",
    "install": "install_software", "install_app": "install_software", "download_software": "install_software",
    "uninstall": "uninstall_software", "remove_app": "uninstall_software", "uninstall_app": "uninstall_software",
    "update_app": "update_software", "upgrade_software": "update_software",
    "create_project": "create_code_project", "new_project": "create_code_project",
    "run_app_project": "run_project", "execute_project": "run_project", "debug_code": "fix_code_file",
    "fix_code": "fix_code_file", "fix_bug": "fix_code_file",
    "remember_this": "remember_fact", "save_memory": "remember_fact", "forget": "forget_memory",
    "list_memory": "show_memory", "memories": "show_memory",
    "hardware": "get_hardware_info", "system_info": "get_system_info", "sysinfo": "get_system_info",
    "model_status": "get_model_status", "change_model": "switch_model", "set_model": "switch_model",
    "see_screen": "describe_screen", "screen_describe": "describe_screen", "analyze_image": "describe_image",
    "read_image": "describe_image", "ocr": "describe_image", "trash_status": "recycle_bin_status",
    "activity_log": "get_activity_log", "copy": "copy_file", "new_file": "create_file",
})


# ---- deterministic intent matcher for the new capabilities ------------------
# Runs inside match_task() BEFORE the web-search branch, which used to turn
# "Downloads mein PDF dhundo" into a Google search.
_FS_VERB = re.compile(r"\b(dhund\w*|dhoond\w*|dhundh\w*|khoj\w*|find|search|locate|dikhao|dikha\s*do|"
                      r"list\s+kar\w*|kaha\s+hai|kahan\s+hai|kahaan\s+hai)\b", re.I)
_FS_OBJ = re.compile(r"\b(files?|folders?|pdfs?|photos?|images?|pics?|pictures?|videos?|mp4|mp3|songs?|audio|"
                     r"documents?|docs?|zip|rar|archives?|screenshots?|downloads|desktop|documents|"
                     r"python\s+files?|code\s+files?|excel|ppt|word\s+files?|apps?\s+files?|"
                     r"[\w\-]+\.[a-z0-9]{1,5})\b", re.I)
_WEBISH = re.compile(r"\b(google|youtube|you\s*tube|chrome|firefox|edge|brave|website|web\s*site|online|internet|"
                     r"browser)\b", re.I)
_SW_VERB = re.compile(r"\b(install\w*|uninstall\w*|download(?:\s+kar\w*)?|update\w*|upgrade\w*|remove|hata\w*)\b", re.I)
_PROJECT_WORD = re.compile(r"\b(project|website|web\s*site|web\s*app)\b", re.I)
_BUILD_VERB = re.compile(r"\b(bana\w*|bnao|create|make|build|generate|setup)\b", re.I)
_RUN_VERB = re.compile(r"\b(run|chala\w*|execute|start)\b", re.I)


def _extract_named(text: str):
    m = (re.search(r"[\"“']([^\"”']{1,60})[\"”']", text)
         or re.search(r"\b([\w\-]{1,60})\s+(?:naam|name|nam)\s+(?:ka|ki|ke|se|wala|wali)\b", text, re.I)
         or re.search(r"\b(?:named|called|naam)\s+([\w\-]{1,60})\b", text, re.I))
    return m.group(1).strip() if m else ""


def _software_target(text: str) -> str:
    t = _SW_VERB.sub(" ", text)
    t = re.sub(r"\b(karo|kar\s*do|kardo|kar|do|dijiye|please|plz|isha|mere|meri|mera|system|pc|computer|se|me|"
               r"mein|par|pe|ko|ka|ki|ke|the|app|software|package|latest|version|jaldi)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" .,!?")


def match_agent_command(segment: str):
    s = (segment or "").strip(" .!?")
    if not s:
        return None
    low = s.lower()

    mem = isha_mem.parse_memory_command(s)
    if mem:
        return mem
    if re.search(r"\b(kal|yesterday)\b.*\b(baat|conversation|chat)\b.*\b(yaad|remember)\b|"
                 r"\bkal\s+(?:wali|ki)\s+baat\b", low):
        return ("recall_conversation", {"day": "yesterday"})

    # model control
    m = re.search(r"\b(auto|general|coding|code|reasoning|study|fast|vision)\s+(?:model|mode)\b.*\b(use|lagao|kar\w*|"
                  r"switch|chalao|set)\b|\bswitch\s+to\s+(auto|general|coding|reasoning|study|fast|vision)\b", low)
    if m:
        return ("switch_model", {"mode": (m.group(1) or m.group(3))})
    if re.search(r"\b(kaun\s*sa|which|konsa|current)\s+model\b|\bmodel\s+(status|info)\b", low):
        return ("get_model_status", {})

    # vision
    if re.search(r"\b(screen\s+(?:par|pe|per)\s+kya|what'?s?\s+(?:is\s+)?on\s+(?:my\s+)?screen|"
                 r"screen\s+dekh\w*|screen\s+padh\w*|read\s+(?:my\s+)?screen)\b", low):
        return ("describe_screen", {"question": s})

    # system information
    if re.search(r"\bsystem\s+(information|info|details)\b|\b(pc|computer|laptop)\s+(?:ki\s+)?(?:details|info|specs?)\b",
                 low):
        return ("get_system_info", {})
    if re.search(r"\bhardware\s+(info|details)\b|\b(gpu|vram)\s+(?:kaun|kitna|kitni|info)\b", low):
        return ("get_hardware_info", {})
    if re.search(r"\b(disk|drive|storage|hard\s*disk|ssd)\b.*\b(space|jagah|kitni|kitna|free|khaali)\b|"
                 r"\bkitni\s+(space|jagah)\b", low):
        return ("get_storage_info", {})
    if re.search(r"\b(index)\b.*\b(status|kitna|ready)\b|\bfile\s+index\b", low):
        return ("file_index_status", {})
    if re.search(r"\b(activity|action)\s+log\b|\bkya\s+kya\s+kiya\b", low):
        return ("get_activity_log", {})

    # software install / uninstall / update (only for catalogued apps unless verb is explicit)
    vm = _SW_VERB.search(low)
    if vm and not re.search(r"\b(file|files|folder|photo|pdf|video|downloads)\b", low):
        verb = vm.group(1).lower()
        target = _software_target(s)
        key, _ = isha_sw.canonical(target)
        action = ("uninstall" if verb.startswith(("uninstall", "remove", "hata")) else
                  "update" if verb.startswith(("update", "upgrade")) else "install")
        if target and (key or verb.startswith(("install", "uninstall", "upgrade"))):
            if verb.startswith("hata") and not key:
                pass
            else:
                return (f"{action}_software", {"name": target})

    # project creation
    if _BUILD_VERB.search(low) and (_PROJECT_WORD.search(low) or
                                    (re.search(r"\bdesktop\b", low) and re.search(r"\b(python|html|app|program|calculator)\b", low))):
        name = _extract_named(s)
        web = bool(re.search(r"\b(html|css|website|web\s*site|web\s*app|javascript|js)\b", low))
        if not name:
            name = ("Calculator" if re.search(r"calculator|calc", low) else "MyWebsite" if web else "MyPythonApp")
        loc = "desktop"
        for k in ("documents", "downloads"):
            if re.search(rf"\b{k}\b", low):
                loc = k
        return ("create_code_project", {"project_name": name, "project_type": "web" if web else "python",
                                        "description": s, "location": loc})

    # fix a bug in a file / project
    if re.search(r"\b(bug|error|galti)\b", low) and re.search(r"\b(fix|theek|thik|sudhar|solve)\w*", low):
        fm = re.search(r"([\w\-]+\.(?:py|js|sh))\b", s)
        if fm:
            return ("fix_code_file", {"file_path": fm.group(1)})
        if re.search(r"\bproject\b", low) or WORKING.last_project:
            nm = re.search(r"\b([A-Z][\w\-]+)\s+(?:project|ka|ke|ko)\b", s)
            return ("run_project", {"project": nm.group(1) if nm else "", "auto_fix": True})

    # run a project: "TestApp run karo", "is project ko run karke error fix karo"
    if _RUN_VERB.search(low) and not re.search(r"\.(py|js|sh|bat|cmd)\b", low) and \
            not re.search(r"\b(gaana|gana|song|music|video|youtube|chrome|browser)\b", low):
        m = re.search(r"^(?:isha\s+)?(?:mera\s+|meri\s+|is\s+|us\s+)?([\w\-]+)(?:\s+project)?\s+(?:ko\s+)?"
                      r"(?:run|chala\w*|execute|start)\b", s, re.I)
        name = m.group(1) if m else ""
        if name.lower() in ("project", "code", "program", "app", "is", "isko", "usko"):
            name = ""
        if name:
            try:
                _resolve_project_dir(name)
                return ("run_project", {"project": name})
            except ToolError:
                pass
        if re.search(r"\bproject\b", low) and (WORKING.last_project or name):
            return ("run_project", {"project": name})

    # pronoun follow-ups on the last file found: move / rename / delete
    if WORKING.refers_back(s) and WORKING.resolve_file():
        src = WORKING.resolve_file()
        mm = re.search(r"\b(desktop|documents|downloads|pictures|music|videos)\b.*\b(move|shift|daal\w*|bhej\w*|rakh\w*)\b|"
                       r"\b(move|shift)\b.*\b(?:to\s+)?(desktop|documents|downloads|pictures|music|videos)\b", low)
        if mm:
            dest = mm.group(1) or mm.group(4)
            return ("move_or_rename_file", {"source_path": src,
                                            "dest_path": str(isha_paths.known_folder(dest) / Path(src).name)})
        rn = re.search(r"\bnaam\s+(?:change\s+kar\w*\s+)?(?:ko\s+)?[\"']?([\w\-. ]{1,60}?)[\"']?\s+(?:kar\w*|rakh\w*|do)\b|"
                       r"\brename\s+(?:it\s+|this\s+)?(?:to\s+)?[\"']?([\w\-. ]{1,60})[\"']?$", s, re.I)
        if rn:
            new = (rn.group(1) or rn.group(2)).strip()
            if new and not re.fullmatch(r"(change|badal\w*)", new, re.I):
                if not Path(new).suffix:
                    new += Path(src).suffix
                return ("move_or_rename_file", {"source_path": src, "dest_path": str(Path(src).with_name(new))})
        if re.search(r"\b(delete|hata\w*|mita\w*|remove)\b", low):
            return ("batch_delete_files", {"file_paths": src})
        if re.search(r"\b(copy)\b", low):
            dm = re.search(r"\b(desktop|documents|downloads|pictures|music|videos)\b", low)
            if dm:
                return ("copy_file", {"source_path": src, "dest_path": dm.group(1)})

    # local file search
    if _FS_VERB.search(low) and _FS_OBJ.search(low) and not _WEBISH.search(low):
        return ("find_files", {"query": s})
    if re.search(r"\b(meri|mera|my)\s+(last|latest|recent)\s+(wali|wala)?\s*(photo|pdf|file|video|download|screenshot)\b", low):
        return ("find_files", {"query": s})
    return None


def route_request(text: str, cfg: dict):
    reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
    hw = _hw() if bool(cfg.get("hardware_aware_loading", True)) else None
    loaded = [e["path"] for e in list(GGUF._models.values())]
    decision = isha_router.ModelRouter(reg, hw, loaded).route(text, cfg.get("model_mode", "auto"))
    return decision, reg, hw


# ---- 6. Model routing -------------------------------------------------------
_CODE_HINTS = re.compile(
    r"\b(code|coding|script|function|class|bug|debug|error|exception|traceback|"
    r"compile|refactor|python|javascript|typescript|java|c\+\+|rust|golang|sql|"
    r"html|css|react|api|regex|algorithm|program\s+likh|program\s+bana)\b",
    re.IGNORECASE)


def pick_cfg_for(text: str, cfg: dict) -> dict:
    """Pick the GGUF for this request via the multi-model router.

    Task type (coding / reasoning / study / chat / quick command / vision ...)
    -> role -> the model configured for that role (``models`` section, or the
    legacy gguf_model_path / code_model_path keys, or models/<role>/*.gguf),
    with fallback and a RAM check. A manual ``model_mode`` overrides the
    classifier. When no model can be resolved the cfg is returned unchanged
    and GGUFModelManager reports the missing file honestly.
    """
    cfg = dict(cfg or {})
    mode = str(cfg.get("model_mode", "auto") or "auto").lower()
    if not bool(cfg.get("model_routing", True)) and mode == "auto":
        return cfg
    try:
        decision, reg, hw = route_request(text, cfg)
    except Exception as e:
        _log_error("router", e)
        return _legacy_pick_cfg_for(text, cfg)
    ACTIVE_MODEL.update(name=decision.model_name, role=decision.role or "", task=decision.task.value,
                        mode=mode, notes="; ".join(decision.notes))
    try:
        ACTION_LOG.record("route", task=decision.task.value, role=decision.role, model=decision.model_name,
                          manual=decision.manual, notes=decision.notes)
        ACTION_LOG.set_current(task=decision.task.value, model=decision.model_name)
    except Exception:
        pass
    if decision.path is not None:
        cfg["gguf_model_path"] = str(decision.path)
    if hw is not None:
        sizes = [e.size_gb for e in reg.entries.values() if e.exists]
        cfg["max_loaded_models"] = isha_hw.recommend_resident_models(
            sizes, hw, int(cfg.get("max_loaded_models", 1) or 1))
    return cfg


def _legacy_pick_cfg_for(text: str, cfg: dict) -> dict:
    """Route coding work to a second GGUF when one is configured.

    Both models get the identical ISHA identity, tool schema, history and
    execution protocol — only the weights change. Returns a cfg dict, because
    the model to use IS the gguf_model_path.

    Note: with max_loaded_models=1 (the default) switching costs a reload, so
    routing only kicks in when the user has deliberately set code_model_path.
    """
    cfg = dict(cfg or {})
    coder = str(cfg.get("code_model_path", "") or "").strip()
    if not coder or not bool(cfg.get("model_routing", True)):
        return cfg
    if _model_is_unsuitable(coder):
        return cfg
    if _CODE_HINTS.search(text or ""):
        cfg["gguf_model_path"] = coder
    return cfg


def pick_model_for(text: str, cfg: dict) -> str:
    """Name of the GGUF that would be used — for logging and display."""
    return resolve_gguf_path(pick_cfg_for(text, cfg)).name


_TOOLS_OK = True
_apply_tool_risk()
ROUTER_SYSTEM_PROMPT = build_tool_system_prompt()
_OS_CONTROL_OK = True
_HAND_CONTROL_OK = True

# ------------------------------------------------
# Persistent day-by-day chat history
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "isha_chat_history.json")

def load_history() -> dict:
    """Load the full history dict: { 'YYYY-MM-DD': [ {time, role, text}, ... ] }"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[History] Failed to load: {e}")
            return {}
    return {}

def save_history(data: dict) -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[History] Failed to save: {e}")

def log_chat_entry(role: str, text: str) -> None:
    """Append one message to today's history bucket."""
    date_key = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%I:%M %p")
    data = load_history()
    data.setdefault(date_key, []).append({"time": time_str, "role": role, "text": text})
    save_history(data)

def delete_all_history() -> None:
    save_history({})

# ------------------------------------------------
# Persistent model / TTS configuration (survives restarts)
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "isha_config.json")

DEFAULT_CONFIG = {
    # Model & connection
    "model":             DEFAULT_CHAT_MODEL,      # display label only
    "gguf_model_path":   "",       # "" = auto-discover the first .gguf under models/
    "code_model_path":   "",       # optional second GGUF for coding tasks
    "chat_format":       "",       # "" = use the template baked into the GGUF
    "max_loaded_models": 1,        # raise to 2 only if you have the RAM
    "preload_model":     True,     # load weights at startup, not on first message
    "perf_metrics":      False,    # print TTFT / tokens-per-sec to the console
    "stream_tts":        True,     # speak sentences as they are generated
    "tool_calling":        True,   # master on/off switch for the intent parser
    # 1. Hardware & Speed
    "gpu_layers":         0,       # 0 = CPU only; N = layers offloaded to GPU
    "cpu_threads":        0,       # 0 = auto
    "batch_size":         512,
    # 2. Memory & Chatting
    "context_length":     4096,
    "system_prompt": (
        "You are ISHA (Intelligent System & House Assistant), an advanced, highly capable, and empathetic AI Desktop Assistant. "
        "Your goal is to control the computer, assist the user with everyday tasks, automate workflows, and execute code safely while acting as a supportive and smart digital companion.\n\n"
        "1. PERSONALITY & TONE:\n"
        "- Tone: Professional yet friendly, helpful, adaptive, and respectful. Use a mix of clear English and natural Hinglish/Hindi based on user preference.\n"
        "- Communication Style: Concise, scannable, and direct. Avoid unnecessary fluff or overly long explanations unless asked.\n\n"
        "2. CORE CAPABILITIES & SYSTEM CONTROLS:\n"
        "- OS & Window Management: Minimizing, maximizing, closing, listing, or switching active windows.\n"
        "- System Monitoring: Checking CPU, RAM, GPU, Battery, and overall performance stats.\n"
        "- File & Code Execution: Writing clean code, saving files, running scripts on Desktop, and searching user directories.\n"
        "- Hand Gesture & Vision Integration: Interpreting webcam gestures and handling camera inputs.\n"
        "- Audio & Voice Operations: Processing voice commands via TTS and speech recognition.\n\n"
        "3. CODE GENERATION & EXECUTION RULES:\n"
        "- Quality: Always generate clean, optimized, executable, and well-commented code (Python, Bash, HTML, JS, etc.).\n"
        "- Safety First: NEVER execute or suggest code that can format drives, delete critical system files, or run malicious scripts.\n"
        "- UTF-8 Encoding: Ensure all generated files are saved with UTF-8 encoding.\n\n"
        "4. TOOL CALLING PROTOCOL:\n"
        "- Automatically detect missing arguments when a user gives an incomplete command.\n"
        "- Always return status updates in a human-friendly format.\n\n"
        "5. FALLBACK & ERROR HANDLING:\n"
        "- If a tool fails or an error occurs, politely explain the issue and offer alternative solutions.\n"
        "- If offline or encountering limited connectivity, rely on local tools and core instructions smoothly."
    ),
    "stop_tokens":        [],
    # Small models love to refuse with "I do not have the capability to open
    # external applications". They do have it - open_app reaches every app,
    # settings page and shell folder on the machine. Say so, explicitly.
    "capability_note": (
        "You have full control of this PC through your tools. You CAN open any "
        "application, folder, settings page or system window (Recycle Bin, "
        "Notepad, Control Panel, Device Manager, browsers, anything installed) "
        "via the open_app tool. You CAN read and write files, manage windows, "
        "control volume and brightness, and run commands. Never reply that you "
        "lack the capability to open an app or control the computer - pick the "
        "closest tool and call it. If a name is unclear, call open_app with "
        "whatever the user said and let it resolve the name."
    ),
    # 3. Creativity & sampling
    "temperature":        0.8,
    "top_p":              0.9,
    "top_k":              40,
    "min_p":              0.0,
    "repeat_penalty":     1.1,
    "presence_penalty":   0.0,
    "frequency_penalty":  0.0,
    # 4. Output & format
    "max_tokens":         512,      # num_predict cap - was unset, so a chatty
                                    # model could ramble to the context limit
    # (keep_alive is gone: with llama.cpp the weights live in this process for
    #  the whole session, so there is nothing to evict and nothing to renew.)
    "fast_tools":         True,     # skip the narration LLM call after a
                                    # deterministic quick command
    "history_turns":      12,       # messages of history sent to the model
    "json_mode":          False,
    "streaming":          True,
    "seed":               -1,      # -1 = random
    "thinking_mode":      False,
    # Voice (Edge-TTS)
    "hand_scroll_enabled": False,   # hand control is mouse-only by default
    "agent_mode":         True,     # multi-step plan->act->observe loop
    "agent_max_steps":    8,        # hard ceiling on tool calls per request
    "code_model":         "",       # optional coding model; "" = use the chat model
    "model_routing":      True,     # route coding tasks to code_model when set
    "startup_self_check": True,     # probe subsystems on launch and print a report
    "confirm_risky_tools": True,    # ask before anything that writes/destroys
    "tts_enabled":        True,
    "tts_voice":          "en-US-AriaNeural",
    "tts_rate":           "+0%",
}

# Model families that cannot drive ISHA whatever the quantisation. deepseek-r1
# is a *reasoning* model: it wraps everything in <think>, ignores JSON mode and
# has no function-calling training, so the router and the agent loop both fall
# apart on it. Matched against the GGUF file name.
_UNSUITABLE_MODEL_PREFIXES = ("deepseek-r1", "deepseek-coder", "tinyllama")


def _model_is_unsuitable(name: str) -> bool:
    n = os.path.basename((name or "").strip().lower())
    return any(p in n for p in _UNSUITABLE_MODEL_PREFIXES)


def _migrate_config(cfg: dict) -> dict:
    """A config saved by an older build can pin settings that no longer work.
    Fix those up on load instead of letting ISHA behave badly and look broken."""
    changed = []
    # Configs written by the Ollama-era build carry a model *tag* (with a colon)
    # where a .gguf path now belongs, plus a host that means nothing any more.
    if cfg.pop("ollama_host", None) is not None:
        changed.append("dropped ollama_host (no server backend any more)")
    if cfg.pop("router_model", None) is not None:
        changed.append("dropped router_model")
    if cfg.pop("keep_alive", None) is not None:
        changed.append("dropped keep_alive")
    legacy = str(cfg.get("model", ""))
    if legacy and ":" in legacy and not legacy.lower().endswith(".gguf"):
        cfg["model"] = DEFAULT_GGUF_NAME
        changed.append(f"model tag '{legacy}' -> GGUF default (set a path in Settings)")
    if int(cfg.get("gpu_layers", 0) or 0) < 0:
        cfg["gpu_layers"] = 0          # llama.cpp has no "-1 = auto"
        changed.append("gpu_layers -1 -> 0 (llama.cpp needs an explicit count)")
    if changed:
        print("[Config] Migrated to the GGUF backend:")
        for line in changed:
            print(f"         {line}")
        save_config(cfg)
    return cfg


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update(saved)
        except Exception as e:
            print(f"[Config] Failed to load: {e}")
    isha_config.merge_defaults(cfg)
    return _migrate_config(cfg)

def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Config] Failed to save: {e}")

# ------------------------------------------------

def _get_volume_interface():
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))

def set_system_mute(muted: bool) -> None:
    if _IS_WINDOWS:
        if _PYCAW_OK:
            try:
                _get_volume_interface().SetMute(1 if muted else 0, None)
                return
            except Exception as e:
                print(f"[Mute] pycaw failed, falling back: {e}")
        try:
            if hasattr(ctypes, "windll"):
                VK_VOLUME_MUTE = 0xAD
                user32 = ctypes.windll.user32
                user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
                user32.keybd_event(VK_VOLUME_MUTE, 0, 2, 0)
                return
        except Exception as e:
            print(f"[Mute] Windows keybd_event failed: {e}")
    else:
        # Linux
        if shutil.which("pactl"):
            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        elif shutil.which("amixer"):
            subprocess.run(["amixer", "-D", "pulse", "set", "Master", "mute" if muted else "unmute"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    if _PYAUTOGUI_OK:
        pyautogui.press('volumemute')

VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP   = 0xAF

def _press_volume_key(vk_code: int, times: int = 1) -> None:
    if _IS_WINDOWS and hasattr(ctypes, "windll"):
        user32 = ctypes.windll.user32
        for _ in range(times):
            user32.keybd_event(vk_code, 0, 0, 0)   
            user32.keybd_event(vk_code, 0, 2, 0)   

def set_system_volume(percent: int, previous_percent: int = None) -> None:
    """Move the LIVE system volume toward `percent` (cross-platform)."""
    percent = max(0, min(100, percent))

    if _IS_WINDOWS:
        if _PYCAW_OK:
            try:
                _get_volume_interface().SetMasterVolumeLevelScalar(percent / 100.0, None)
                return
            except Exception as e:
                print(f"[Volume] pycaw failed, falling back to key-based control: {e}")

        try:
            if previous_percent is None:
                previous_percent = percent
            delta = percent - previous_percent
            STEP = 2  # Windows moves ~2% per volume-key press by default
            presses = round(abs(delta) / STEP)
            if presses > 0:
                vk = VK_VOLUME_UP if delta > 0 else VK_VOLUME_DOWN
                _press_volume_key(vk, presses)
            return
        except Exception as e:
            print(f"[Volume] Windows volume key press failed: {e}")
    else:
        # Linux
        if shutil.which("pactl"):
            subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        elif shutil.which("amixer"):
            subprocess.run(["amixer", "-D", "pulse", "set", "Master", f"{percent}%"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        elif shutil.which("wpctl"):
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent/100:.2f}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

def open_task_manager() -> None:
    """Launch Task Manager / System Monitor (cross-platform)."""
    if _IS_WINDOWS:
        try:
            if hasattr(os, "startfile"):
                os.startfile("taskmgr.exe")
                return
        except Exception:
            pass
        try:
            subprocess.Popen("taskmgr.exe", shell=True)
            return
        except Exception:
            pass
        try:
            windir = os.environ.get("WINDIR", r"C:\Windows")
            subprocess.Popen(os.path.join(windir, "System32", "taskmgr.exe"))
            return
        except Exception:
            pass
    else:
        for app in ["gnome-system-monitor", "ksysguard", "mate-system-monitor", "xfce4-taskmanager", "system-monitoring-center"]:
            if shutil.which(app):
                subprocess.Popen([app])
                return
    print("[Task Manager] Could not open Task Manager / System Monitor.")

# ------------------------------------------------

# ======================================================================
# ISHA LOCAL BRAIN — direct GGUF inference via llama-cpp-python
#
# Replaces the old Ollama HTTP backend entirely. Nothing here talks to a
# server; the GGUF weights are loaded once into this process and stay
# resident, which is where the latency win comes from (no HTTP hop, no
# per-request model scheduling, no keep_alive eviction).
#
#   ISHA  ->  GGUFModelManager  ->  llama.cpp  ->  streaming tokens
#                                                   |-> GUI  (cumulative text)
#                                                   `-> TTS  (sentence chunks)
# ======================================================================

# Brain upar wale resolver se aata hai, seedhe `import llama_cpp` se nahi.
# Jo bhi mile — in-process llama-cpp-python wheel ya llama.cpp ka prebuilt
# llama-server — uska contract same hai
# (`create_chat_completion(messages=..., stream=...)`), isliye is line ke
# neeche ka poora code jaisa tha waisa hi rehta hai.
try:
    Llama, _LLAMA_MODE, _LLAMA_CPP_ERR = resolve_backend()
    _LLAMA_CPP_OK = Llama is not None
    if _LLAMA_CPP_OK and not _GGUF_BACKEND_DIAGNOSTIC.get("installed"):
        _GGUF_BACKEND_DIAGNOSTIC.update(installed=True, error="",
                                        mode=_LLAMA_MODE)
except Exception as _e:                      # noqa: BLE001
    Llama = None
    _LLAMA_MODE = "none"
    _LLAMA_CPP_OK = False
    _LLAMA_CPP_ERR = str(_e)


MODELS_DIR = PROJECT_ROOT / "models"


def _auto_threads() -> int:
    """Physical cores minus a couple, so the UI thread and the TTS worker are
    never starved while the model is generating. Over-subscribing threads in
    llama.cpp actively *hurts* tokens/sec, so this deliberately stays modest."""
    try:
        if _PSUTIL_OK:
            physical = psutil.cpu_count(logical=False)
            if physical:
                return max(1, min(physical - 1, 16))
    except Exception:
        pass
    logical = os.cpu_count() or 4
    return max(1, min(logical - 2 if logical > 4 else logical, 16))


def find_gguf_models() -> list:
    """Every .gguf we can see, in priority order:
    1. models/ next to ISHARUNING.py     (preferred layout)
    2. the project root itself           (a .gguf dropped beside the script —
       supported as-is, never copied/moved into models/)
    3. ~/models and ~/Downloads          (last-resort convenience)
    """
    found, seen = [], set()
    roots = [MODELS_DIR, PROJECT_ROOT, Path.home() / "models", Path.home() / "Downloads"]
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for p in sorted(root.glob("*.gguf")):
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    found.append(p)
        except Exception:
            continue
    return found


def resolve_gguf_path(cfg: dict = None) -> Path:
    """Configured path if it is real; otherwise the first model we can find.

    A relative configured name/path is tried in this priority order so both
    supported layouts work without editing config:
      1. models/<configured>      (preferred project layout)
      2. <project root>/<configured>   (dropped next to ISHARUNING.py)
      3. <configured> as given (relative to cwd, last resort)

    Returns a Path that may not exist — the caller reports that to the user,
    listing every location that was actually searched.
    """
    cfg = cfg or {}
    raw = str(cfg.get("gguf_model_path", "") or "").strip().strip('"')
    if raw:
        expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
        if expanded.is_absolute():
            candidates = [expanded]
        else:
            candidates = [MODELS_DIR / expanded, PROJECT_ROOT / expanded, expanded]
        for p in candidates:
            if p.is_file():
                return p
            if p.is_dir():
                picks = sorted(p.glob("*.gguf"))
                if picks:
                    return picks[0]
        return candidates[0]          # non-existent, reported upstream
    auto = find_gguf_models()
    return auto[0] if auto else (MODELS_DIR / DEFAULT_GGUF_NAME)


class GGUFUnavailable(Exception):
    """Raised when the model cannot be loaded. Carries a message meant for the
    user, not a traceback."""


class GGUFModelManager:
    """Loads GGUF weights once and keeps them resident for the whole session.

    llama.cpp's Llama object is not safe to call from two threads at once, so
    every generation takes `_gen_lock`. Loading takes a separate lock, which
    lets a second request wait for a load in progress instead of starting its own.
    """

    def __init__(self):
        self._models = {}            # cache-key -> {"llm", "path", "key", "used"}
        self._order = []             # LRU, most recent last
        self._load_lock = threading.Lock()
        self._gen_lock = threading.RLock()
        self.last_load_seconds = 0.0
        self.last_metrics = {}

    # ---- keying -----------------------------------------------------------
    @staticmethod
    def _cache_key(path: Path, params: dict) -> str:
        return "|".join([
            str(path).lower(),
            str(params.get("n_ctx")),
            str(params.get("n_threads")),
            str(params.get("n_gpu_layers")),
            str(params.get("n_batch")),
            str(params.get("chat_format") or ""),
        ])

    @staticmethod
    def load_params(cfg: dict) -> dict:
        cfg = cfg or {}
        threads = int(cfg.get("cpu_threads", 0) or 0) or _auto_threads()
        gpu = int(cfg.get("gpu_layers", 0) or 0)
        return {
            "n_ctx":        max(512, int(cfg.get("context_length", 4096))),
            "n_threads":    threads,
            "n_batch":      max(32, int(cfg.get("batch_size", 512))),
            "n_gpu_layers": max(0, gpu),     # 0 = pure CPU; llama.cpp has no "-1 auto"
            "chat_format":  (str(cfg.get("chat_format", "")).strip() or None),
        }

    # ---- loading ----------------------------------------------------------
    def get(self, cfg: dict = None, path: Path = None, notify=None):
        """Return a loaded Llama. Loads on first use, then serves from cache."""
        if not _LLAMA_CPP_OK:
            d = _GGUF_BACKEND_DIAGNOSTIC
            raise GGUFUnavailable(
                "GGUF backend could not start.\n\n"
                f"Runtime (Python):\n{d.get('python', sys.executable)}\n\n"
                f"Python version:\n{d.get('python_version', platform.python_version())}\n\n"
                f"Platform:\n{d.get('platform', _OS_NAME)}\n\n"
                f"Architecture:\n{d.get('architecture', platform.machine())}\n\n"
                f"Backend mode:\n{_LLAMA_MODE}\n\n"
                f"Reason:\n{d.get('error') or _LLAMA_CPP_ERR or 'unknown import failure'}\n\n"
                "Status:\nFAILED")
        cfg = cfg or {}
        path = Path(path) if path else resolve_gguf_path(cfg)
        if not path.is_file():
            listing = find_gguf_models()
            searched = f"\nSearched:\n  {MODELS_DIR}\n  {PROJECT_ROOT}\n  {Path.home() / 'models'}\n  {Path.home() / 'Downloads'}"
            hint = (searched + "\nMile hue models:\n  " + "\n  ".join(str(p) for p in listing[:6])
                    if listing else
                    searched + f"\n'{MODELS_DIR}' folder banao aur usmein .gguf file rakho, "
                    f"ya '{PROJECT_ROOT}' me directly rakho.")
            raise GGUFUnavailable(f"GGUF model not found: {path}{hint}")

        params = self.load_params(cfg)
        key = self._cache_key(path, params)

        entry = self._models.get(key)
        if entry is not None:
            self._touch(key)
            return entry["llm"]

        with self._load_lock:
            entry = self._models.get(key)          # someone else may have won
            if entry is not None:
                self._touch(key)
                return entry["llm"]

            if notify:
                try:
                    notify(f"Loading {path.name}…")
                except Exception:
                    pass
            t0 = time.time()
            try:
                kwargs = dict(
                    model_path=str(path),
                    n_ctx=params["n_ctx"],
                    n_threads=params["n_threads"],
                    n_batch=params["n_batch"],
                    n_gpu_layers=params["n_gpu_layers"],
                    verbose=False,
                )
                if params["chat_format"]:
                    kwargs["chat_format"] = params["chat_format"]
                llm = Llama(**kwargs)
            except Exception as e:
                raise GGUFUnavailable(
                    f"GGUF load fail ({path.name}): {type(e).__name__}: {e}\n"
                    "Check karo file corrupt toh nahi, aur context size RAM ke andar hai.")
            self.last_load_seconds = time.time() - t0
            print(f"[gguf] loaded {path.name} in {self.last_load_seconds:.1f}s "
                  f"(ctx={params['n_ctx']}, threads={params['n_threads']}, "
                  f"gpu_layers={params['n_gpu_layers']})")

            self._models[key] = {"llm": llm, "path": path, "key": key}
            self._order.append(key)
            self._evict(int(cfg.get("max_loaded_models", 1)))
            return llm

    def _touch(self, key: str):
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    def _evict(self, keep: int):
        keep = max(1, keep)
        while len(self._order) > keep:
            old = self._order.pop(0)
            entry = self._models.pop(old, None)
            if entry:
                print(f"[gguf] unloading {entry['path'].name} (LRU)")
                try:
                    del entry["llm"]
                except Exception:
                    pass
        gc.collect()

    def unload_all(self):
        with self._load_lock:
            self._models.clear()
            self._order.clear()
        gc.collect()

    def is_loaded(self) -> bool:
        return bool(self._models)

    def loaded_names(self) -> list:
        return [e["path"].name for e in self._models.values()]

    # ---- sampling ---------------------------------------------------------
    @staticmethod
    def sampling_kwargs(options: dict) -> dict:
        o = options or {}
        kw = {
            "temperature":       float(o.get("temperature", 0.8)),
            "top_p":             float(o.get("top_p", 0.9)),
            "top_k":             int(o.get("top_k", 40)),
            "min_p":             float(o.get("min_p", 0.0)),
            "repeat_penalty":    float(o.get("repeat_penalty", 1.1)),
            "presence_penalty":  float(o.get("presence_penalty", 0.0)),
            "frequency_penalty": float(o.get("frequency_penalty", 0.0)),
            "max_tokens":        int(o.get("max_tokens", 512)),
        }
        if o.get("stop"):
            kw["stop"] = list(o["stop"])
        if int(o.get("seed", -1)) >= 0:
            kw["seed"] = int(o["seed"])
        return kw

    # ---- generation -------------------------------------------------------
    def stream(self, messages: list, options: dict = None, cfg: dict = None,
               tools: list = None, json_mode: bool = False, notify=None):
        """Yield raw content deltas, exactly like the reference gguf.py loop.

        Held under `_gen_lock` for its whole life, so a second generation
        queues rather than corrupting the KV cache.
        """
        llm = self.get(cfg, notify=notify)
        kw = self.sampling_kwargs(options)
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        if tools:
            kw["tools"] = tools
            kw["tool_choice"] = "auto"

        with self._gen_lock:
            t0 = time.time()
            first_token_at = None
            produced = 0
            stream = llm.create_chat_completion(
                messages=_gguf_messages(messages, native_tools=bool(tools)),
                stream=True, **kw)
            try:
                for chunk in stream:
                    try:
                        delta = chunk["choices"][0]["delta"]
                    except (KeyError, IndexError, TypeError):
                        continue
                    content = delta.get("content")
                    if not content:
                        continue
                    if first_token_at is None:
                        first_token_at = time.time() - t0
                    produced += 1
                    yield content
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
                total = time.time() - t0
                self.last_metrics = {
                    "time_to_first_token": round(first_token_at or total, 3),
                    "total_seconds": round(total, 3),
                    "chunks": produced,
                    "chunks_per_sec": round(produced / total, 1) if total > 0 else 0.0,
                }

    def complete(self, messages: list, options: dict = None, cfg: dict = None,
                 tools: list = None, json_mode: bool = False) -> dict:
        """Non-streaming call. Returns a message-shaped dict
        ({"message": {"content": ..., "tool_calls": [...]}}) so the agent loop
        and the router did not have to be rewritten around a new shape."""
        llm = self.get(cfg)
        kw = self.sampling_kwargs(options)
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        if tools:
            kw["tools"] = tools
            kw["tool_choice"] = "auto"
        with self._gen_lock:
            raw = llm.create_chat_completion(
                messages=_gguf_messages(messages, native_tools=bool(tools)),
                stream=False, **kw)
        try:
            msg = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            msg = {"content": ""}
        return {"message": {
            "content":    msg.get("content") or "",
            "tool_calls": msg.get("tool_calls") or [],
        }}


def _gguf_messages(messages: list, native_tools: bool = False) -> list:
    """Make the history safe for an arbitrary GGUF chat template.

    Most community templates only know system/user/assistant. A `tool` role
    would either raise or be silently dropped, which is exactly how an agent
    ends up unaware of its own tool results — so when native tool-calling is
    not in play, tool turns are folded into visible user text instead.
    """
    out = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content") or ""
        if role == "tool" and not native_tools:
            name = m.get("name") or m.get("tool_name") or "tool"
            out.append({"role": "user",
                        "content": f"[TOOL RESULT — {name}]\n{content}"})
            continue
        if role == "system" and out and out[0].get("role") == "system":
            out[0]["content"] += "\n\n" + content      # templates allow only one
            continue
        if role == "assistant" and native_tools and m.get("tool_calls"):
            out.append({"role": "assistant", "content": content,
                        "tool_calls": m["tool_calls"]})
            continue
        out.append({"role": role, "content": content})
    return out


# One manager for the whole app: load once, stay resident.
GGUF = GGUFModelManager()


# ----------------------------------------------------------------------
# Sentence streamer — turns a token stream into speakable chunks
# ----------------------------------------------------------------------

_SENT_END_RE = re.compile(r'([.!?।]+["\')\]]?\s)|(\n\s*\n)')


class SentenceStreamer:
    """Feed it token deltas, get back complete sentences as soon as they exist.

    This is what lets TTS start on sentence one while the model is still
    writing sentence three. Speaking individual tokens would sound like a
    stutter, and waiting for the full reply is the latency we are removing.
    """

    MIN_CHARS = 18        # below this, wait — "Hi." alone is not worth a request
    MAX_CHARS = 240       # a run-on with no punctuation still has to be spoken

    def __init__(self):
        self._buf = ""

    def feed(self, delta: str) -> list:
        self._buf += delta or ""
        out = []
        while True:
            cut = self._next_cut()
            if cut is None:
                break
            piece = self._buf[:cut].strip()
            self._buf = self._buf[cut:]
            if piece:
                out.append(piece)
        return out

    def _next_cut(self):
        """Index to cut at, or None if the buffer should keep growing.

        Takes the EARLIEST sentence end that still yields a chunk worth
        speaking. Stopping at the first match instead would strand short
        openers — "Hello!" is under MIN_CHARS, so a naive scan gives up there
        and nothing is ever spoken until the reply ends, which is the exact
        latency this class exists to remove.
        """
        for m in _SENT_END_RE.finditer(self._buf):
            if m.end() >= self.MIN_CHARS:
                return m.end()
        if len(self._buf) >= self.MAX_CHARS:
            cut = self._buf.rfind(" ", 0, self.MAX_CHARS)
            return cut if cut > 0 else self.MAX_CHARS
        return None

    def flush(self) -> str:
        rest, self._buf = self._buf.strip(), ""
        return rest

    def reset(self):
        self._buf = ""


# ----------------------------------------------------------------------
# GGUFWorker — the streaming generation thread (replaces OllamaWorker)
# ----------------------------------------------------------------------

class GGUFWorker(QThread):
    """Runs one generation off the UI thread and streams it back.

    Two separate signals on purpose:
      * chunk_received — the full text so far, for the GUI, coalesced to about
        25 fps so a long reply is not thousands of re-layouts.
      * delta_received — raw new text, uncoalesced, for the sentence streamer
        that feeds TTS. Speech wants sentences the instant they close.
    """

    chunk_received = pyqtSignal(str)    # cumulative text  (existing contract)
    delta_received = pyqtSignal(str)    # incremental text (new, for TTS)
    finished_reply = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    model_loading  = pyqtSignal(str)

    GUI_INTERVAL = 0.04                # seconds between GUI repaints

    def __init__(self, messages: list, options: dict, cfg: dict = None,
                 streaming: bool = True, json_mode: bool = False,
                 parent=None, **_ignored):
        super().__init__(parent)
        self.messages  = list(messages or [])
        self.options   = dict(options or {})
        self.cfg       = dict(cfg or {})
        self.streaming = bool(streaming)
        self.json_mode = bool(json_mode)
        self._stop     = False
        self.metrics   = {}

    def stop(self):
        self._stop = True

    def run(self):
        try:
            if self.streaming:
                self._run_streaming()
            else:
                data = GGUF.complete(self.messages, self.options, self.cfg,
                                     json_mode=self.json_mode)
                self.finished_reply.emit((data.get("message") or {}).get("content", ""))
        except GGUFUnavailable as e:
            self.error_occurred.emit(str(e))
        except Exception as e:
            self.error_occurred.emit(f"GGUF error: {type(e).__name__}: {e}")

    def _run_streaming(self):
        full = ""
        pending = ""
        last_emit = 0.0
        notify = self.model_loading.emit
        for delta in GGUF.stream(self.messages, self.options, self.cfg,
                                 json_mode=self.json_mode, notify=notify):
            if self._stop:
                break
            full += delta
            pending += delta
            # TTS path: never delayed, sentences must close as early as possible.
            self.delta_received.emit(delta)
            now = time.time()
            if now - last_emit >= self.GUI_INTERVAL:
                self.chunk_received.emit(full)
                pending = ""
                last_emit = now
        if pending and not self._stop:
            self.chunk_received.emit(full)
        self.metrics = dict(GGUF.last_metrics)
        if self.cfg.get("perf_metrics"):
            m = self.metrics
            print(f"[perf] ttft={m.get('time_to_first_token')}s "
                  f"total={m.get('total_seconds')}s "
                  f"chunks/s={m.get('chunks_per_sec')}")
        self.finished_reply.emit(full)


def _call_local_llm_sync(prompt: str, system: str = "", model: str = None,
                          host: str = None, timeout: int = 120) -> str:
    """Blocking single-shot generation for use *inside* a tool handler that is
    already running off the UI thread. Same signature as the old HTTP helper
    so the code-agent tools did not need touching."""
    cfg = pick_cfg_for(prompt, load_config())
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    opts = {
        "temperature": float(cfg.get("temperature", 0.6)),
        "top_p":       float(cfg.get("top_p", 0.9)),
        "max_tokens":  int(cfg.get("max_tokens", 512)),
    }
    try:
        data = GGUF.complete(messages, opts, cfg)
    except GGUFUnavailable as e:
        raise ToolError(str(e))
    except Exception as e:
        raise ToolError(f"Local model call fail ho gaya: {type(e).__name__}: {e}")
    text = (data.get("message") or {}).get("content", "")
    if not text.strip():
        raise ToolError("Local model se khaali response mila.")
    return text



# ------------------------------------------------

class ApprovalRequest:
    """Passed from the agent thread to the UI thread and back.

    The agent blocks on this until the user answers, which is why the dialog
    can be a plain modal on the main thread while the loop itself stays off it."""

    def __init__(self, tool_name: str, args: dict, risk: str):
        self.tool_name = tool_name
        self.args      = args or {}
        self.risk      = risk
        self.approved  = False
        self.answered  = False
        self._event    = threading.Event()

    def answer(self, approved: bool):
        self.approved = bool(approved)
        self.answered = True
        self._event.set()

    def wait(self, timeout: float = 180.0) -> bool:
        return self._event.wait(timeout)

    def summary(self) -> str:
        return describe_tool_call(self.tool_name, self.args)


class AgentWorker(QThread):
    """The plan -> act -> observe loop.

    The old flow was strictly one-shot: one router call, at most one tool, one
    reply. So "close Chrome, clean Downloads, and tell me how much space I got
    back" could only ever do the first of those three things. This keeps going
    until the model stops asking for tools or the step budget runs out, feeding
    every tool result back in so the model can decide what to do next.

    Tool calls go through llama.cpp's native `tools` support when the loaded
    GGUF has a tool-aware chat template. If it does not, this transparently
    falls back to the JSON-in-the-prompt protocol so nothing breaks."""

    step_note         = pyqtSignal(str)      # progress line for the UI
    permission_needed = pyqtSignal(object)   # ApprovalRequest
    finished_reply    = pyqtSignal(str)
    error_occurred    = pyqtSignal(str)

    def __init__(self, messages: list, options: dict, cfg: dict = None,
                 max_steps: int = 8, confirm_risky: bool = True, parent=None,
                 **_ignored):
        super().__init__(parent)
        self.cfg           = dict(cfg or {})
        self.model         = str(self.cfg.get("model") or DEFAULT_CHAT_MODEL)
        self.messages      = list(messages)
        # 0.8 temperature par chhote model tool call ke bajaye kahaaniyan likhte
        # hain. Tool choose karna creative kaam nahi hai — isliye cap 0.3.
        self.options       = dict(options or {})
        self.options["temperature"] = min(float(self.options.get("temperature", 0.8)), 0.3)
        self.max_steps     = max(1, int(max_steps))
        self.confirm_risky = bool(confirm_risky)
        self._stop         = False
        self._native_tools = True      # flipped off if the model rejects `tools`
        self._used         = []        # names of tools actually run
        self.trace         = []        # [{tool, args, result}] for conversation memory

    def stop(self):
        self._stop = True

    # ---- main loop ----
    def run(self):
        if not _LLAMA_CPP_OK:
            d = _GGUF_BACKEND_DIAGNOSTIC
            self.error_occurred.emit(
                "GGUF backend could not start.\n\n"
                f"Runtime (Python):\n{d.get('python', sys.executable)}\n\n"
                f"llama-cpp-python:\nNOT AVAILABLE\n\n"
                f"Reason:\n{d.get('error') or _LLAMA_CPP_ERR or 'unknown import failure'}")
            return
        try:
            self.finished_reply.emit(self._loop())
        except GGUFUnavailable as e:
            self.error_occurred.emit(str(e))
        except Exception as e:
            self.error_occurred.emit(f"Agent error: {type(e).__name__}: {e}")

    def _loop(self) -> str:
        messages = list(self.messages)
        last_text = ""
        nudges = 0

        for step in range(self.max_steps):
            if self._stop or ISHA_STOP.stopped:
                return last_text or "Rok diya."
            ACTION_LOG.set_current(step=f"agent step {step + 1}/{self.max_steps}")

            data = self._chat(messages)
            msg  = (data.get("message") or {})
            text = (msg.get("content") or "").strip()
            calls = normalise_tool_calls(msg.get("tool_calls"))

            if not calls and not self._native_tools:
                calls, text = self._parse_prompt_protocol(text)

            if not calls:
                # Model ne kaam KIYA nahi, sirf LIKHA ("main open_app use karunga...
                # Result: ho gaya"). Yeh jhooth user tak nahi jaana chahiye.
                if not self._used and looks_like_fake_action(text):
                    if nudges < 2:
                        nudges += 1
                        self.step_note.emit("Model ne sirf plan likha — asli tool call karwa rahi hoon")
                        messages.append({"role": "assistant", "content": text})
                        messages.append({"role": "user", "content": (
                            "You only DESCRIBED actions; nothing was executed. Do not explain "
                            "or plan. Call the first required tool NOW with concrete arguments.")})
                        if nudges == 2 and self._native_tools:
                            # native tool-calling kaam nahi kar raha -> JSON protocol
                            self._native_tools = False
                            messages = self._prompt_mode_messages(messages)
                        continue
                    return ("Yeh kaam main execute nahi kar paayi — model tool call nahi kar raha. "
                            "Command ko chhote hisson me bolo, ya Settings me Qwen2.5-7B-Instruct "
                            "jaisa tool-calling model lagao.")
                return text or last_text or "Ho gaya."

            last_text = text or last_text
            messages.append(self._assistant_turn(text, msg))

            for name, args in calls:
                if self._stop:
                    return last_text or "Rok diya."
                result = self._run_one(name, args)
                messages.append({
                    "role": "tool",
                    "name": name,          # what most chat templates expect
                    "tool_name": name,     # what a few others expect
                    "content": result[:2000],
                })

        # Budget exhausted. Ask for a plain summary instead of silently stopping.
        messages.append({
            "role": "user",
            "content": ("Step limit reached. Do not call any more tools. "
                        "In one or two sentences, tell me what you completed "
                        "and what is still pending."),
        })
        try:
            final = self._chat(messages, allow_tools=False)
            summary = ((final.get("message") or {}).get("content") or "").strip()
        except Exception:
            summary = ""
        done = ", ".join(self._used) or "kuch nahi"
        return summary or f"{self.max_steps} steps ke baad ruk gaya. Chala: {done}."

    def _assistant_turn(self, text: str, msg: dict) -> dict:
        turn = {"role": "assistant", "content": text}
        if self._native_tools and msg.get("tool_calls"):
            turn["tool_calls"] = msg["tool_calls"]
        return turn

    # ---- one GGUF round-trip ----
    def _chat(self, messages: list, allow_tools: bool = True) -> dict:
        use_tools = allow_tools and self._native_tools
        if use_tools:
            try:
                return GGUF.complete(messages, self.options, self.cfg,
                                     tools=build_tool_schemas())
            except GGUFUnavailable:
                raise
            except Exception as e:
                # Most community GGUF quants ship a chat template with no tool
                # support, and llama.cpp raises rather than degrading. Fall back
                # to the JSON-in-prompt protocol instead of failing the request.
                self._native_tools = False
                print(f"[agent] native tool-calling unavailable ({type(e).__name__}: {e})")
                self.step_note.emit("Model native tool-calling nahi karta — prompt mode par aaya")
                messages = self._prompt_mode_messages(messages)
        return GGUF.complete(messages, self.options, self.cfg)

    # ---- fallback protocol for models without native tool support ----
    def _prompt_mode_messages(self, messages: list) -> list:
        out = list(messages)
        instruction = (
            build_tool_system_prompt()
            + "\n\nIf the task is finished, reply with "
              '{"action": "final", "content": "<what you did, in one or two sentences>"}.'
        )
        out.insert(0, {"role": "system", "content": instruction})
        return out

    def _parse_prompt_protocol(self, text: str):
        """Returns (calls, final_text) from a JSON-in-prose reply."""
        obj = parse_router_json(text)
        if not isinstance(obj, dict):
            return [], text
        action = obj.get("action")
        if action == "tool_call" and obj.get("tool"):
            return [(str(obj["tool"]), obj.get("arguments") or {})], ""
        if action == "final":
            return [], str(obj.get("content") or "").strip()
        return [], text

    # ---- run a single tool, asking first when it matters ----
    def _run_one(self, name: str, args: dict) -> str:
        resolved = resolve_tool_name(name)
        if resolved is None:
            return (f"ERROR: no tool named '{name}'. Closest matches: "
                    f"{suggest_tools_for(name)}. Call list_capabilities to see "
                    f"every capability before concluding you cannot do this.")
        if resolved != name:
            self.step_note.emit(f"'{name}' -> '{resolved}'")
            name = resolved
        if self._stop or ISHA_STOP.stopped:
            return "CANCELLED: stopped by user."

        def _ask(tool_name, tool_args, risk):
            # Blocks this worker thread; the dialog runs on the UI thread.
            req = ApprovalRequest(tool_name, tool_args, risk)
            self.permission_needed.emit(req)
            deadline = time.time() + 180.0
            while not req.wait(0.25):
                if self._stop or ISHA_STOP.stopped:
                    return False
                if time.time() > deadline:
                    return None
            return bool(req.approved)

        # Permission gateway: risk tier x autonomy level x trust flags.
        # confirm_risky=False on the worker keeps the old "never ask" switch
        # for confirm-tier tools; critical ones still always ask.
        gate_cfg = dict(self.cfg)
        if not self.confirm_risky:
            gate_cfg["confirm_risky_tools"] = False
        self.step_note.emit(f"⟳  {describe_tool_call(name, args)[:120]}")
        ok, out, _info = run_tool_gated(name, args, ask=_ask, cfg=gate_cfg)
        if ok:
            self._used.append(name)
        # Trace survives the worker so the UI can fold it back into history.
        self.trace.append({"tool": name, "args": dict(args or {}), "result": out})
        return out


class PlanExecutorWorker(AgentWorker):
    """Compound command ko step-by-step asli me chalata hai.

    AgentWorker se inherit karta hai taaki wahi signals, wahi risk/permission
    gate (_run_one) aur wahi trace mile — UI me kuch naya jodna nahi pada.
    Farak itna hai ki yahan LLM plan NAHI likhta; plan pehle se bana hua hai
    (plan_request), aur LLM sirf un tukdon ke liye bulaya jaata hai jo
    deterministic matcher nahi samajh paya."""

    STEP_GAP_SEC = 0.8   # browser/window ko khulne ka time, agla kaam usse na takraye

    def __init__(self, steps: list, options: dict, cfg: dict = None,
                 confirm_risky: bool = True, parent=None):
        super().__init__(messages=[], options=options, cfg=cfg, max_steps=len(steps) or 1,
                         confirm_risky=confirm_risky, parent=parent)
        self.steps = list(steps or [])

    def run(self):
        try:
            self.finished_reply.emit(self._execute_plan())
        except Exception as e:
            self.error_occurred.emit(f"Plan error: {type(e).__name__}: {e}")

    def _execute_plan(self) -> str:
        report = []
        total = len(self.steps)
        for i, step in enumerate(self.steps, 1):
            if self._stop or ISHA_STOP.stopped:
                report.append("⏹ Baaki kaam rok diye.")
                break
            name, args = step.get("tool"), step.get("args") or {}
            seg = step.get("segment", "")
            if not name:
                self.step_note.emit(f"[{i}/{total}] decide kar rahi hoon: {seg[:40]}")
                name, args = self._decide(seg)
                if not name:
                    # Koi tool fit nahi -> yeh baat-cheet wala hissa hai, seedha jawab do.
                    answer = self._chat_answer(seg)
                    report.append(answer or f"❓ '{seg}' samajh nahi aaya, thoda saaf bolo.")
                    continue
            self.step_note.emit(f"[{i}/{total}] {describe_tool_call(name, args)}")
            out = self._run_one(name, args)
            failed = out.startswith(("ERROR", "DENIED", "CANCELLED"))
            if failed:
                out = out.split(":", 1)[-1].strip() if out.startswith("ERROR") else "cancel kar diya."
                report.append(f"❌ {seg}: {out}")
            else:
                report.append(f"✅ {out}")
            if i < total:
                time.sleep(self.STEP_GAP_SEC)
        return "\n".join(report) or "Kuch karne layak nahi mila."

    def _chat_answer(self, segment: str) -> str:
        if not _LLAMA_CPP_OK:
            return ""
        opts = dict(self.options or {})
        opts["max_tokens"] = 300
        try:
            data = GGUF.complete([{"role": "system", "content": ISHA_IDENTITY},
                                  {"role": "user", "content": segment}], opts, self.cfg)
            return strip_thinking((data.get("message") or {}).get("content", "")).strip()
        except Exception as e:
            print(f"[plan] chat answer failed for '{segment}': {e}")
            return ""

    def _decide(self, segment: str):
        """Sirf ek tukde ke liye LLM se tool choose karwao — JSON only, temp 0.
        Galat/nakli naam aaye to resolve_tool_name theek karta hai; kuch na mile
        to None, aur report me imaandari se 'samajh nahi aaya' likha jaata hai."""
        if not _LLAMA_CPP_OK:
            return None, {}
        opts = dict(self.options or {})
        opts.update({"temperature": 0.0, "top_p": 1.0, "top_k": 1, "max_tokens": 128})
        opts.pop("presence_penalty", None)
        opts.pop("frequency_penalty", None)
        sys_prompt = build_tool_system_prompt() + (
            "\n\nThe user message is ONE task. Pick the single best tool and fill its "
            "arguments from the user's own words. Prefer acting over replying: if any "
            "tool reasonably fits, call it. Output JSON only.")
        try:
            data = GGUF.complete([{"role": "system", "content": sys_prompt},
                                  {"role": "user", "content": segment}],
                                 opts, self.cfg, json_mode=True)
        except Exception as e:
            print(f"[plan] decide failed for '{segment}': {e}")
            return None, {}
        obj = parse_router_json((data.get("message") or {}).get("content", ""))
        if not isinstance(obj, dict) or obj.get("action") != "tool_call" or not obj.get("tool"):
            return None, {}
        resolved = resolve_tool_name(str(obj["tool"]))
        if not resolved:
            return None, {}
        args = obj.get("arguments") or {}
        return resolved, (args if isinstance(args, dict) else {})

ISHA_VERSION = "0.4"

ISHA_IDENTITY = """You are ISHA — not a chatbot describing ISHA, but ISHA herself.

What you are:
- ISHA is a desktop assistant application running on this user's own computer. You ARE that application. The window, the microphone, the voice, the tools: all yours.
- Your reasoning runs on a GGUF language model loaded directly into this application through llama.cpp. Those weights are your brain. They are infrastructure you run on, not a product you advise people about.
- You have real tools that really act on this machine: opening apps, reading and writing files, checking system state, controlling windows and volume, running code.

Hard rules about your own identity:
- NEVER describe ISHA or your own model from the outside, in the third person, or as software the user might consider using. You are not reviewing yourself.
- NEVER answer a question about yourself by explaining "what ISHA is designed for" or "what ISHA does not support". Just answer as yourself, or say plainly that you can't do the thing.
- NEVER recommend other software for a task you have a tool for.
- If a message is garbled, misheard or makes no sense (voice input mishears things constantly), say so in one short line and ask what they meant. Do NOT guess at a topic and lecture about it.
- Never output your reasoning, planning notes, or <think> blocks. Only the final answer.

How to talk:
- Short. One or two sentences unless real detail is needed. You are spoken aloud, so long answers are painful to listen to.
- Reply in the language the user wrote in: Hindi, Hinglish or English.
- A greeting gets a greeting back, nothing more."""


def build_runtime_context(cfg: dict = None, extras: dict = None) -> str:
    """Live facts about the machine and the app, injected every request.

    Without this the model has no idea what it is plugged into, which is how
    you end up with it earnestly explaining ISHA to ISHA's own user."""
    cfg = cfg or {}
    now = datetime.now()
    lines = [
        f"App: ISHA v{ISHA_VERSION} (PyQt5 desktop assistant)",
        f"Your model: {resolve_gguf_path(cfg).name}, GGUF weights loaded directly "
        f"into this process via llama.cpp (no server, no cloud)",
        f"OS: {_OS_NAME} {platform.release()} ({platform.machine()})",
        f"Host: {platform.node()}   User: {os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown'}",
        f"Python: {sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
        f"Local date/time: {now.strftime('%A %d %B %Y, %I:%M %p')}",
        f"Tools you can call: {len(TOOL_REGISTRY)}",
    ]
    caps = []
    caps.append(f"voice output: {'on' if _TTS_OK else 'unavailable'}")
    caps.append(f"microphone: {'on' if _STT_OK else 'unavailable'}")
    _hb = hand_backend_name()
    caps.append(f"hand gestures: {_hb if _hb != 'none' else 'unavailable'}")
    caps.append(f"system control: {'on' if _OS_CONTROL_OK else 'unavailable'}")
    lines.append("Capabilities — " + ", ".join(caps))
    try:
        lines.append("Tool catalogue by category:\n" + capability_summary())
    except Exception:
        pass
    for k, v in (extras or {}).items():
        lines.append(f"{k}: {v}")
    return "[RUNTIME CONTEXT — this is you, right now]\n" + "\n".join(lines)


AGENT_SYSTEM_PROMPT = ISHA_IDENTITY + """

YOUR ARCHITECTURE
You are the reasoning core of ISHA. Think of it as one body:
- You (a GGUF model running inside this process through llama.cpp) are the BRAIN.
- ISHA's tool layer is your HANDS. It really opens apps, reads and writes files,
  inspects and changes the operating system on this computer.
- The orchestrator wires the two together: you choose an action, ISHA executes it
  through a validated, permission-checked gateway, and the real result comes back
  to you before you say anything to the user.
Nothing reaches the machine except through that gateway. That is why you can act
freely inside it: the dangerous operations are gated for you, not by you.

HOW TO THINK ABOUT A REQUEST
1. Work out what the user actually wants, in plain terms. Do not pattern-match on
   exact wording; "Recycle Bin clean kar do", "trash khaali karo" and "empty the
   bin" are one intent.
2. Decide which capability that intent needs — an app action, a file action, a
   window action, a system query, and so on.
3. Pick the tool for that capability and call it with concrete arguments.
4. Read the real result. Decide the next step from it, not from what you assumed
   would happen.
5. When the whole task is done, say what happened in one or two sentences.

ACT, DON'T NARRATE
- When the user asks for an action, CALL the tool. Never write "I will use
  open_app", "main X tool ka upyog karunga", a plan, or a "Result:" section.
- Never claim something happened unless a tool result in this conversation says so.

NEVER DEAD-END
- Never tell the user "I don't have a tool for that" as your first answer. If no
  tool name looks obviously right, call list_capabilities first and look properly.
- Tool names do not have to be guessed exactly; ISHA resolves close names. But
  prefer the real name when you know it.
- If a capability genuinely does not exist, say so in one line and name the
  closest thing you CAN do.

MULTI-STEP WORK
- One tool call at a time. Read its result before the next.
- A request can need four or five steps. Keep going until it is actually finished,
  not until you have done the first part.
- Never guess a file path you have not seen. Use list_directory or
  advanced_file_search first, then act on what you found.

TRUTHFULNESS ABOUT RESULTS
- A tool result beginning with ERROR means the action DID NOT HAPPEN. Never report
  success for it. Read the error, and either fix the arguments and retry once, or
  try one genuinely different approach, or report the failure plainly.
- A result of DENIED or CANCELLED means the user said no. Stop that line of work
  immediately. Do not look for a workaround.
- Never invent a result you did not receive.

OUTPUT
- Plain conversational language. No JSON, no tool syntax, no markdown, no bullet
  lists — you are spoken aloud.
- Reply in the same language the user wrote in (Hindi, Hinglish or English)."""


# ------------------------------------------------

class TTSWorker(QThread):
    """Speaks one reply, out loud, without ever failing silently.

    Order of attack per chunk: edge-tts -> pyttsx3 -> the OS's own engine.
    Whatever goes wrong is reported through error_occurred instead of being
    swallowed, and `stop()` cuts speech off mid-sentence (used when the mic
    is switched on, so ISHA doesn't transcribe its own voice)."""

    finished_speaking = pyqtSignal()
    error_occurred    = pyqtSignal(str)

    def __init__(self, text: str, voice: str = "en-US-AriaNeural",
                 rate: str = "+0%", parent=None):
        super().__init__(parent)
        self.text  = text
        self.voice = voice or "en-US-AriaNeural"
        self.rate  = rate or "+0%"
        self._abort = False
        self._proc  = None

    # ---- control ----
    def stop(self):
        self._abort = True
        try:
            if _PYGAME_OK and pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass
        p = self._proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass

    # ---- main ----
    def run(self):
        try:
            self._speak_all()
        except Exception as e:
            self.error_occurred.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished_speaking.emit()

    def _speak_all(self):
        text = _strip_for_speech(self.text)
        if not text:
            return
        if not _TTS_OK:
            self.error_occurred.emit(
                "No usable voice engine. Fix with:  pip install edge-tts pygame  "
                "(or: pip install pyttsx3).  " + _tts_diagnostics())
            return

        problems = []
        for chunk in _chunk_for_speech(text):
            if self._abort:
                return
            if not self._speak_chunk(chunk, problems):
                break                      # every engine refused - stop retrying
        if problems and not self._abort:
            uniq = list(dict.fromkeys(problems))
            self.error_occurred.emit(" | ".join(uniq) + "  [" + _tts_diagnostics() + "]")

    def _speak_chunk(self, chunk: str, problems: list) -> bool:
        if _EDGE_TTS_OK and _MP3_PLAYER_KIND:
            try:
                self._edge_speak(chunk)
                return True
            except Exception as e:
                problems.append(f"edge-tts: {type(e).__name__}: {e}")
        if self._abort:
            return False
        if _PYTTSX3_OK:
            try:
                self._pyttsx3_speak(chunk)
                return True
            except Exception as e:
                problems.append(f"pyttsx3: {e}")
        if self._abort:
            return False
        if _NATIVE_TTS_OK:
            try:
                self._native_speak(chunk)
                return True
            except Exception as e:
                problems.append(f"native TTS: {e}")
        return False

    # ---- engine 1: edge-tts ----
    def _edge_speak(self, chunk: str):
        last_err = None
        for attempt in range(3):
            if self._abort:
                return
            path = os.path.join(
                tempfile.gettempdir(),
                f"isha_tts_{os.getpid()}_{int(time.time()*1000)}_{attempt}.mp3")
            try:
                self._synth_sync(chunk, path)
                # edge-tts happily "succeeds" while writing a 0-byte file when
                # the voice name is wrong or the DRM token handshake failed.
                if (not os.path.exists(path)) or os.path.getsize(path) < 1024:
                    raise RuntimeError(
                        "no audio returned (check voice name, internet, and that "
                        "the system clock is correct)")
                self._play_mp3(path)
                return
            except Exception as e:
                last_err = e
                time.sleep(0.5 * (attempt + 1))
            finally:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
        raise last_err if last_err else RuntimeError("synthesis failed")

    def _synth_sync(self, chunk: str, out_path: str):
        """Run edge-tts's async API from this worker thread.

        A QThread has no running event loop, and `asyncio.run` can trip over
        Windows' Proactor loop off the main thread, so we build a loop by hand
        and fall back to a selector loop if that fails."""
        async def _go():
            comm = edge_tts.Communicate(chunk, voice=self.voice, rate=self.rate)
            await comm.save(out_path)

        def _run_on(loop):
            try:
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_go())
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                try:
                    loop.run_until_complete(asyncio.sleep(0.05))
                except Exception:
                    pass
                asyncio.set_event_loop(None)
                loop.close()

        try:
            _run_on(asyncio.new_event_loop())
        except (NotImplementedError, ValueError, RuntimeError):
            if not hasattr(asyncio, "SelectorEventLoop"):
                raise
            _run_on(asyncio.SelectorEventLoop())

    # ---- mp3 playback ----
    def _play_mp3(self, path: str):
        if _MP3_PLAYER_KIND == "pygame":
            self._play_pygame(path)
        elif _MP3_PLAYER_KIND == "powershell":
            self._play_windows(path)
        elif _MP3_PLAYER_KIND == "cmd":
            self._run_proc(list(_MP3_PLAYER_CMD) + [path])
        else:
            raise RuntimeError("no mp3 player available")

    def _play_pygame(self, path: str):
        if not pygame.mixer.get_init():
            try:
                pygame.mixer.init()
            except Exception:
                pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=1024)
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(1.0)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if self._abort:
                pygame.mixer.music.stop()
                break
            time.sleep(0.03)
        # Release the file handle (Windows won't let us delete an open mp3) but
        # deliberately do NOT call pygame.mixer.quit() - tearing the device down
        # and reopening it between every sentence is what made the 2nd utterance
        # come out silent, and it also killed any song that was playing.
        try:
            pygame.mixer.music.unload()
        except Exception:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass

    def _play_windows(self, path: str):
        ps = (
            "Add-Type -AssemblyName presentationCore;"
            "$p = New-Object System.Windows.Media.MediaPlayer;"
            f"$p.Open([uri]'{path}');"
            "$n = 0; while (-not $p.NaturalDuration.HasTimeSpan -and $n -lt 100) "
            "{ Start-Sleep -Milliseconds 50; $n++ };"
            "$p.Play();"
            "if ($p.NaturalDuration.HasTimeSpan) "
            "{ Start-Sleep -Milliseconds ([int]$p.NaturalDuration.TimeSpan.TotalMilliseconds + 350) } "
            "else { Start-Sleep -Seconds 3 };"
            "$p.Stop(); $p.Close()"
        )
        self._run_proc(self._powershell_argv(ps))

    # ---- engine 2: pyttsx3 ----
    def _pyttsx3_speak(self, chunk: str):
        co_init = False
        if _IS_WINDOWS:
            try:
                import pythoncom            # pyttsx3's SAPI driver needs COM per-thread
                pythoncom.CoInitialize()
                co_init = True
            except Exception:
                pass
        engine = pyttsx3.init()
        try:
            pct = self._rate_percent()
            if pct:
                base = engine.getProperty("rate") or 200
                engine.setProperty("rate", max(60, int(base * (1 + pct / 100.0))))
            engine.say(chunk)
            engine.runAndWait()
        finally:
            try:
                engine.stop()
            except Exception:
                pass
            if co_init:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    # ---- engine 3: whatever the OS ships with ----
    def _native_speak(self, chunk: str):
        if _IS_WINDOWS:
            safe = chunk.replace("'", "''")
            ps = ("Add-Type -AssemblyName System.Speech;"
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                  f"$s.Rate = {self._sapi_rate()};"
                  f"$s.Speak('{safe}')")
            self._run_proc(self._powershell_argv(ps))
            return
        if _IS_MACOS:
            self._run_proc(["say", chunk])
            return
        exe = _which_first("espeak-ng", "espeak", "spd-say")
        if not exe:
            raise RuntimeError("no espeak / spd-say binary found")
        if os.path.basename(exe).startswith("spd-say"):
            self._run_proc([exe, "-w", chunk])
        else:
            wpm = int(175 * (1 + self._rate_percent() / 100.0))
            self._run_proc([exe, "-s", str(max(80, min(400, wpm))), chunk])

    # ---- helpers ----
    def _rate_percent(self) -> int:
        m = re.match(r"\s*([+-]?\d+)\s*%", str(self.rate or ""))
        return int(m.group(1)) if m else 0

    def _sapi_rate(self) -> int:
        return max(-10, min(10, int(round(self._rate_percent() / 10.0))))

    @staticmethod
    def _powershell_argv(script: str) -> list:
        exe = _which_first("powershell", "pwsh") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script]

    def _run_proc(self, argv: list):
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if _IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
        self._proc = subprocess.Popen(argv, **kwargs)
        try:
            while self._proc.poll() is None:
                if self._abort:
                    self._proc.terminate()
                    break
                time.sleep(0.05)
            rc = self._proc.returncode
            if rc not in (0, None) and not self._abort:
                err = b""
                try:
                    err = (self._proc.stderr.read() or b"")[:200]
                except Exception:
                    pass
                raise RuntimeError(
                    f"{os.path.basename(argv[0])} exited {rc} "
                    f"{err.decode('utf-8', 'ignore').strip()}".strip())
        finally:
            self._proc = None


# ------------------------------------------------

class MicListenerThread(QThread):
    speech_recognized = pyqtSignal(str)
    error_occurred     = pyqtSignal(str)

    def __init__(self, language: str = "en-IN", parent=None):
        super().__init__(parent)
        self.language = language
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        if not _STT_OK:
            self.error_occurred.emit(
                "Microphone ke liye packages missing. Install: pip install SpeechRecognition PyAudio")
            return
        recognizer = sr.Recognizer()
        try:
            mic = sr.Microphone()
        except Exception as e:
            self.error_occurred.emit(f"Microphone not available: {e}")
            return
        try:
            with mic as source:
                try:
                    recognizer.adjust_for_ambient_noise(source, duration=0.6)
                except Exception:
                    pass
                while self._running:
                    try:
                        audio = recognizer.listen(source, timeout=4, phrase_time_limit=12)
                    except sr.WaitTimeoutError:
                        continue
                    except Exception as e:
                        if self._running:
                            self.error_occurred.emit(str(e))
                        continue
                    if not self._running:
                        break
                    try:
                        text = recognizer.recognize_google(audio, language=self.language)
                        if text:
                            self.speech_recognized.emit(text)
                    except sr.UnknownValueError:
                        continue
                    except sr.RequestError as e:
                        self.error_occurred.emit(f"Speech service error: {e}")
                    except Exception as e:
                        self.error_occurred.emit(str(e))
        except Exception as e:
            self.error_occurred.emit(f"Mic error: {e}")


# ------------------------------------------------
# UI Widgets (copied verbatim from original)
class Star:
    def __init__(self, max_w: int, max_h: int):
        self.max_w = max_w; self.max_h = max_h
        self._reset()
    def _reset(self):
        self.x     = random.uniform(0, self.max_w)
        self.y     = random.uniform(0, self.max_h)
        self.vx    = random.uniform(-0.25, 0.25)
        self.vy    = random.uniform(-0.15, 0.15)
        self.sz    = random.uniform(0.6, 2.2)
        a          = random.randint(60, 190)
        self.color = QColor(random.randint(0, 60),
                            random.randint(160, 240),
                            random.randint(200, 255), a)
    def update(self):
        self.x += self.vx; self.y += self.vy
        if not (0 <= self.x <= self.max_w and 0 <= self.y <= self.max_h):
            self._reset()


class WaveformBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(60, 36)
        self._active = False
        self._bars   = [0.0] * 7
        self._t = QTimer(self); self._t.timeout.connect(self._tick)
    def set_active(self, v: bool):
        self._active = v
        if v: self._t.start(45)
        else: self._t.stop(); self._bars = [0.0] * 7; self.update()
    def _tick(self):
        for i in range(len(self._bars)):
            self._bars[i] = random.uniform(0.15, 1.0) if self._active else 0.0
        self.update()
    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        bw = 5; gap = 3
        total = len(self._bars) * bw + (len(self._bars) - 1) * gap
        x0 = (W - total) // 2
        for i, h_frac in enumerate(self._bars):
            bh = max(4, int(H * 0.7 * h_frac))
            x  = x0 + i * (bw + gap)
            y  = (H - bh) // 2
            g  = QLinearGradient(x, y, x, y + bh)
            g.setColorAt(0, QColor(0, 240, 255, 200))
            g.setColorAt(1, QColor(0, 100, 200, 120))
            p.setPen(Qt.NoPen); p.setBrush(QBrush(g))
            p.drawRoundedRect(x, y, bw, bh, 2, 2)


class ThinkingDots(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 36)
        self._phase = 0; self._on = False
        self._t = QTimer(self); self._t.timeout.connect(self._tick)
    def start(self): self._on = True;  self._t.start(200)
    def stop(self):  self._on = False; self._t.stop();  self.update()
    def _tick(self): self._phase = (self._phase + 1) % 4; self.update()
    def paintEvent(self, e):
        if not self._on: return
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        for i in range(3):
            r = 4.5 if (self._phase == i or self._phase == 3) else 3.0
            a = 230 if self._phase == i else 80
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 240, 255, a))
            cx = W // 4 + i * (W // 4)
            p.drawEllipse(QPointF(cx, H // 2), r, r)


class MicButton(QWidget):
    clicked = pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(62, 62)
        self.active  = False; self.hover_v = 0.0
        self.wave_r  = [0.0, 8.0, 16.0]; self.wave_a = [0, 0, 0]
        self._hv = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=200)
        self._hv.valueChanged.connect(
            lambda v: (setattr(self, "hover_v", v), self.update()))
        self._wt = QTimer(self); self._wt.timeout.connect(self._tick_wave)
        self.setCursor(Qt.PointingHandCursor)
    def _tick_wave(self):
        for i in range(3):
            self.wave_r[i] = (self.wave_r[i] + 1.4) % 26
            self.wave_a[i] = max(0, int(200 * (1 - self.wave_r[i] / 26)))
        self.update()
    def set_active(self, v: bool):
        self.active = v
        if v: self._wt.start(16)
        else: self._wt.stop(); self.wave_a = [0, 0, 0]
        self.update()
    def enterEvent(self, e):
        self._hv.setDirection(QVariantAnimation.Forward);  self._hv.start()
    def leaveEvent(self, e):
        self._hv.setDirection(QVariantAnimation.Backward); self._hv.start()
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton: self.clicked.emit()
    def paintEvent(self, e):
        p  = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        cx, cy, R = self.width() // 2, self.height() // 2, 23
        if self.hover_v > 0:
            g = QRadialGradient(cx, cy, R + 18)
            g.setColorAt(0, QColor(0, 240, 255, int(80 * self.hover_v)))
            g.setColorAt(1, QColor(0, 0, 0, 0))
            p.setPen(Qt.NoPen); p.setBrush(QBrush(g))
            p.drawEllipse(QPointF(cx, cy), R + 18, R + 18)
        if self.active:
            for i in range(3):
                if self.wave_a[i] > 0:
                    p.setPen(QPen(QColor(0, 240, 255, self.wave_a[i]), 1.2))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(QPointF(cx, cy), R + self.wave_r[i], R + self.wave_r[i])
        g2 = QRadialGradient(cx - 7, cy - 7, R * 1.8)
        if self.active:
            g2.setColorAt(0, QColor(0, 220, 255)); g2.setColorAt(1, QColor(0, 80, 160))
        else:
            g2.setColorAt(0, QColor(20, 30, 60)); g2.setColorAt(1, QColor(10, 15, 38))
        p.setPen(QPen(
            QColor(0, 240, 255, 140) if self.active else QColor(0, 120, 180, 90), 1.5))
        p.setBrush(QBrush(g2))
        p.drawEllipse(QPointF(cx, cy), R, R)
        col = QColor(255, 255, 255) if self.active else QColor(185, 225, 255)
        p.setPen(QPen(col, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        body = QPainterPath()
        body.addRoundedRect(QRectF(cx - 5, cy - 12, 10, 14), 5, 5)
        p.drawPath(body)
        p.drawArc(QRectF(cx - 9, cy - 5, 18, 12), 0, -180 * 16)
        p.drawLine(QPointF(cx, cy + 7), QPointF(cx, cy + 12))
        p.drawLine(QPointF(cx - 4.5, cy + 12), QPointF(cx + 4.5, cy + 12))


class SettingsButton(QWidget):
    clicked = pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(54, 54)
        self.hover_v  = 0.0; self.rotation = 0.0
        self._hv = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=200)
        self._hv.valueChanged.connect(
            lambda v: (setattr(self, "hover_v", v), self.update()))
        self._st = QTimer(self); self._st.timeout.connect(self._spin)
        self.setCursor(Qt.PointingHandCursor)
    def _spin(self): self.rotation = (self.rotation + 2.5) % 360; self.update()
    def enterEvent(self, e):
        self._hv.setDirection(QVariantAnimation.Forward);  self._hv.start()
        self._st.start(18)
    def leaveEvent(self, e):
        self._hv.setDirection(QVariantAnimation.Backward); self._hv.start()
        self._st.stop()
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton: self.clicked.emit()
    def paintEvent(self, e):
        p  = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        cx, cy, R = self.width() // 2, self.height() // 2, 21
        if self.hover_v > 0:
            g = QRadialGradient(cx, cy, R + 14)
            g.setColorAt(0, QColor(0, 240, 255, int(70 * self.hover_v)))
            g.setColorAt(1, QColor(0, 0, 0, 0))
            p.setPen(Qt.NoPen); p.setBrush(QBrush(g))
            p.drawEllipse(QPointF(cx, cy), R + 14, R + 14)
        g2 = QRadialGradient(cx - 5, cy - 5, R * 1.6)
        g2.setColorAt(0, QColor(10, 20, 50)); g2.setColorAt(1, QColor(5, 10, 30))
        p.setBrush(QBrush(g2)); p.setPen(QPen(QColor(0, 180, 255, 100), 1.2))
        p.drawEllipse(QPointF(cx, cy), R, R)
        p.save(); p.translate(cx, cy); p.rotate(self.rotation)
        teeth = 8; pts = []
        for i in range(teeth * 2):
            a = math.radians(i * 180 / teeth)
            r = 9.5 if i % 2 == 0 else 6.8
            pts.append(QPointF(r * math.cos(a), r * math.sin(a)))
        path = QPainterPath(); path.moveTo(pts[0])
        for pt in pts[1:]: path.lineTo(pt)
        path.closeSubpath()
        p.setPen(QPen(QColor(0, 240, 255, 200), 1.3)); p.setBrush(Qt.NoBrush)
        p.drawPath(path); p.drawEllipse(QPointF(0, 0), 3.5, 3.5)
        p.restore()


class SearchBar(QLineEdit):
    submitted = pyqtSignal(str)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Ask ISHA anything…")
        self.setFixedHeight(48); self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFrame(False); self.setFont(QFont("Segoe UI", 11))
        self.setStyleSheet("""
            QLineEdit {
                background: transparent; color: #D7E1FF;
                border: none; padding: 0 14px;
                selection-background-color: rgba(0,240,255,80);
            }
        """)
        self.setAcceptDrops(True)
        self.dropped_file_content = None
        self.returnPressed.connect(self._submit)
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)
    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)
    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                file_path = urls[0].toLocalFile()
                if os.path.exists(file_path):
                    ext = os.path.splitext(file_path)[1].lower()
                    if ext in ['.txt', '.py', '.html', '.css', '.js', '.json', '.md']:
                        try:
                            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                            self.dropped_file_content = content
                            filename = os.path.basename(file_path)
                            self.setText(f"[File: {filename}] ")
                            self.setFocus()
                        except Exception as e:
                            print(f"Error reading dropped file: {e}")
            event.acceptProposedAction()
        else:
            super().dropEvent(event)
    def _submit(self):
        t = self.text().strip()
        if t: self.submitted.emit(t); self.clear()
    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(2, 8, self.width() - 4, self.height() - 16), 16, 16)
        p.fillPath(path, QColor(0, 240, 255, 8))
        p.setPen(QPen(QColor(0, 200, 255, 50), 1)); p.setBrush(Qt.NoBrush)
        p.drawPath(path); p.end(); super().paintEvent(e)


class BubbleLabel(QWidget):
    """The floating reply bubble.

    It is now a top-level window of its own rather than a child of the ISHA bar.
    The bar is a fixed 760x82, so a child widget could never be painted above
    it — the old placement maths went negative and got clamped to y=2, which is
    why the bubble ended up sitting *inside* the search bar (that small rounded
    box with the dot). As a separate always-on-top window it can float freely
    above the whole UI."""

    def __init__(self, parent=None):
        flags = (Qt.Tool | Qt.FramelessWindowHint |
                 Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self._owner = parent
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._text    = ""; self._role = "isha"
        self._opacity = 0.0; self._yo = 20.0
        self._oa = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=320)
        self._oa.setEasingCurve(QEasingCurve.OutCubic)
        self._oa.valueChanged.connect(
            lambda v: (setattr(self, "_opacity", v), self.update()))
        self._ya = QVariantAnimation(self, startValue=20.0, endValue=0.0, duration=320)
        self._ya.setEasingCurve(QEasingCurve.OutCubic)
        self._ya.valueChanged.connect(
            lambda v: (setattr(self, "_yo", v), self.update()))
        self._ht = QTimer(self); self._ht.setSingleShot(True)
        self._ht.timeout.connect(self._fade_out); self.hide()
    def current_text(self) -> str:
        return self._text

    def show_text(self, text: str, role: str = "isha", ms: int = 5500):
        self._text = text; self._role = role; self._resize()
        self.show(); self.raise_()
        self._oa.setDirection(QVariantAnimation.Forward); self._oa.start()
        self._ya.setDirection(QVariantAnimation.Forward); self._ya.start()
        self._ht.start(ms)
    def begin_stream(self):
        """Show the bubble immediately (no fade timer yet) so live tokens can be pushed in."""
        self._ht.stop()
        self._role = "isha"; self._text = ""
        self._opacity = 1.0; self._yo = 0.0
        self._resize(); self.show(); self.raise_(); self.update()
    def update_streaming_text(self, text: str):
        """Update bubble contents live while the model is still generating."""
        self._text = text
        self._resize()
        self.update()
    def _fade_out(self):
        self._oa.setDirection(QVariantAnimation.Backward)
        self._oa.finished.connect(self.hide); self._oa.start()
    def _resize(self):
        fm   = QFontMetrics(QFont("Segoe UI", 11))
        rect = fm.boundingRect(QRectF(0, 0, 520, 9999).toRect(),
                                Qt.TextWordWrap, self._text)
        self.setFixedSize(max(160, min(572, rect.width() + 52)),
                          max(44,  rect.height() + 30))
    def paintEvent(self, e):
        if self._opacity <= 0: return
        p  = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self._opacity)
        W, H, yo = self.width(), self.height(), int(self._yo)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, yo, W, H - yo), 14, 14)
        if self._role == "isha":
            fill = QLinearGradient(0, yo, W, H)
            fill.setColorAt(0, QColor(5, 10, 25, 235))
            fill.setColorAt(1, QColor(10, 18, 40, 235))
            border_c = QColor(0, 240, 255, 120)
        else:
            fill = QLinearGradient(0, yo, W, H)
            fill.setColorAt(0, QColor(0, 60, 80, 220))
            fill.setColorAt(1, QColor(0, 40, 65, 215))
            border_c = QColor(0, 240, 255, 180)
        p.fillPath(path, QBrush(fill))
        p.setPen(QPen(border_c, 1.4)); p.setBrush(Qt.NoBrush); p.drawPath(path)
        sheen = QPainterPath()
        sheen.addRoundedRect(QRectF(2, yo + 2, W - 4, 20), 12, 12)
        p.fillPath(sheen, QColor(255, 255, 255, 7))
        dot_c = C_NEON if self._role == "isha" else QColor(0, 240, 255, 220)
        p.setPen(Qt.NoPen); p.setBrush(dot_c)
        p.drawEllipse(QPointF(16, yo + (H - yo) // 2), 3.5, 3.5)
        p.setFont(QFont("Segoe UI", 11))
        tc = C_TEXT if self._role == "isha" else QColor(200, 245, 255)
        p.setPen(tc)
        p.drawText(QRectF(28, yo + 10, W - 40, H - yo - 20),
                   Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignVCenter, self._text)


class ChatRow(QWidget):
    def __init__(self, text: str, role: str, ts: str = "", parent=None):
        super().__init__(parent)
        self._text = text; self._role = role; self._ts = ts
        fm   = QFontMetrics(QFont("Segoe UI", 10))
        rect = fm.boundingRect(QRectF(0, 0, 440, 9000).toRect(),
                                Qt.TextWordWrap, text)
        self.setFixedSize(500, rect.height() + 34)
    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        bubble_path = QPainterPath()
        if self._role == "isha":
            bubble_path.addRoundedRect(QRectF(6, 3, W - 16, H - 6), 10, 10)
            p.fillPath(bubble_path, QBrush(QColor(255, 255, 255, 10)))
            p.setPen(QPen(QColor(0, 200, 255, 60), 1))
            p.setBrush(Qt.NoBrush); p.drawPath(bubble_path)
            bar_path = QPainterPath()
            bar_path.addRoundedRect(QRectF(6, 3, 3, H - 6), 1.5, 1.5)
            p.setPen(Qt.NoPen); p.setBrush(QColor(0, 240, 255, 230))
            p.drawPath(bar_path)
            p.setFont(QFont("Segoe UI Semibold", 8))
            p.setPen(QColor(0, 200, 255, 200))
            p.drawText(QRectF(16, 4, 60, 14), Qt.AlignLeft | Qt.AlignVCenter, "ISHA")
            if self._ts:
                p.setFont(QFont("Segoe UI", 7)); p.setPen(QColor(60, 90, 130, 160))
                p.drawText(QRectF(W - 90, 4, 82, 14), Qt.AlignRight | Qt.AlignVCenter, self._ts)
            p.setFont(QFont("Segoe UI", 10)); p.setPen(C_TEXT)
            p.drawText(QRectF(16, 18, W - 28, H - 24),
                       Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, self._text)
        else:
            bubble_path.addRoundedRect(QRectF(16, 3, W - 22, H - 6), 10, 10)
            fill = QLinearGradient(0, 0, W, 0)
            fill.setColorAt(0, QColor(0, 50, 70, 200)); fill.setColorAt(1, QColor(0, 70, 100, 210))
            p.fillPath(bubble_path, QBrush(fill))
            p.setPen(QPen(QColor(0, 240, 255, 120), 1))
            p.setBrush(Qt.NoBrush); p.drawPath(bubble_path)
            p.setFont(QFont("Segoe UI Semibold", 8))
            p.setPen(QColor(0, 240, 255, 180))
            p.drawText(QRectF(24, 4, 32, 14), Qt.AlignLeft | Qt.AlignVCenter, "You")
            if self._ts:
                p.setFont(QFont("Segoe UI", 7)); p.setPen(QColor(60, 90, 130, 160))
                p.drawText(QRectF(W - 90, 4, 82, 14), Qt.AlignRight | Qt.AlignVCenter, self._ts)
            p.setFont(QFont("Segoe UI", 10)); p.setPen(QColor(220, 250, 255))
            p.drawText(QRectF(24, 18, W - 36, H - 24),
                       Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, self._text)


class ChatPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(560); self.setMaximumHeight(340)
        self._visible = False; self.setWindowOpacity(0.0)
        self._scroll = QScrollArea(self); self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: rgba(0,20,30,80); width: 4px; border-radius: 2px; }
            QScrollBar::handle:vertical { background: rgba(0,240,255,140); border-radius: 2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        self._ctr = QWidget(); self._ctr.setAttribute(Qt.WA_TranslucentBackground)
        self._lay = QVBoxLayout(self._ctr)
        self._lay.setContentsMargins(8, 8, 8, 8); self._lay.setSpacing(5)
        self._lay.addStretch(); self._scroll.setWidget(self._ctr)
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._scroll)
        self._oa = QPropertyAnimation(self, b"windowOpacity")
        self._oa.setDuration(280); self._oa.setEasingCurve(QEasingCurve.OutCubic)
    def add_entry(self, text: str, role: str):
        ts  = datetime.now().strftime("%I:%M %p")
        row = ChatRow(text, role, ts, self._ctr)
        self._lay.insertWidget(self._lay.count() - 1, row)
        QTimer.singleShot(60, self._scroll_to_bottom)
    def _scroll_to_bottom(self):
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
    def toggle(self):
        self._visible = not self._visible
        self._oa.setStartValue(0.0 if self._visible else 1.0)
        self._oa.setEndValue(1.0 if self._visible else 0.0)
        if self._visible: self.show()
        try: self._oa.finished.disconnect()
        except Exception: pass
        if not self._visible:
            self._oa.finished.connect(self.hide)
        self._oa.start()


class _ToggleCard(QWidget):
    toggled = pyqtSignal(bool)
    def __init__(self, label: str, icon: str, icon_path: str = None, parent=None):
        super().__init__(parent)
        self.label = label
        self.icon = icon
        self.icon_path = icon_path
        self.on = False
        self.hov = False
        self.setFixedSize(84, 60)
        self.setCursor(Qt.PointingHandCursor)
        self._load_icon()
        
    def _load_icon(self):
        """Load custom icon if path is provided"""
        self._pixmap = None
        if self.icon_path and os.path.exists(self.icon_path):
            pixmap = QPixmap(self.icon_path)
            if not pixmap.isNull():
                # Scale icon to fit nicely
                self._pixmap = pixmap.scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                
    def set_on(self, value: bool):
        self.on = value
        self.update()
        
    def enterEvent(self, e): 
        self.hov = True
        self.update()
        
    def leaveEvent(self, e): 
        self.hov = False
        self.update()
        
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.on = not self.on
            self.toggled.emit(self.on)
            self.update()
            
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 10, 10)
        
        if self.on:
            fill = QLinearGradient(0, 0, W, H)
            fill.setColorAt(0, QColor(0, 80, 120, 220))
            fill.setColorAt(1, QColor(0, 100, 150, 220))
        elif self.hov:
            fill = QLinearGradient(0, 0, W, H)
            fill.setColorAt(0, QColor(10, 30, 50, 200))
            fill.setColorAt(1, QColor(15, 40, 65, 200))
        else:
            fill = QLinearGradient(0, 0, W, H)
            fill.setColorAt(0, QColor(6, 10, 25, 190))
            fill.setColorAt(1, QColor(10, 16, 38, 190))
        p.fillPath(path, QBrush(fill))
        
        bc = QColor(0,240,255,140) if self.on else QColor(0,120,180,60)
        p.setPen(QPen(bc, 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        
        if self._pixmap:
            
            icon_x = (W - self._pixmap.width()) // 2
            icon_y = 4
            p.drawPixmap(icon_x, icon_y, self._pixmap)
        else:
            # Fallback to emoji
            p.setFont(QFont("Segoe UI Emoji", 15))
            p.setPen(QColor(220,245,255) if self.on else QColor(80,140,180))
            p.drawText(QRectF(0, 2, W, 34), Qt.AlignHCenter | Qt.AlignVCenter, self.icon)
        
        # Draw label
        p.setFont(QFont("Segoe UI", 7))
        p.setPen(QColor(190,240,255) if self.on else QColor(80,130,170))
        p.drawText(QRectF(0, H - 18, W, 16), Qt.AlignHCenter | Qt.AlignVCenter, self.label)


class _ActionCard(QWidget):
    clicked = pyqtSignal()
    def __init__(self, label: str, icon: str, icon_path: str = None, parent=None):
        super().__init__(parent)
        self.label = label
        self.icon = icon  
        self.icon_path = icon_path  
        self.hov = False
        self.setFixedHeight(36)
        self.setCursor(Qt.PointingHandCursor)
        self._load_icon()
        
    def _load_icon(self):
        """Load custom icon if path is provided"""
        self._pixmap = None
        if self.icon_path and os.path.exists(self.icon_path):
            pixmap = QPixmap(self.icon_path)
            if not pixmap.isNull():
                # Scale icon to fit nicely
                self._pixmap = pixmap.scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                
    def enterEvent(self, e): 
        self.hov = True
        self.update()
        
    def leaveEvent(self, e): 
        self.hov = False
        self.update()
        
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton: 
            self.clicked.emit()
            
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        
        if self.hov:
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 1, W, H - 2), 8, 8)
            p.fillPath(path, QBrush(QColor(0, 40, 60, 190)))
        
        
        if self._pixmap:
            
            icon_x = 12
            icon_y = (H - self._pixmap.height()) // 2
            p.drawPixmap(icon_x, icon_y, self._pixmap)
            text_x = 38  
        else:
            
            p.setFont(QFont("Segoe UI Emoji", 12))
            p.setPen(QColor(0, 200, 240))
            p.drawText(QRectF(10, 0, 26, H), Qt.AlignLeft | Qt.AlignVCenter, self.icon)
            text_x = 40
        
        # Draw label text
        p.setFont(QFont("Segoe UI", 10))
        p.setPen(QColor(190, 235, 255) if self.hov else QColor(120, 175, 215))
        p.drawText(QRectF(text_x, 0, W - text_x - 8, H), 
                   Qt.AlignLeft | Qt.AlignVCenter, self.label)


class _LauncherPopup(QWidget):
    def __init__(self, title: str, items: list, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(300, 380)
        self._items = items
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12); root.setSpacing(7)
        hdr_row = QHBoxLayout()
        hdr = QLabel(title)
        hdr.setFont(QFont("Segoe UI Semibold", 10))
        hdr.setStyleSheet("color: #00F0FF; background: transparent;")
        hdr_row.addWidget(hdr, 1)
        close_btn_lp = QPushButton("✕")
        close_btn_lp.setFixedSize(22, 22)
        close_btn_lp.setStyleSheet("""
            QPushButton { background: rgba(60,0,0,180); color: #FF4040;
                border: 1px solid rgba(255,60,60,70); border-radius: 6px;
                font-size: 9pt; font-weight: bold; }
            QPushButton:hover { background: rgba(100,0,0,220); color: #FFFFFF; border: 1px solid rgb(255,80,80); }
            QPushButton:pressed { background: rgba(150,0,0,240); }
        """)
        close_btn_lp.setCursor(Qt.PointingHandCursor)
        close_btn_lp.clicked.connect(self._close)
        hdr_row.addWidget(close_btn_lp)
        root.addLayout(hdr_row)
        self._flt = QLineEdit()
        self._flt.setPlaceholderText("Filter…"); self._flt.setFixedHeight(28)
        self._flt.setStyleSheet("""
            QLineEdit { background: rgba(0,20,30,210); color: #C0E8FF;
                border: 1px solid rgba(0,200,255,80); border-radius: 6px;
                padding: 0 8px; font-size: 10px; }
        """)
        self._flt.textChanged.connect(self._filter); root.addWidget(self._flt)
        self._lst = QListWidget()
        self._lst.setWordWrap(True)
        self._lst.setStyleSheet("""
            QListWidget { background: transparent; border: none;
                color: #B0D8F0; font-size: 10px; outline: none; }
            QListWidget::item { padding: 4px 8px; border-radius: 5px; }
            QListWidget::item:hover { background: rgba(0,60,90,170); }
            QListWidget::item:selected { background: rgba(0,100,150,180); color: white; }
            QScrollBar:vertical { background: rgba(0,10,20,80); width: 4px; border-radius: 2px; }
            QScrollBar::handle:vertical { background: rgba(0,240,255,140); border-radius: 2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        for name, _ in items:
            self._lst.addItem(name)
        self._lst.itemClicked.connect(self._on_click)
        root.addWidget(self._lst)
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(200); self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.setWindowOpacity(0.0)
    def _filter(self, text: str):
        tl = text.lower()
        for i in range(self._lst.count()):
            item = self._lst.item(i)
            item.setHidden(tl != "" and tl not in item.text().lower())
    def _on_click(self, item: QListWidgetItem):
        idx = self._lst.row(item); _, cmd = self._items[idx]
        _popen_silent(cmd); self._close()
    def show_popup(self):
        self.setWindowOpacity(0.0); self.show()
        self._flt.clear(); self._filter("")
        self._anim.setStartValue(0.0); self._anim.setEndValue(1.0)
        try: self._anim.finished.disconnect()
        except Exception: pass
        self._anim.start()
    def _close(self):
        self._anim.setStartValue(self.windowOpacity()); self._anim.setEndValue(0.0)
        try: self._anim.finished.disconnect()
        except Exception: pass
        self._anim.finished.connect(self.hide); self._anim.start()
    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 14, 14)
        bg = QLinearGradient(0, 0, W // 2, H)
        bg.setColorAt(0, QColor(4, 8, 20, 252)); bg.setColorAt(1, QColor(8, 14, 35, 252))
        p.fillPath(path, QBrush(bg))
        p.setPen(QPen(QColor(0, 240, 255, 80), 1.5)); p.setBrush(Qt.NoBrush)
        p.drawPath(path)
    def mousePressEvent(self, e):
        if not self.rect().contains(e.pos()): self._close()


class AboutISHAWindow(QWidget):
    """Small 'About ISHA' popup: name, full form, credit."""
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(300, 220)
        self._drag_pos = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(6)

        hdr_row = QHBoxLayout()
        hdr_row.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setStyleSheet("""
            QPushButton { background: rgba(60,0,0,180); color: #FF4040;
                border: 1px solid rgba(255,60,60,70); border-radius: 6px;
                font-size: 9pt; font-weight: bold; }
            QPushButton:hover { background: rgba(100,0,0,220); color: #FFFFFF; border: 1px solid rgb(255,80,80); }
            QPushButton:pressed { background: rgba(150,0,0,240); }
        """)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        hdr_row.addWidget(close_btn)
        root.addLayout(hdr_row)

        root.addStretch()

        name_lbl = QLabel("ISHA")
        name_lbl.setAlignment(Qt.AlignCenter)
        name_lbl.setFont(QFont("Segoe UI", 28, QFont.Bold))
        name_lbl.setStyleSheet("color: #00F0FF; background: transparent;")
        root.addWidget(name_lbl)

        full_form_lbl = QLabel("Intelligent System for Human Assistance")
        full_form_lbl.setAlignment(Qt.AlignCenter)
        full_form_lbl.setWordWrap(True)
        full_form_lbl.setFont(QFont("Segoe UI", 10))
        full_form_lbl.setStyleSheet("color: #B0D8F0; background: transparent;")
        root.addWidget(full_form_lbl)

        root.addStretch()

        credit_lbl = QLabel("Powered by @Amit Vishawakram")
        credit_lbl.setAlignment(Qt.AlignCenter)
        credit_lbl.setFont(QFont("Segoe UI", 9))
        credit_lbl.setStyleSheet("color: #3C6488; background: transparent;")
        root.addWidget(credit_lbl)

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 14, 14)
        bg = QLinearGradient(0, 0, W // 2, H)
        bg.setColorAt(0, QColor(4, 8, 20, 252)); bg.setColorAt(1, QColor(8, 14, 35, 252))
        p.fillPath(path, QBrush(bg))
        p.setPen(QPen(QColor(0, 240, 255, 90), 1.5)); p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not _start_native_drag(self, e):
                self._drag_pos = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        
        if e.buttons() == Qt.LeftButton and self._drag_pos:
            self.move(e.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None


class QuickSettingsPopup(QWidget):
    _CELL_W = 126
    _CELL_H = 64
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.main_window   = parent          
        self._is_open      = False
        self._about_popup  = None
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(220); self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.setWindowOpacity(0.0)
        self._drag_pos = None   # for dragging
        self._build()
        self.adjustSize()
    
    def toggle(self, pos: QPoint):
        if self._is_open: self._do_hide()
        else:             self._do_show(pos)
    
    def _do_show(self, pos: QPoint):
        self.move(pos); self.setWindowOpacity(0.0); self.show()
        if self.main_window is not None:
            self.update_stats(getattr(self.main_window, "_latest_stats", {}))
        self._anim.stop()
        try: self._anim.finished.disconnect()
        except Exception: pass
        self._anim.setStartValue(0.0); self._anim.setEndValue(1.0); self._anim.start()
        self._is_open = True
    
    def _do_hide(self):
        self._anim.stop()
        try: self._anim.finished.disconnect()
        except Exception: pass
        self._anim.setStartValue(self.windowOpacity()); self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._after_hide); self._anim.start()
    
    def _after_hide(self):
        self.hide(); self._is_open = False
        try: self._anim.finished.disconnect(self._after_hide)
        except Exception: pass
    
    def _build(self):
        
        info_icon_path  = str(_ICON_DIR / "info.png")
        info_icon_path1 = str(_ICON_DIR / "task.png")
        info_icon_path2 = str(_ICON_DIR / "mute.png")
        info_icon_path3 = str(_ICON_DIR / "generative-image.png")
        info_icon_path4 = str(_ICON_DIR / "intuitive.png")
        info_icon_path5 = str(_ICON_DIR / "configuration.png")
        info_icon_path6 = str(_ICON_DIR / "medical-report.png")
        info_icon_path7 = str(_ICON_DIR / "chat.png")
        
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 16)
        root.setSpacing(9)
        
        hdr = QLabel("Quick Settings")
        hdr.setFont(QFont("Segoe UI Semibold", 11))
        hdr.setStyleSheet("color: #00F0FF; background: transparent;")
        root.addWidget(hdr, 0, Qt.AlignCenter)
        
        sep_top = QFrame(); sep_top.setFixedHeight(1)
        sep_top.setStyleSheet("background: rgba(0,200,255,40);")
        root.addWidget(sep_top)
        
        grid = QGridLayout()
        grid.setSpacing(8)
        grid.setContentsMargins(0, 0, 0, 0)
        cw, ch = self._CELL_W, self._CELL_H
        
        # All toggle cards with info icon
        self._t_mute = _ToggleCard("Mute", "🔇", info_icon_path2)
        self._t_display = _ToggleCard("Ask image", "🖥", info_icon_path3)
        self._t_mute.setFixedSize(cw, ch)
        self._t_display.setFixedSize(cw, ch)
        self._t_mute.toggled.connect(self._on_mute_toggled)
        self._t_display.toggled.connect(lambda on: print(f"UI: Display toggled {on}"))
        grid.addWidget(self._t_mute, 0, 0)
        grid.addWidget(self._t_display, 0, 1)

        self._t_hand_control = _ToggleCard("Hand", "🤚", info_icon_path4)
        self._t_hand_control.setFixedSize(cw, ch)
        self._t_hand_control.toggled.connect(self._on_hand_control_toggled)
        self._t_model_settings = _ToggleCard("Configurations", "🤖", info_icon_path5)
        self._t_model_settings.setFixedSize(cw, ch)
        self._t_model_settings.toggled.connect(self._on_model_settings_toggled)
        grid.addWidget(self._t_hand_control, 1, 0)
        grid.addWidget(self._t_model_settings, 1, 1)

        # History and Comm Dock side by side
        self._t_chat_history = _ToggleCard("History", "💬", info_icon_path6)
        self._t_chat_history.setFixedSize(cw, ch)
        self._t_chat_history.toggled.connect(self._on_history_toggled)
        grid.addWidget(self._t_chat_history, 2, 0)

        self._t_comm_dock = _ToggleCard("Comm Dock", "📡", info_icon_path7)
        self._t_comm_dock.setFixedSize(cw, ch)
        self._t_comm_dock.toggled.connect(self._on_comm_dock_toggled)
        grid.addWidget(self._t_comm_dock, 2, 1)

        self._t_agent = _ToggleCard("Agent Center", "🧠", info_icon_path5)
        self._t_agent.setFixedSize(cw, ch)
        self._t_agent.toggled.connect(
            lambda on: self.main_window.toggle_agent_panel(on) if self.main_window else None)
        grid.addWidget(self._t_agent, 3, 0)

        self._a_stop = _ToggleCard("Stop", "■", info_icon_path1)
        self._a_stop.setFixedSize(cw, ch)
        self._a_stop.toggled.connect(self._on_stop_card)
        grid.addWidget(self._a_stop, 3, 1)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        sep_mid = QFrame(); sep_mid.setFixedHeight(1)
        sep_mid.setStyleSheet("background: rgba(0,200,255,40);")
        root.addWidget(sep_mid)

        self._stats_label = QLabel("CPU -- % · RAM -- % · Battery -- %")
        self._stats_label.setFont(QFont("Segoe UI", 9))
        self._stats_label.setStyleSheet("color: #00C8FF; background: transparent;")
        self._stats_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self._stats_label)

        sep_stats = QFrame(); sep_stats.setFixedHeight(1)
        sep_stats.setStyleSheet("background: rgba(0,200,255,40);")
        root.addWidget(sep_stats)

        vl = QLabel("Volume")
        vl.setFont(QFont("Segoe UI", 9))
        vl.setStyleSheet("color: #006080; background: transparent;")
        root.addWidget(vl)
        self._vol = QSlider(Qt.Horizontal)
        self._vol.setRange(0, 100); self._vol.setValue(50); self._vol.setFixedHeight(22)
        self._last_vol = 50   # tracks previous slider value to compute live +/- delta
        self._vol.setStyleSheet("""
            QSlider::groove:horizontal { height:6px; background:rgba(0,40,60,180); border-radius:3px; }
            QSlider::handle:horizontal { width:16px; height:16px;
                background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #00F0FF,stop:1 #0080CC);
                border-radius:8px; margin:-5px 0; }
            QSlider::sub-page:horizontal { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #005080,stop:1 #00C0E0); border-radius:3px; }
        """)
        self._vol.valueChanged.connect(self._on_volume_changed)
        root.addWidget(self._vol)

        sep_bot = QFrame(); sep_bot.setFixedHeight(1)
        sep_bot.setStyleSheet("background: rgba(0,200,255,40);")
        root.addWidget(sep_bot)

        # Action cards with info icon
        apps_card = _ActionCard("Task Manager", "🚀", info_icon_path1)
        stgs_card = _ActionCard("About ISHA", "⚙️", info_icon_path)
        apps_card.clicked.connect(self._open_task_manager)
        stgs_card.clicked.connect(self._show_about_isha)
        root.addWidget(apps_card)
        root.addWidget(stgs_card)

        sep3 = QFrame(); sep3.setFixedHeight(1)
        sep3.setStyleSheet("background: rgba(0,200,255,40);")
        root.addWidget(sep3)

    def _on_stop_card(self, on: bool):
        self._a_stop.set_on(False)          # momentary button
        if self.main_window:
            self.main_window._stop_generation()

    def _on_comm_dock_toggled(self, on: bool):
        if self.main_window:
            self.main_window.toggle_comm_dock(on)

    def _on_model_settings_toggled(self, on: bool):
        if self.main_window:
            self.main_window.toggle_config_dialog(on)

    def _on_hand_control_toggled(self, on: bool):
        if self.main_window:
            self.main_window.toggle_hand_control(on)

    def _on_history_toggled(self, on: bool):
        if self.main_window:
            self.main_window.toggle_history_window(on)

    def _on_mute_toggled(self, on: bool):
        set_system_mute(on)
        if self.main_window:
            self.main_window.respond("Audio muted." if on else "Audio unmuted.", "isha")

    def _on_volume_changed(self, v: int):
        set_system_volume(v, self._last_vol)
        self._last_vol = v

    def update_stats(self, stats: dict):
        if not stats:
            return
        cpu = stats.get("cpu_percent")
        ram = stats.get("ram_percent")
        batt = stats.get("battery_percent")
        parts = []
        if cpu is not None: parts.append(f"CPU {cpu:.0f}%")
        if ram is not None: parts.append(f"RAM {ram:.0f}%")
        if batt is not None: parts.append(f"Battery {batt:.0f}%")
        self._stats_label.setText(" · ".join(parts) if parts else "Stats unavailable")

    def _open_task_manager(self):
        open_task_manager()

    def _show_about_isha(self):
        if not self._about_popup:
            self._about_popup = AboutISHAWindow(self)
        btn_g = self.mapToGlobal(QPoint(0, 0))
        self._about_popup.move(btn_g.x() - 300, btn_g.y())
        self._about_popup.show()
        self._about_popup.raise_()

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 14, 14)
        bg = QLinearGradient(0, 0, W // 2, H)
        bg.setColorAt(0, QColor(4,  8, 20, 252))
        bg.setColorAt(1, QColor(8, 14, 35, 252))
        p.fillPath(path, QBrush(bg))
        p.setPen(QPen(QColor(0, 240, 255, 80), 1.5))
        p.setBrush(Qt.NoBrush); p.drawPath(path)
        pen = QPen(QColor(0, 200, 255, 60), 1.2); p.setPen(pen)
        for (x, y, dx, dy) in [(1,1,10,0),(1,1,0,10),(W-1,1,-10,0),(W-1,1,0,10),
                                 (1,H-1,10,0),(1,H-1,0,-10),(W-1,H-1,-10,0),(W-1,H-1,0,-10)]:
            p.drawLine(x, y, x + dx, y + dy)

    # ---- Dragging support ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not _start_native_drag(self, e):
                self._drag_pos = e.globalPos() - self.frameGeometry().topLeft()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        
        if e.buttons() == Qt.LeftButton and self._drag_pos:
            self.move(e.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None


class CommDockWindow(QWidget):
    
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(540, 340)
        self._drag_pos = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(6)
        self._log_area = QTextEdit()
        self._log_area.setReadOnly(True)
        self._log_area.setFont(QFont("Consolas", 8))
        self._log_area.setStyleSheet("""
            QTextEdit {
                background: rgba(2, 6, 15, 230); color: #58A8C0;
                border: 1px solid rgba(0,160,200,60); border-radius: 8px;
                padding: 6px;
            }
            QScrollBar:vertical {
                background: rgba(0,10,20,80); width: 4px; border-radius: 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(0,200,255,100); border-radius: 2px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        hdr_row = QHBoxLayout()
        title = QLabel("● ISHA Communication Dock (UI only)")
        title.setFont(QFont("Segoe UI Semibold", 10))
        title.setStyleSheet("color: #00F0FF; background: transparent;")
        hdr_row.addWidget(title)
        hdr_row.addStretch()
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedSize(52, 22)
        clear_btn.setStyleSheet("""
            QPushButton { background: rgba(0,40,60,200); color: #00C8E8;
                border: 1px solid rgba(0,180,240,60); border-radius: 5px; font-size: 8pt; }
            QPushButton:hover { background: rgba(0,60,90,220); border: 1px solid rgb(0,240,255); color: #FFFFFF; }
            QPushButton:pressed { background: rgba(0,90,130,240); }
        """)
        clear_btn.clicked.connect(self._log_area.clear)
        hdr_row.addWidget(clear_btn)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setStyleSheet("""
            QPushButton { background: rgba(60,0,0,180); color: #FF4040;
                border: 1px solid rgba(255,60,60,60); border-radius: 5px; font-size: 9pt; }
            QPushButton:hover { background: rgba(100,0,0,220); border: 1px solid rgb(255,80,80); color: #FFFFFF; }
            QPushButton:pressed { background: rgba(150,0,0,240); }
        """)
        close_btn.clicked.connect(self.close)  # will trigger closeEvent
        hdr_row.addWidget(close_btn)
        root.addLayout(hdr_row)
        root.addWidget(self._log_area)
        self._log_area.append("=== ISHA v13 UI-only Demo ===")

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 14, 14)
        bg = QLinearGradient(0, 0, W, H)
        bg.setColorAt(0, QColor(3, 6, 16, 252))
        bg.setColorAt(1, QColor(6, 10, 24, 252))
        p.fillPath(path, QBrush(bg))
        p.setPen(QPen(QColor(0, 200, 255, 80), 1.5))
        p.setBrush(Qt.NoBrush); p.drawPath(path)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not _start_native_drag(self, e):
                self._drag_pos = e.globalPos() - self.frameGeometry().topLeft()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        # Fallback path only — see AboutISHAWindow's mousePressEvent comment.
        if e.buttons() == Qt.LeftButton and self._drag_pos:
            self.move(e.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None


class HistoryWindow(QWidget):
    """Day-by-day chat history viewer, with a 'Delete All History' option."""
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(560, 400)
        self._drag_pos = None
        self._build()
        self.refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        hdr_row = QHBoxLayout()
        title = QLabel("● ISHA Chat History")
        title.setFont(QFont("Segoe UI Semibold", 10))
        title.setStyleSheet("color: #00F0FF; background: transparent;")
        hdr_row.addWidget(title)
        hdr_row.addStretch()

        del_btn = QPushButton("Delete All History")
        del_btn.setFixedHeight(24)
        del_btn.setStyleSheet("""
            QPushButton { background: rgba(60,0,0,180); color: #FF6060;
                border: 1px solid rgba(255,60,60,70); border-radius: 6px;
                font-size: 8pt; padding: 0 10px; }
            QPushButton:hover { background: rgba(100,0,0,220); color: #FFFFFF; border: 1px solid rgb(255,80,80); }
            QPushButton:pressed { background: rgba(150,0,0,240); }
        """)
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(self._delete_all)
        hdr_row.addWidget(del_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setStyleSheet("""
            QPushButton { background: rgba(60,0,0,180); color: #FF4040;
                border: 1px solid rgba(255,60,60,60); border-radius: 5px; font-size: 9pt; }
            QPushButton:hover { background: rgba(100,0,0,220); border: 1px solid rgb(255,80,80); color: #FFFFFF; }
            QPushButton:pressed { background: rgba(150,0,0,240); }
        """)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        hdr_row.addWidget(close_btn)
        root.addLayout(hdr_row)

        body = QSplitter(Qt.Horizontal)
        body.setStyleSheet("QSplitter::handle { background: rgba(0,200,255,40); }")

        self._date_list = QListWidget()
        self._date_list.setFixedWidth(150)
        self._date_list.setStyleSheet("""
            QListWidget { background: rgba(2,6,15,230); border: 1px solid rgba(0,160,200,60);
                border-radius: 8px; color: #B0D8F0; font-size: 10px; outline: none; }
            QListWidget::item { padding: 6px 8px; border-radius: 5px; }
            QListWidget::item:hover { background: rgba(0,60,90,170); }
            QListWidget::item:selected { background: rgba(0,100,150,180); color: white; }
            QScrollBar:vertical { background: rgba(0,10,20,80); width: 4px; border-radius: 2px; }
            QScrollBar::handle:vertical { background: rgba(0,200,255,100); border-radius: 2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        self._date_list.itemClicked.connect(self._show_day)
        body.addWidget(self._date_list)

        self._day_view = QTextEdit()
        self._day_view.setReadOnly(True)
        self._day_view.setFont(QFont("Consolas", 8))
        self._day_view.setStyleSheet("""
            QTextEdit { background: rgba(2, 6, 15, 230); color: #58A8C0;
                border: 1px solid rgba(0,160,200,60); border-radius: 8px; padding: 6px; }
            QScrollBar:vertical { background: rgba(0,10,20,80); width: 4px; border-radius: 2px; }
            QScrollBar::handle:vertical { background: rgba(0,200,255,100); border-radius: 2px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        body.addWidget(self._day_view)
        body.setSizes([150, 400])
        root.addWidget(body)

    def refresh(self):
        """Reload dates + entries from disk."""
        self._date_list.clear()
        data = load_history()
        for date_key in sorted(data.keys(), reverse=True):
            self._date_list.addItem(date_key)
        self._day_view.clear()
        if self._date_list.count():
            self._date_list.setCurrentRow(0)
            self._show_day(self._date_list.currentItem())
        else:
            self._day_view.setPlainText("No chat history yet.")

    def _show_day(self, item):
        if item is None:
            return
        date_key = item.text()
        entries = load_history().get(date_key, [])
        self._day_view.clear()
        for entry in entries:
            who = "ISHA" if entry.get("role") == "isha" else "You"
            self._day_view.append(f"[{entry.get('time', '')}] {who}: {entry.get('text', '')}")

    def _delete_all(self):
        confirm = QMessageBox.question(
            self, "Delete All History",
            "Are you sure you want to permanently delete all chat history?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm == QMessageBox.Yes:
            delete_all_history()
            self.refresh()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 14, 14)
        bg = QLinearGradient(0, 0, W, H)
        bg.setColorAt(0, QColor(3, 6, 16, 252))
        bg.setColorAt(1, QColor(6, 10, 24, 252))
        p.fillPath(path, QBrush(bg))
        p.setPen(QPen(QColor(0, 200, 255, 80), 1.5))
        p.setBrush(Qt.NoBrush); p.drawPath(path)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not _start_native_drag(self, e):
                self._drag_pos = e.globalPos() - self.frameGeometry().topLeft()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        
        if e.buttons() == Qt.LeftButton and self._drag_pos:
            self.move(e.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None


# ------------------------------------------------
# Model / Voice configuration window (opened from Quick Settings -> Configurations)

_ARROW_CACHE: dict = {}


def _arrow_png(direction: str, color: str = "#7FE4FF", w: int = 9, h: int = 6) -> str:
    """Draw a small triangle to PNG and return a QSS-safe path, or "" on
    failure. Qt ignores the CSS `width:0;height:0` + borders triangle hack for
    ::up-arrow / ::down-arrow sub-controls and paints a filled block, so the
    arrows have to be real images."""
    key = (direction, color, w, h)
    if key in _ARROW_CACHE:
        return _ARROW_CACHE[key]
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        out = _RUNTIME_DIR / f"arrow_{direction}_{w}x{h}_{color.lstrip('#')}.png"
        if not out.exists():
            img = QImage(w, h, QImage.Format_ARGB32)
            img.fill(Qt.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing, True)
            path = QPainterPath()
            if direction == "up":
                path.moveTo(w / 2.0, 0.0)
                path.lineTo(float(w), float(h))
                path.lineTo(0.0, float(h))
            else:
                path.moveTo(0.0, 0.0)
                path.lineTo(float(w), 0.0)
                path.lineTo(w / 2.0, float(h))
            path.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(color)))
            p.drawPath(path)
            p.end()
            if not img.save(str(out), "PNG"):
                _ARROW_CACHE[key] = ""
                return ""
        # QSS needs forward slashes; quote it so spaces in the path survive.
        _ARROW_CACHE[key] = out.as_posix()
        return _ARROW_CACHE[key]
    except Exception:
        _ARROW_CACHE[key] = ""
        return ""


def _apply_arrow_icons(qss: str) -> str:
    """Swap the arrow placeholders for real image rules. If the PNGs could not
    be written we drop the rules entirely and let Qt draw its native arrows --
    lower contrast, but never a cyan block."""
    up   = _arrow_png("up")
    down = _arrow_png("down")
    big  = _arrow_png("down", w=11, h=7)
    if not (up and down and big):
        import re as _re
        return _re.sub(r"[^\n]*__ARROW_[A-Z_]+__[^\n]*\n", "", qss)
    return (qss.replace("__ARROW_UP__", up)
               .replace("__ARROW_DOWN_BIG__", big)
               .replace("__ARROW_DOWN__", down))


class ConfigWindow(QWidget):
    """Popup used to configure the local GGUF model + Edge-TTS voice.
    Settings are persisted to isha_config.json so they survive restarts."""
    closed = pyqtSignal()
    saved  = pyqtSignal(dict)

    _INPUT_QSS = """
        /* The scroll body is painted explicitly instead of being left
           transparent. Relying on the parent's paintEvent showing through a
           QScrollArea viewport is fragile - the viewport fills itself with the
           palette window colour on some styles, which is why the panel body
           came out pale grey once WA_TranslucentBackground was removed. A flat
           fill matching the window gradient's dark end is stable everywhere. */
        QWidget#cfgBody { background: #04081A; }
        QLabel  { background: transparent; }
        QFrame  { background: transparent; }

        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {
            background: rgba(0, 22, 34, 235);
            color: #CFEFFF;
            border: 1px solid rgba(0, 200, 255, 70);
            border-radius: 6px;
            padding: 4px 8px;
            font-size: 9pt;
            selection-background-color: rgba(0, 150, 200, 210);
            selection-color: #FFFFFF;
        }
        QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover,
        QComboBox:hover, QTextEdit:hover {
            border: 1px solid rgba(0, 220, 255, 130);
        }
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
        QComboBox:focus, QTextEdit:focus {
            border: 1px solid rgb(0, 225, 255);
            background: rgba(0, 30, 46, 245);
        }

        /* Spin buttons. Left unstyled these render as the platform's pale
           grey blocks, which is what those washed-out rectangles on the
           right edge of every numeric field were. */
        QSpinBox::up-button, QDoubleSpinBox::up-button,
        QSpinBox::down-button, QDoubleSpinBox::down-button {
            subcontrol-origin: border;
            width: 18px;
            background: rgba(0, 60, 90, 170);
            border-left: 1px solid rgba(0, 200, 255, 55);
        }
        QSpinBox::up-button, QDoubleSpinBox::up-button {
            subcontrol-position: top right;
            border-top-right-radius: 5px;
            border-bottom: 1px solid rgba(0, 200, 255, 40);
        }
        QSpinBox::down-button, QDoubleSpinBox::down-button {
            subcontrol-position: bottom right;
            border-bottom-right-radius: 5px;
        }
        QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
        QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
            background: rgba(0, 130, 180, 225);
        }
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
            image: url("__ARROW_UP__"); width: 9px; height: 6px;
        }
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
            image: url("__ARROW_DOWN__"); width: 9px; height: 6px;
        }
        QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
        QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {
            image: none;
        }

        QComboBox::drop-down {
            subcontrol-origin: border; subcontrol-position: center right;
            width: 22px; border: none; background: transparent;
        }
        QComboBox::down-arrow {
            image: url("__ARROW_DOWN_BIG__"); width: 11px; height: 7px;
            margin-right: 7px;
        }
        QComboBox QAbstractItemView {
            background: rgb(5, 12, 24); color: #CFEFFF;
            border: 1px solid rgba(0, 200, 255, 90);
            selection-background-color: rgba(0, 120, 175, 235);
            outline: none; padding: 2px;
        }

        QCheckBox {
            color: #B8DEF4; font-size: 9pt; background: transparent;
            spacing: 8px; padding: 2px 0;
        }
        QCheckBox::indicator {
            width: 14px; height: 14px; border-radius: 4px;
            border: 1px solid rgba(0, 200, 255, 90);
            background: rgba(0, 22, 34, 235);
        }
        QCheckBox::indicator:hover { border: 1px solid rgb(0, 225, 255); }
        QCheckBox::indicator:checked {
            background: rgb(0, 170, 225);
            border: 1px solid rgb(0, 235, 255);
        }

        QToolTip {
            background: rgb(6, 14, 26); color: #CFEFFF;
            border: 1px solid rgba(0, 200, 255, 110); padding: 4px 6px;
        }

        /* The System Prompt box has its own inner scrollbar. */
        QTextEdit QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
        QTextEdit QScrollBar::handle:vertical {
            background: rgba(0, 200, 255, 120); border-radius: 4px; min-height: 20px;
        }
        QTextEdit QScrollBar::add-line:vertical,
        QTextEdit QScrollBar::sub-line:vertical { height: 0; }
        QTextEdit QScrollBar::add-page:vertical,
        QTextEdit QScrollBar::sub-page:vertical { background: transparent; }
    """

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 420x580 left ~250px for every field and cut the last section in half.
        self.setFixedSize(470, 660)
        self._drag_pos = None
        self.cfg = dict(cfg)
        self._build()

    # ---- small layout helpers ----
    def _section(self, root, title):
        # `margin-top: 6px` in the stylesheet shrank the label's content rect
        # without growing its sizeHint, so the top of every heading was being
        # clipped. Vertical spacing belongs to the layout, not to QSS.
        root.addSpacing(10)
        lbl = QLabel(title)
        lbl.setFont(QFont("Segoe UI Semibold", 9))
        lbl.setStyleSheet("color: #00F0FF; background: transparent;")
        lbl.setMinimumHeight(19)
        root.addWidget(lbl)
        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(0,200,255,45); border: none;")
        root.addWidget(sep)
        root.addSpacing(4)

    def _row(self, root, label_text, widget, align_top: bool = False):
        row = QHBoxLayout()
        row.setSpacing(10)
        lbl = QLabel(label_text)
        lbl.setFont(QFont("Segoe UI", 9))
        lbl.setStyleSheet("color: #B0D8F0; background: transparent;")
        lbl.setFixedWidth(128)
        lbl.setWordWrap(True)
        if align_top:
            # A tall QTextEdit beside a vertically-centred caption looked
            # detached from its own label; pin the caption to the first line.
            lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            lbl.setContentsMargins(0, 5, 0, 0)
            row.addWidget(lbl, 0, Qt.AlignTop)
        else:
            lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            row.addWidget(lbl)
        # Uniform control height - mixed sizeHints were making the column of
        # fields look ragged.
        if not isinstance(widget, QTextEdit):
            widget.setMinimumHeight(27)
        row.addWidget(widget, 1)
        root.addLayout(row)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(16, 12, 12, 4)
        title = QLabel("● ISHA — Model Configurations")
        title.setFont(QFont("Segoe UI Semibold", 10))
        title.setStyleSheet("color: #00F0FF; background: transparent;")
        hdr_row.addWidget(title, 1)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setStyleSheet("""
            QPushButton { background: rgba(60,0,0,180); color: #FF4040;
                border: 1px solid rgba(255,60,60,70); border-radius: 6px;
                font-size: 9pt; font-weight: bold; }
            QPushButton:hover { background: rgba(100,0,0,220); color: #FFFFFF; border: 1px solid rgb(255,80,80); }
            QPushButton:pressed { background: rgba(150,0,0,240); }
        """)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        hdr_row.addWidget(close_btn)
        outer.addLayout(hdr_row)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical {
                background: rgba(0, 30, 45, 110);
                width: 8px; border-radius: 4px;
                /* Right inset keeps the bar off the window's rounded edge,
                   where it used to read as a stray cyan line. */
                margin: 3px 4px 3px 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(0, 210, 255, 150);
                border-radius: 4px; min-height: 28px;
            }
            QScrollBar::handle:vertical:hover { background: rgb(0, 230, 255); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0; border: none; background: transparent;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
        """)
        body = QWidget()
        # NO WA_TranslucentBackground here. This window is itself translucent
        # (for the rounded corners), and that attribute on a child of a
        # translucent top-level punches a real hole straight through to the
        # desktop instead of just letting the parent's paintEvent show. That
        # is why the wallpaper was visible behind every field, and why only
        # the scroll region was affected while the title bar and Save row -
        # which never had the attribute - stayed dark. A plain QWidget does
        # not paint a background at all, so the panel's own gradient shows
        # through correctly on its own.
        body.setObjectName("cfgBody")
        body.setStyleSheet(_apply_arrow_icons(self._INPUT_QSS))
        root = QVBoxLayout(body)
        root.setContentsMargins(18, 2, 14, 12); root.setSpacing(7)

        c = self.cfg

        # ---- Model & Connection ----
        self._section(root, "Model & Connection")
        self._f_model = QLineEdit(str(c.get("model", DEFAULT_GGUF_NAME)))
        self._row(root, "Model name", self._f_model)
        self._f_host = QLineEdit(str(c.get("gguf_model_path", "")))
        self._f_host.setPlaceholderText("models/your-model.gguf  (blank = auto-detect)")
        self._f_host.setToolTip(
            "Full path to a .gguf file, or a name inside the models/ folder.\n"
            "Blank chhod do toh ISHA models/ folder se pehli .gguf utha legi.")
        self._row(root, "GGUF model path", self._f_host)
        self._f_codepath = QLineEdit(str(c.get("code_model_path", "")))
        self._f_codepath.setPlaceholderText("optional second .gguf for coding tasks")
        self._row(root, "Coding GGUF (optional)", self._f_codepath)

        # ---- 1. Hardware & Speed ----
        self._section(root, "1. Hardware & Speed")
        self._f_gpu = QSpinBox(); self._f_gpu.setRange(0, 999)
        self._f_gpu.setValue(max(0, int(c.get("gpu_layers", 0))))
        self._f_gpu.setToolTip("0 = CPU only. N = itni layers GPU par offload hongi "
                               "(llama-cpp-python CUDA/Metal build chahiye).")
        self._row(root, "GPU Layers", self._f_gpu)

        self._f_threads = QSpinBox(); self._f_threads.setRange(0, 128)
        self._f_threads.setValue(int(c.get("cpu_threads", 0)))
        self._f_threads.setToolTip(f"0 = auto (is machine par {_auto_threads()}). "
                                   "Zyada threads = slower, kam mat samjho.")
        self._row(root, "CPU Threads", self._f_threads)

        self._f_batch = QSpinBox(); self._f_batch.setRange(1, 8192)
        self._f_batch.setValue(int(c.get("batch_size", 512)))
        self._row(root, "Batch Size", self._f_batch)

        # ---- 2. Memory & Chatting ----
        self._section(root, "2. Memory & Chatting")
        self._f_ctx = QSpinBox(); self._f_ctx.setRange(256, 131072); self._f_ctx.setSingleStep(256)
        self._f_ctx.setValue(int(c.get("context_length", 4096)))
        self._row(root, "Context Length", self._f_ctx)

        self._f_sysprompt = QTextEdit(str(c.get("system_prompt", "")))
        # 56px showed barely two wrapped lines of a paragraph-long prompt.
        self._f_sysprompt.setFixedHeight(92)
        self._f_sysprompt.setLineWrapMode(QTextEdit.WidgetWidth)
        self._f_sysprompt.setTabChangesFocus(True)
        self._row(root, "System Prompt", self._f_sysprompt, align_top=True)

        stop_val = c.get("stop_tokens", [])
        stop_str = ", ".join(stop_val) if isinstance(stop_val, list) else str(stop_val)
        self._f_stop = QLineEdit(stop_str)
        self._f_stop.setPlaceholderText("comma se alag karo, e.g. </s>, User:")
        self._row(root, "Stop Tokens", self._f_stop)

        # ---- 3. Creativity & sampling ----
        self._section(root, "3. Creativity & Sampling")
        self._f_temp = QDoubleSpinBox(); self._f_temp.setRange(0.0, 2.0); self._f_temp.setSingleStep(0.05)
        self._f_temp.setValue(float(c.get("temperature", 0.8)))
        self._row(root, "Temperature", self._f_temp)

        self._f_topp = QDoubleSpinBox(); self._f_topp.setRange(0.0, 1.0); self._f_topp.setSingleStep(0.05)
        self._f_topp.setValue(float(c.get("top_p", 0.9)))
        self._row(root, "Top P", self._f_topp)

        self._f_topk = QSpinBox(); self._f_topk.setRange(0, 500)
        self._f_topk.setValue(int(c.get("top_k", 40)))
        self._row(root, "Top K", self._f_topk)

        self._f_minp = QDoubleSpinBox(); self._f_minp.setRange(0.0, 1.0); self._f_minp.setSingleStep(0.01)
        self._f_minp.setValue(float(c.get("min_p", 0.0)))
        self._row(root, "Min P", self._f_minp)

        self._f_repeat = QDoubleSpinBox(); self._f_repeat.setRange(0.0, 3.0); self._f_repeat.setSingleStep(0.05)
        self._f_repeat.setValue(float(c.get("repeat_penalty", 1.1)))
        self._row(root, "Repeat Penalty", self._f_repeat)

        self._f_presence = QDoubleSpinBox(); self._f_presence.setRange(-2.0, 2.0); self._f_presence.setSingleStep(0.1)
        self._f_presence.setValue(float(c.get("presence_penalty", 0.0)))
        self._row(root, "Presence Penalty", self._f_presence)

        self._f_frequency = QDoubleSpinBox(); self._f_frequency.setRange(-2.0, 2.0); self._f_frequency.setSingleStep(0.1)
        self._f_frequency.setValue(float(c.get("frequency_penalty", 0.0)))
        self._row(root, "Frequency Penalty", self._f_frequency)

        # ---- 4. Output & Format ----
        self._section(root, "4. Output & Format")
        self._f_json = QCheckBox("JSON Mode (sirf JSON reply)")
        self._f_json.setChecked(bool(c.get("json_mode", False)))
        root.addWidget(self._f_json)

        self._f_stream = QCheckBox("Streaming (word-by-word)")
        self._f_stream.setChecked(bool(c.get("streaming", True)))
        root.addWidget(self._f_stream)

        self._f_thinking = QCheckBox("Thinking Mode (reasoning models)")
        self._f_thinking.setChecked(bool(c.get("thinking_mode", False)))
        root.addWidget(self._f_thinking)

        self._f_maxtok = QSpinBox(); self._f_maxtok.setRange(16, 8192)
        self._f_maxtok.setSingleStep(64)
        self._f_maxtok.setValue(int(c.get("max_tokens", 512)))
        self._f_maxtok.setToolTip("num_predict - reply ki max length. Chhota = fast.")
        self._row(root, "Max Tokens", self._f_maxtok)

        self._f_seed = QSpinBox(); self._f_seed.setRange(-1, 2_147_483_647)
        self._f_seed.setValue(int(c.get("seed", -1)))
        self._f_seed.setToolTip("-1 = random, fixed number = same output har baar")
        self._row(root, "Seed", self._f_seed)

        # ---- 5. Speed ----
        self._section(root, "5. Speed")
        self._f_fasttools = QCheckBox("Fast tools (command ke baad narration skip)")
        self._f_fasttools.setChecked(bool(c.get("fast_tools", True)))
        root.addWidget(self._f_fasttools)

        self._f_keepalive = QLineEdit(str(c.get("chat_format", "")))
        self._f_keepalive.setPlaceholderText("blank = GGUF ka apna template")
        self._f_keepalive.setToolTip(
            "Chat template override. Blank chhodo jab tak model galat format na de.\n"
            "Common: llama-3, chatml, mistral-instruct, gemma, zephyr.")
        self._row(root, "Chat format", self._f_keepalive)

        self._f_histturns = QSpinBox(); self._f_histturns.setRange(4, 60)
        self._f_histturns.setValue(int(c.get("history_turns", 12)))
        self._f_histturns.setToolTip("Model ko bheji jaane wali history. Kam = fast.")
        self._row(root, "History Turns", self._f_histturns)

        # ---- Voice (Edge-TTS) ----
        self._section(root, "Voice (Edge-TTS)")
        self._f_tts_on = QCheckBox("Har reply bolke sunao")
        self._f_tts_on.setChecked(bool(c.get("tts_enabled", True)))
        root.addWidget(self._f_tts_on)

        self._f_voice = QComboBox()
        self._voice_options = [
            ("en-US-AriaNeural",   "English (US) — Aria, female"),
            ("en-US-JennyNeural",  "English (US) — Jenny, female"),
            ("en-GB-SoniaNeural",  "English (UK) — Sonia, female"),
            ("en-IN-NeerjaNeural", "English (India) — Neerja, female"),
            ("hi-IN-SwaraNeural",  "Hindi — Swara, female"),
        ]
        for tag, label in self._voice_options:
            self._f_voice.addItem(label, tag)
        cur_voice = c.get("tts_voice", "en-US-AriaNeural")
        idx = next((i for i, (tag, _) in enumerate(self._voice_options) if tag == cur_voice), 0)
        self._f_voice.setCurrentIndex(idx)
        self._row(root, "Voice", self._f_voice)

        root.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # ---- Save button ----
        # The last section used to run straight into the button with nothing
        # between them, so it read as clipped rather than scrollable.
        foot_sep = QFrame(); foot_sep.setFixedHeight(1)
        foot_sep.setStyleSheet("background: rgba(0,200,255,55); border: none;")
        outer.addWidget(foot_sep)

        save_row = QHBoxLayout()
        save_row.setContentsMargins(16, 10, 16, 14)
        self._save_btn = QPushButton("💾  Save Settings")
        self._save_btn.setFixedHeight(34)
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.setStyleSheet("""
            QPushButton { background: rgba(0,80,120,220); color: #E8FBFF;
                border: 1px solid rgba(0,220,255,140); border-radius: 8px;
                font-size: 10pt; font-weight: 600; }
            QPushButton:hover { background: rgba(0,110,160,240); border: 1px solid rgb(0,240,255); }
            QPushButton:pressed { background: rgba(0,140,200,255); }
        """)
        self._save_btn.clicked.connect(self._on_save)
        save_row.addWidget(self._save_btn)
        outer.addLayout(save_row)

    def _on_save(self):
        stop_text = self._f_stop.text().strip()
        stop_list = [s.strip() for s in stop_text.split(",") if s.strip()] if stop_text else []
        new_cfg = dict(self.cfg)
        new_cfg.update({
            "model":             self._f_model.text().strip() or DEFAULT_GGUF_NAME,
            "gguf_model_path":   self._f_host.text().strip(),
            "code_model_path":   self._f_codepath.text().strip(),
            "gpu_layers":        self._f_gpu.value(),
            "cpu_threads":       self._f_threads.value(),
            "batch_size":        self._f_batch.value(),
            "context_length":    self._f_ctx.value(),
            "system_prompt":     self._f_sysprompt.toPlainText(),
            "stop_tokens":       stop_list,
            "temperature":       self._f_temp.value(),
            "top_p":             self._f_topp.value(),
            "top_k":             self._f_topk.value(),
            "min_p":             self._f_minp.value(),
            "repeat_penalty":    self._f_repeat.value(),
            "presence_penalty":  self._f_presence.value(),
            "frequency_penalty": self._f_frequency.value(),
            "json_mode":         self._f_json.isChecked(),
            "streaming":         self._f_stream.isChecked(),
            "thinking_mode":     self._f_thinking.isChecked(),
            "seed":              self._f_seed.value(),
            "max_tokens":        self._f_maxtok.value(),
            "fast_tools":        self._f_fasttools.isChecked(),
            "chat_format":       self._f_keepalive.text().strip(),
            "history_turns":     self._f_histturns.value(),
            "tts_enabled":       self._f_tts_on.isChecked(),
            "tts_voice":         self._f_voice.currentData(),
        })
        self.cfg = new_cfg
        self.saved.emit(new_cfg)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 14, 14)
        bg = QLinearGradient(0, 0, W, H)
        bg.setColorAt(0, QColor(3, 6, 16, 252))
        bg.setColorAt(1, QColor(6, 10, 24, 252))
        p.fillPath(path, QBrush(bg))
        p.setPen(QPen(QColor(0, 200, 255, 80), 1.5))
        p.setBrush(Qt.NoBrush); p.drawPath(path)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not _start_native_drag(self, e):
                self._drag_pos = e.globalPos() - self.frameGeometry().topLeft()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        # Fallback path only — see AboutISHAWindow's mousePressEvent comment.
        if e.buttons() == Qt.LeftButton and self._drag_pos:
            self.move(e.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None


# ------------------------------------------------

class HandPreviewWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(260, 200)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self._label = QLabel("Starting camera…")
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet(
            "background: rgba(7,10,20,230); color:#00E0FF; border:1px solid rgba(0,200,255,90); border-radius:10px;")
        self._label.setFixedSize(248, 188)
        lay.addWidget(self._label)

    def update_frame(self, qimg: QImage):
        pix = QPixmap.fromImage(qimg).scaled(
            self._label.width(), self._label.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._label.setPixmap(pix)

    def show_message(self, text: str):
        self._label.setText(text)


# ------------------------------------------------
# Main Window (UI only)
class _AuditBridge(QObject):
    """ActionLog listeners fire on worker threads; this hops them onto the
    UI thread through a queued Qt signal."""
    entry = pyqtSignal(object)


class AgentPanelWindow(QWidget):
    """ISHA Agent Center: model/mode/autonomy controls, live status,
    STOP, file-index controls and the activity log."""

    closed = pyqtSignal()
    _MODES = [("Auto", "auto"), ("General", "general"), ("Coding", "coding"), ("Reasoning", "reasoning"),
              ("Study", "study"), ("Fast", "fast"), ("Vision", "vision")]
    _AUTONOMY = [("Low - ask before most actions", "low"), ("Medium - read-only automatic", "medium"),
                 ("High - only risky actions ask", "high")]

    def __init__(self, main_window, parent=None):
        super().__init__(parent, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.main_window = main_window
        self.setWindowTitle("ISHA — Agent Center")
        self.resize(640, 620)
        from isha_core import theme as _theme
        self.setStyleSheet(_theme.QSS + """
            QWidget { background: #070a14; color: #c8e6ff; font-family: 'Segoe UI'; font-size: 10pt; }
            QLabel#h { color: #00F0FF; font-size: 13pt; font-weight: 600; }
            QLabel#k { color: #5f8fb0; }
            QComboBox, QListWidget, QTextEdit { background: #0d1424; border: 1px solid #12405a;
                border-radius: 6px; padding: 4px; }
            QPushButton { background: #0f2a3d; border: 1px solid #1b6a8a; border-radius: 6px; padding: 6px 12px; }
            QPushButton:hover { background: #154060; }
            QPushButton#stop { background: #5a0d1e; border-color: #ff2d6f; color: #ffd0dc; font-weight: 700; }
            QPushButton#stop:hover { background: #7a1028; }
        """)
        self._bridge = _AuditBridge()
        self._bridge.entry.connect(self._on_entry)
        self._listener = lambda e: self._bridge.entry.emit(e)
        ACTION_LOG.subscribe(self._listener)
        self._build()
        for e in ACTION_LOG.recent(80):
            self._on_entry(e)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1500)
        self.refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        top = QHBoxLayout()
        h = QLabel("ISHA Agent Center"); h.setObjectName("h")
        top.addWidget(h); top.addStretch()
        self.stop_btn = QPushButton("■  STOP"); self.stop_btn.setObjectName("stop")
        self.stop_btn.setToolTip("Stop the agent loop, generation, queued tools and running code (Esc)")
        self.stop_btn.clicked.connect(self._stop)
        top.addWidget(self.stop_btn)
        root.addLayout(top)

        grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(5)
        self.mode_cb = QComboBox()
        for label, val in self._MODES:
            self.mode_cb.addItem(label, val)
        self.auto_cb = QComboBox()
        for label, val in self._AUTONOMY:
            self.auto_cb.addItem(label, val)
        cfg = self.main_window.cfg
        self.mode_cb.setCurrentIndex(max(0, [v for _, v in self._MODES].index(cfg.get("model_mode", "auto"))
                                         if cfg.get("model_mode", "auto") in [v for _, v in self._MODES] else 0))
        au = isha_perm.normalise_autonomy(cfg.get("autonomy_level"))
        self.auto_cb.setCurrentIndex([v for _, v in self._AUTONOMY].index(au))
        self.mode_cb.currentIndexChanged.connect(self._mode_changed)
        self.auto_cb.currentIndexChanged.connect(self._autonomy_changed)
        self.labels = {}
        rows = [("Model mode", self.mode_cb), ("Autonomy", self.auto_cb)]
        for key in ("Backend", "Active model", "Loaded", "Current task", "Current tool", "Agent step",
                    "CPU / RAM", "GPU", "Voice / Mic", "Tools", "File index"):
            lbl = QLabel("-"); lbl.setWordWrap(True)
            self.labels[key] = lbl
            rows.append((key, lbl))
        for i, (k, w) in enumerate(rows):
            kl = QLabel(k); kl.setObjectName("k")
            grid.addWidget(kl, i, 0, Qt.AlignTop)
            grid.addWidget(w, i, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        btns = QHBoxLayout()
        for text, fn in (("Rebuild index", self._rebuild), ("Pause/Resume index", self._pause),
                         ("Self-check", self._selfcheck), ("Models", self._models)):
            b = QPushButton(text); b.clicked.connect(fn); btns.addWidget(b)
        root.addLayout(btns)

        al = QLabel("Activity"); al.setObjectName("k")
        root.addWidget(al)
        self.activity = QListWidget()
        root.addWidget(self.activity, 1)
        self.detail = QTextEdit(); self.detail.setReadOnly(True); self.detail.setFixedHeight(110)
        root.addWidget(self.detail)

    # ---- controls ----
    def _stop(self):
        self.main_window._stop_generation()

    def _mode_changed(self, _i):
        val = self.mode_cb.currentData()
        self.main_window.cfg["model_mode"] = val
        save_config(self.main_window.cfg)
        ACTION_LOG.record("model_mode", mode=val, source="gui")

    def _autonomy_changed(self, _i):
        val = self.auto_cb.currentData()
        self.main_window.cfg["autonomy_level"] = val
        save_config(self.main_window.cfg)
        ACTION_LOG.record("autonomy", level=val, source="gui")

    def _bg(self, fn):
        def run():
            try:
                out = fn()
            except Exception as e:  # noqa: BLE001
                out = f"ERROR: {e}"
            self._bridge.entry.emit({"kind": "_detail", "text": str(out)})
        threading.Thread(target=run, daemon=True).start()

    def _rebuild(self):
        self._bg(lambda: _rebuild_file_index({}))

    def _pause(self):
        idx = get_file_index()
        if idx is None:
            self.detail.setPlainText("File index disabled.")
            return
        self._bg(lambda: _pause_file_index({"pause": not idx.status()["paused"]}))

    def _selfcheck(self):
        self._bg(lambda: format_self_diagnostics(run_self_diagnostics(self.main_window.cfg)))

    def _models(self):
        self._bg(lambda: _get_model_status({}))

    # ---- updates ----
    def _on_entry(self, e: dict):
        if e.get("kind") == "_detail":
            self.detail.setPlainText(e.get("text", ""))
            return
        if e.get("kind") == "status":
            return
        t = str(e.get("time", ""))[-8:]
        k = e.get("kind")
        if k == "tool":
            mark = "✔" if e.get("success") else "✖"
            text = (f"{t}  {mark} {e.get('tool')}  [{e.get('risk')}/{e.get('approval')}]  "
                    f"{e.get('duration_ms', 0)} ms  {str(e.get('result', ''))[:70]}")
        elif k == "route":
            text = f"{t}  ⇢ route {e.get('task')} → {e.get('model')}"
        elif k == "approval":
            text = f"{t}  ? approval {e.get('tool')}: {e.get('approval')}"
        elif k == "request":
            text = f"{t}  ▶ {str(e.get('text', ''))[:80]}"
        else:
            text = f"{t}  {k}: " + ", ".join(f"{a}={b}" for a, b in e.items()
                                             if a not in ("time", "ts", "kind"))[:90]
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, e)
        self.activity.addItem(item)
        while self.activity.count() > 300:
            self.activity.takeItem(0)
        self.activity.scrollToBottom()
        try:
            self.activity.itemClicked.disconnect()
        except Exception:
            pass
        self.activity.itemClicked.connect(
            lambda it: self.detail.setPlainText(json.dumps(it.data(Qt.UserRole), ensure_ascii=False,
                                                           indent=2, default=str)))

    def refresh(self):
        cur = ACTION_LOG.current
        s = getattr(self.main_window, "_latest_stats", {}) or {}
        L = self.labels
        L["Backend"].setText(f"llama.cpp ({_LLAMA_MODE})" + ("" if _LLAMA_CPP_OK else " — UNAVAILABLE"))
        L["Active model"].setText(f"{ACTIVE_MODEL.get('name') or '-'}  ({ACTIVE_MODEL.get('role') or '-'})"
                                  + (f" — {ACTIVE_MODEL['notes']}" if ACTIVE_MODEL.get("notes") else ""))
        L["Loaded"].setText(", ".join(GGUF.loaded_names()) or "none")
        L["Current task"].setText(cur.get("task") or "-")
        L["Current tool"].setText(cur.get("tool") or "-")
        L["Agent step"].setText(cur.get("step") or "-")
        cpu, ram = s.get("cpu_percent"), s.get("ram_percent")
        L["CPU / RAM"].setText(f"{cpu:.0f}% / {ram:.0f}%" if cpu is not None and ram is not None else "-")
        hw = _HW_CACHE.get("info")
        L["GPU"].setText(", ".join(g.name for g in hw.gpus) if hw and hw.gpus else "none detected")
        L["Voice / Mic"].setText(f"TTS {'on' if _TTS_OK else 'off'} / mic {'on' if _STT_OK else 'off'}")
        L["Tools"].setText(f"{len(TOOL_REGISTRY)} registered")
        idx = get_file_index(create=False)
        if idx is None:
            L["File index"].setText("not started" if self.main_window.cfg.get("file_index_enabled", True) else "disabled")
        else:
            st = idx.status()
            L["File index"].setText(f"{st['state']}{' (paused)' if st['paused'] else ''}, {st['entries']} entries, "
                                    f"last: {st['last_complete'] or 'never'}")
        self.stop_btn.setEnabled(bool(self.main_window._is_busy()))

    def closeEvent(self, event):
        self.closed.emit()
        event.ignore()
        self.hide()

    def shutdown(self):
        ACTION_LOG.unsubscribe(self._listener)
        self._timer.stop()


class ISHAWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ISHA")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(760, 82)
        self._centre()
        self._drag_pos   = None
        self._mic_on     = False
        self._chat_vis   = False
        self._qs_popup   = None
        self._comm_dock  = None          
        self._history_win = None         
        self._config_win  = None         
        self._agent_panel = None
        self._busy        = False
        self.cfg          = load_config()
        if not bool(self.cfg.get("audit_log_enabled", True)):
            ACTION_LOG.path = None          # keep the in-memory log only
        self._llm_worker  = None
        # --- streaming response pipeline ---
        self._sent_streamer = SentenceStreamer()   # tokens -> speakable sentences
        self._stream_spoke  = False                # did TTS already say this reply?
        self._suppress_speak = False
        self._router_worker = None       
        self._agent_worker  = None       
        self._conv_history = []          
        self._os_worker    = None        
        self._latest_stats = {}          

        self.os_controller = None
        self.stats_monitor  = None
        if _OS_CONTROL_OK:
            self.os_controller = WindowsController()
            self.stats_monitor = SystemStatsMonitor(self.os_controller, interval=2.0)
            self.stats_monitor.stats_updated.connect(self._on_stats_updated)
            self.stats_monitor.error_occurred.connect(lambda m: print(f"[Stats] {m}"))
            self.stats_monitor.start()

        self.hand_worker      = None
        self._hand_preview_win = None
        self._tts_worker  = None
        self._tts_busy    = False
        self._tts_queue   = []
        self._mic_thread  = None
        self._stars  = [Star(760, 82) for _ in range(72)]
        # Cyberpunk glow-border effect: `_glow_phase` drives a slow idle pulse,
        # `_glow_boost` (0..1) is added on top while ISHA is actively listening,
        # thinking, or hand-control is on, so the glow visibly reacts to state
        # instead of just looping the same animation regardless of what's happening.
        self._glow_phase = 0.0
        self._glow_boost = 0.0
        self._hand_active = False
        self._ptimer = QTimer(self)
        self._ptimer.timeout.connect(self._tick)
        self._ptimer.start(15)
        self._build_ui()
        # Fade-in
        self.setWindowOpacity(0.0)
        fa = QPropertyAnimation(self, b"windowOpacity")
        fa.setDuration(650); fa.setStartValue(0.0); fa.setEndValue(1.0)
        fa.setEasingCurve(QEasingCurve.OutCubic); fa.start()
        self._fa = fa
        # Greet
        QTimer.singleShot(900, self._greet)
        # Load the model into RAM while the user is still reading the greeting,
        # so the first real message is not the one that pays for it.
        QTimer.singleShot(300, self._warm_up_model)
        QTimer.singleShot(1500, self._start_file_index)
        if bool(self.cfg.get("show_agent_panel", False)):
            QTimer.singleShot(1200, lambda: self.toggle_agent_panel(True))

    def _start_file_index(self):
        """Local file index: built in the background after a delay so it never
        competes with model loading; incremental on later launches."""
        if not bool(self.cfg.get("file_index_enabled", True)):
            return
        idx = get_file_index()
        if idx is not None:
            idx.start_background(full=False, delay_s=float(self.cfg.get("file_index_autostart_delay", 20)))

    def _build_ui(self):
        root = QWidget(self)
        root.setAttribute(Qt.WA_TranslucentBackground)
        self.setCentralWidget(root)
        ml = QVBoxLayout(root)
        ml.setContentsMargins(0, 0, 0, 0); ml.setSpacing(0)
        upper = QWidget(root)
        upper.setAttribute(Qt.WA_TranslucentBackground)
        upper.setFixedHeight(0)
        ul = QVBoxLayout(upper)
        ul.setContentsMargins(16, 0, 16, 6); ul.setSpacing(6)
        ul.addStretch(); ml.addWidget(upper)
        bar = QWidget(root)
        bar.setAttribute(Qt.WA_TranslucentBackground); bar.setFixedHeight(82)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(18, 11, 18, 11); bl.setSpacing(10)
        self.mic_btn      = MicButton(bar)
        self.search_bar   = SearchBar(bar)
        self.wave_bar     = WaveformBar(bar)
        self.dots         = ThinkingDots(bar)
        self.settings_btn = SettingsButton(bar)
        self.mic_btn.clicked.connect(self._toggle_mic)
        self.search_bar.submitted.connect(self._on_typed)
        self.settings_btn.clicked.connect(self._toggle_qs)
        bl.addWidget(self.mic_btn); bl.addWidget(self.search_bar)
        # Always-reachable STOP (spec: "visible Stop button"). Shown while
        # ISHA is working; Esc does the same.
        self.stop_btn = QPushButton("■", bar)
        self.stop_btn.setFixedSize(34, 34)
        self.stop_btn.setToolTip("Stop (Esc) — agent, generation, tools, running code")
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.setStyleSheet(
            "QPushButton { background: rgba(255,0,90,60); color: #ffd0dc; border: 1px solid #ff2d6f;"
            " border-radius: 17px; font-size: 13pt; } QPushButton:hover { background: rgba(255,0,90,120); }")
        self.stop_btn.clicked.connect(self._stop_generation)
        self.stop_btn.hide()
        bl.addWidget(self.wave_bar); bl.addWidget(self.dots)
        bl.addWidget(self.stop_btn)
        bl.addWidget(self.settings_btn)
        ml.addWidget(bar)

        self._bubble = BubbleLabel(self)
        self._chat   = ChatPanel(self); self._chat.hide()
        # Removed separate CommDock button; now it's inside Quick Settings
        self._gesture_lbl = QLabel("ak", self)
        self._gesture_lbl.setFont(QFont("Segoe UI", 7))
        self._gesture_lbl.setStyleSheet(
            "color: rgba(0,200,255,160); background: transparent; padding: 1px 4px;"
        )
        self._gesture_lbl.setFixedWidth(160)
        self._gesture_lbl.move(8, self.height() - 18)
        self._gesture_lbl.show()
        self._gesture_lbl_timer = QTimer(self)
        self._gesture_lbl_timer.setSingleShot(True)
        self._gesture_lbl_timer.timeout.connect(
            lambda: self._gesture_lbl.setText("Gesture: Idle"))

    # ---- Communication Dock toggling ----
    def toggle_comm_dock(self, on: bool):
        if on:
            if not self._comm_dock:
                self._comm_dock = CommDockWindow()
                self._comm_dock.closed.connect(self._on_comm_dock_closed)
                sc = QApplication.primaryScreen().geometry()
                self._comm_dock.move(
                    sc.width()  - self._comm_dock.width()  - 20,
                    sc.height() - self._comm_dock.height() - 80,
                )
            self._comm_dock.show()
            self._comm_dock.raise_()
            # sync toggle state in QuickSettings if open
            if self._qs_popup and self._qs_popup.isVisible():
                self._qs_popup._t_comm_dock.set_on(True)
        else:
            if self._comm_dock:
                self._comm_dock.hide()
            # sync toggle state in QuickSettings if open
            if self._qs_popup and self._qs_popup.isVisible():
                self._qs_popup._t_comm_dock.set_on(False)

    def _on_comm_dock_closed(self):
        # user closed the CommDock window via X button
        if self._qs_popup and self._qs_popup.isVisible():
            self._qs_popup._t_comm_dock.set_on(False)
        if self._comm_dock:
            self._comm_dock.hide()

    # ---- History window toggling ----
    # ---- busy state / agent center ----
    def _is_busy(self) -> bool:
        return any(getattr(self, a, None) is not None for a in
                   ("_llm_worker", "_router_worker", "_agent_worker", "_os_worker")) or self._busy

    def _set_busy(self, on: bool):
        self._busy = bool(on)
        try:
            self.stop_btn.setVisible(bool(on))
        except Exception:
            pass
        if not on:
            ACTION_LOG.set_current(tool="", step="idle")

    def toggle_agent_panel(self, on: bool):
        if on:
            if self._agent_panel is None:
                self._agent_panel = AgentPanelWindow(self)
                self._agent_panel.closed.connect(lambda: self.toggle_agent_panel(False))
            self._agent_panel.show()
            self._agent_panel.raise_()
        elif self._agent_panel is not None:
            self._agent_panel.hide()
        if self._qs_popup is not None and hasattr(self._qs_popup, "_t_agent"):
            self._qs_popup._t_agent.set_on(bool(on))

    def toggle_history_window(self, on: bool):
        if on:
            if not self._history_win:
                self._history_win = HistoryWindow()
                self._history_win.closed.connect(self._on_history_closed)
            else:
                self._history_win.refresh()
            sc = QApplication.primaryScreen().geometry()
            self._history_win.move(
                (sc.width()  - self._history_win.width())  // 2,
                (sc.height() - self._history_win.height()) // 2 - 40,
            )
            self._history_win.show()
            self._history_win.raise_()
            if self._qs_popup and self._qs_popup.isVisible():
                self._qs_popup._t_chat_history.set_on(True)
        else:
            if self._history_win:
                self._history_win.hide()
            if self._qs_popup and self._qs_popup.isVisible():
                self._qs_popup._t_chat_history.set_on(False)

    def _on_history_closed(self):
        # user closed the History window via X button
        if self._qs_popup and self._qs_popup.isVisible():
            self._qs_popup._t_chat_history.set_on(False)
        if self._history_win:
            self._history_win.hide()

    # ---- Model/Voice configuration window toggling ----
    def toggle_config_dialog(self, on: bool):
        if on:
            if not self._config_win:
                self._config_win = ConfigWindow(self.cfg, self)
                self._config_win.saved.connect(self._on_config_saved)
                self._config_win.closed.connect(self._on_config_dialog_closed)
            sc = QApplication.primaryScreen().geometry()
            self._config_win.move(
                (sc.width()  - self._config_win.width())  // 2,
                (sc.height() - self._config_win.height()) // 2,
            )
            self._config_win.show()
            self._config_win.raise_()
            if self._qs_popup and self._qs_popup.isVisible():
                self._qs_popup._t_model_settings.set_on(True)
        else:
            if self._config_win:
                self._config_win.hide()
            if self._qs_popup and self._qs_popup.isVisible():
                self._qs_popup._t_model_settings.set_on(False)

    def _on_config_dialog_closed(self):
        
        if self._qs_popup and self._qs_popup.isVisible():
            self._qs_popup._t_model_settings.set_on(False)
        if self._config_win:
            self._config_win.hide()

    def _on_config_saved(self, new_cfg: dict):
        self.cfg = new_cfg
        save_config(self.cfg)
        self.respond("Settings saved. Naya configuration ab se use hoga.", "isha")

    # ---- Hand-gesture control (webcam + MediaPipe, see isha_hand_control.py) ----
    def toggle_hand_control(self, on: bool):
        if on:
            if not hand_control_available():
                if not _PYAUTOGUI_OK:
                    msg = "Hand control ke liye install karo: pip install pyautogui"
                else:
                    msg = _hand_env_help()
                self.respond(msg, "isha")
                if self._qs_popup and self._qs_popup.isVisible():
                    self._qs_popup._t_hand_control.set_on(False)
                return
            if self.hand_worker is not None:
                return  # already running
            # Picks the in-process worker when MediaPipe imports here, and the
            # Python 3.10-3.12 sidecar when it doesn't.
            self.hand_worker = make_hand_worker(
                camera_index=0, emit_preview=True,
                scroll_enabled=bool(self.cfg.get("hand_scroll_enabled", False)))
            self.hand_worker.gesture_action.connect(self._on_gesture_action)
            self.hand_worker.status_changed.connect(self._on_hand_status)
            self.hand_worker.error_occurred.connect(self._on_hand_error)
            self.hand_worker.frame_ready.connect(self._on_hand_frame)
            self.hand_worker.finished.connect(self._on_hand_worker_finished)
            if self._hand_preview_win is None:
                self._hand_preview_win = HandPreviewWindow(self)
            sc = QApplication.primaryScreen().geometry()
            self._hand_preview_win.move(sc.width() - 280, 40)
            _backend = hand_backend_name()
            if _backend == "sidecar":
                _msg = "Hand env taiyaar ho raha hai…\n(pehli baar 2-4 min)"
            elif _backend == "tasks" and not hand_model_present():
                _msg = "Hand model download ho raha hai…\n(~7 MB, ek hi baar)"
            else:
                _msg = "Starting camera…"
            print(f"[Hand] backend: {_backend}")
            self._hand_preview_win.show_message(_msg)
            self._hand_preview_win.show()
            self.hand_worker.start()
            self._hand_active = True
            self._update_glow_boost()
            if self._qs_popup and self._qs_popup.isVisible():
                self._qs_popup._t_hand_control.set_on(True)
        else:
            if self.hand_worker is not None:
                self.hand_worker.stop()
                self.hand_worker.wait(1500)
                self.hand_worker = None
            if self._hand_preview_win is not None:
                self._hand_preview_win.hide()
            self._hand_active = False
            self._update_glow_boost()
            self.respond("Hand control off.", "isha")
            if self._qs_popup and self._qs_popup.isVisible():
                self._qs_popup._t_hand_control.set_on(False)

    def _on_hand_worker_finished(self):
        # Worker thread ended on its own (e.g. camera error) -> reflect it in the UI
        self.hand_worker = None
        self._hand_active = False
        self._update_glow_boost()
        if self._hand_preview_win is not None:
            self._hand_preview_win.hide()
        if self._qs_popup and self._qs_popup.isVisible():
            self._qs_popup._t_hand_control.set_on(False)

    def _on_hand_status(self, msg: str):
        self.respond(msg, "isha")

    def _on_hand_error(self, msg: str):
        self.respond(f"Hand control: {msg}", "isha")
        if self._hand_preview_win is not None:
            self._hand_preview_win.show_message(msg)

    def _on_hand_frame(self, qimg: QImage):
        if self._hand_preview_win is not None and self._hand_preview_win.isVisible():
            self._hand_preview_win.update_frame(qimg)

    def _on_gesture_action(self, action: str, payload: dict):
        """Runs on the UI thread (Qt queues cross-thread signals automatically).
        This is the ONLY place gesture output turns into real OS changes —
        the worker thread itself never touches pyautogui/win32 directly."""
        if action == "mouse_move":
            hand_move_mouse(float(payload.get("fx", 0.5)), float(payload.get("fy", 0.5)))

        elif action == "left_click":
            hand_left_click()

        elif action == "right_click":
            hand_right_click()

        elif action == "scroll":
            hand_scroll(payload.get("direction", "up"))

    # ------------------------------------------------
    def respond(self, text: str, role: str = "isha"):
        if not text: return
        print(f"[{role}] {text}")
        self._place_bubble(text)
        self._bubble.show_text(text, role)
        self._chat.add_entry(text, role)
        log_chat_entry(role, text)                     # save into day-by-day history file
        if self._comm_dock and self._comm_dock.isVisible():
            # Comm Dock only ever shows messages exchanged while it's open (live only)
            who = "ISHA" if role == "isha" else "You"
            ts  = datetime.now().strftime("%I:%M %p")
            self._comm_dock._log_area.append(f"[{ts}] {who}: {text}")
        if role == "isha":
            short = text[:46] + ("…" if len(text) > 46 else "")
            self.search_bar.setPlaceholderText(f"✦  {short}")
            QTimer.singleShot(5500, lambda:
                self.search_bar.setPlaceholderText("Ask ISHA anything…"))
            if not getattr(self, "_suppress_speak", False):
                self._speak(text)

    def _place_bubble(self, text: str):
        """Park the bubble just above the bar, in screen coordinates.

        The bubble is its own top-level window now, so this positions it
        relative to the bar's global geometry and clamps it to the screen —
        instead of the old maths, which always resolved to y=2 inside the bar."""
        bubble = getattr(self, "_bubble", None)
        if bubble is None:
            return
        fm   = QFontMetrics(QFont("Segoe UI", 11))
        rect = fm.boundingRect(QRectF(0, 0, 520, 9999).toRect(),
                                Qt.TextWordWrap, text)
        bw = max(160, min(572, rect.width() + 52))
        bh = max(44,  rect.height() + 30)
        bubble.setFixedSize(bw, bh)

        bar = self.frameGeometry()
        GAP = 14
        bx = bar.center().x() - bw // 2
        by = bar.top() - bh - GAP                      # above the UI

        screen = None
        if hasattr(QApplication, "screenAt"):
            screen = QApplication.screenAt(bar.center())
        screen = screen or QApplication.primaryScreen()
        area = screen.availableGeometry()

        if by < area.top() + 4:                        # no room up there? go below
            by = bar.bottom() + GAP
        bx = max(area.left() + 4, min(bx, area.right()  - bw - 4))
        by = max(area.top()  + 4, min(by, area.bottom() - bh - 4))

        bubble.move(int(bx), int(by))
        if bubble.isVisible():
            bubble.raise_()

    def moveEvent(self, e):
        """Drag the bar around and the bubble comes along with it."""
        super().moveEvent(e)
        bubble = getattr(self, "_bubble", None)
        if bubble is not None and bubble.isVisible():
            self._place_bubble(bubble.current_text() or " ")

    def hideEvent(self, e):
        bubble = getattr(self, "_bubble", None)
        if bubble is not None:
            bubble.hide()
        super().hideEvent(e)

    def _on_typed(self, text: str):
        self.respond(text, "user")
        self._send_to_llm(text)

    # ---- Windows OS Control  ----
    def _run_os_task(self, method, *args, on_success=None, on_error=None, **kwargs):
        """Run any WindowsController method off the UI thread. Only one
        OS task runs at a time to avoid overlapping window/file operations."""
        if not _OS_CONTROL_OK or self.os_controller is None:
            self.respond("OS control module load nahi hua (isha_os_control.py / pywin32 check karo).", "isha")
            return
        if self._os_worker is not None:
            self.respond("Pichla OS task abhi chal raha hai, thoda ruko.", "isha")
            return
        self._os_worker = OSTaskWorker(method, *args, **kwargs)

        def _finished(result: dict):
            if on_success:
                on_success(result)
            else:
                self.respond(result.get("message", ""), "isha")

        def _errored(msg: str):
            (on_error or (lambda m: self.respond(f"OS task fail: {m}", "isha")))(msg)

        self._os_worker.task_finished.connect(_finished)
        self._os_worker.task_error.connect(_errored)
        self._os_worker.finished.connect(self._os_worker.deleteLater)
        self._os_worker.finished.connect(lambda: setattr(self, "_os_worker", None))
        self._os_worker.start()

    def _on_stats_updated(self, stats: dict):
        """Live CPU/RAM/Battery/Disk feed from SystemStatsMonitor -> update UI & Proactive Agent."""
        self._latest_stats = stats
        if self._qs_popup is not None:
            self._qs_popup.update_stats(stats)

        # Proactive Autonomous Monitoring
        now = time.time()
        if not hasattr(self, "_last_proactive_alert"):
            self._last_proactive_alert = 0

        if now - self._last_proactive_alert > 300:
            ram = stats.get("ram_percent")
            batt = stats.get("battery_percent")
            plugged = stats.get("battery_plugged")
            disk = stats.get("disk_percent")

            if ram is not None and ram > 88.0:
                self._last_proactive_alert = now
                self.respond(f"✦ Proactive Alert: RAM load is high ({ram:.0f}%). Would you like me to inspect background processes?", "isha")
            elif batt is not None and batt < 15.0 and not plugged:
                self._last_proactive_alert = now
                self.respond(f"✦ Proactive Alert: Battery level is low ({batt:.0f}%). Please plug in your charger.", "isha")
            elif disk is not None and disk > 90.0:
                self._last_proactive_alert = now
                self.respond(f"✦ Proactive Alert: Disk space is nearly full ({disk:.0f}%).", "isha")

    # ---- Local LLM (GGUF / llama.cpp) ----
    def _build_llm_options(self) -> dict:
        """Sampling only. Load-time settings (ctx, threads, GPU layers, batch)
        belong to GGUFModelManager.load_params, since changing those means
        reloading the weights, not just re-sampling."""
        c = self.cfg
        opts = {
            "max_tokens":        int(c.get("max_tokens", 512)),
            "temperature":       float(c.get("temperature", 0.8)),
            "top_p":             float(c.get("top_p", 0.9)),
            "top_k":             int(c.get("top_k", 40)),
            "min_p":             float(c.get("min_p", 0.0)),
            "repeat_penalty":    float(c.get("repeat_penalty", 1.1)),
            "presence_penalty":  float(c.get("presence_penalty", 0.0)),
            "frequency_penalty": float(c.get("frequency_penalty", 0.0)),
        }
        stop_raw = c.get("stop_tokens", [])
        stop_list = stop_raw if isinstance(stop_raw, list) else \
            [s.strip() for s in str(stop_raw).split(",") if s.strip()]
        if stop_list:
            opts["stop"] = stop_list
        seed = int(c.get("seed", -1))
        if seed >= 0:
            opts["seed"] = seed
        return opts

    def _warm_up_model(self):
        """Load the GGUF weights on a daemon thread at startup so the first
        real message does not pay the load cost. The model then stays resident
        for the whole session — this is the single biggest latency win over the
        old server backend, which re-scheduled the model per request."""
        if not bool(self.cfg.get("preload_model", True)):
            return
        if not _LLAMA_CPP_OK:
            d = _GGUF_BACKEND_DIAGNOSTIC
            print("[gguf] llama-cpp-python missing — GGUF backend will not start.\n"
                  f"  Runtime: {d.get('python', sys.executable)}\n"
                  f"  Reason:  {d.get('error') or _LLAMA_CPP_ERR or 'unknown'}")
            return

        def _preload():
            print("[gguf] llama-cpp-python: OK")
            resolved = resolve_gguf_path(self.cfg)
            if resolved.is_file():
                print(f"[gguf] GGUF model: FOUND ({resolved})")
            else:
                print(f"[gguf] GGUF model: NOT FOUND ({resolved})")
            try:
                GGUF.get(self.cfg)
                print(f"[gguf] GGUF model: LOADED in {GGUF.last_load_seconds:.1f}s — "
                      f"{', '.join(GGUF.loaded_names())}")
            except GGUFUnavailable as e:
                print(f"[gguf] preload skipped: {e}")
            except Exception as e:
                print(f"[gguf] preload failed: {type(e).__name__}: {e}")

        threading.Thread(target=_preload, daemon=True, name="isha-gguf-preload").start()

    def _send_to_llm(self, user_text: str):
        if (self._llm_worker is not None or self._router_worker is not None
                or self._agent_worker is not None):
            return  
        self.search_bar.setPlaceholderText("⟳  Thinking…")
        self.dots.start()
        self._update_glow_boost()
        ISHA_STOP.reset()                      # a new request clears the last STOP
        self._set_busy(True)
        try:
            _cls = isha_router.classify_task(user_text)
            ACTION_LOG.record("request", text=user_text[:200], task=_cls.task.value)
            ACTION_LOG.set_current(task=_cls.task.value, tool="", step="planning")
        except Exception:
            pass
        self._conv_history.append({"role": "user", "content": user_text})
        # 30 messages of history is a lot of prefill to re-read on every turn
        # for a 1.5-3B model. 12 keeps the thread coherent and roughly halves
        # time-to-first-token.
        _keep = max(4, int(self.cfg.get("history_turns", 12)))
        self._conv_history = self._conv_history[-_keep:]

        if _TOOLS_OK and bool(self.cfg.get("tool_calling", True)):
            # Quick commands are a latency shortcut for single, unambiguous
            # phrases. They must NOT swallow compound requests — "cpu batao aur
            # notepad kholo" used to collapse into just the CPU check, because
            # the regex matched first and the agent never saw the rest.
            # Planner v3: command ko kaamon me todo aur jitna ho sake bina LLM
            # ke seedha tools se jodo. Compound command ab agent ke paas tabhi
            # jaati hai jab uska ek bhi hissa pehchana na ja sake.
            plan = plan_request(user_text)
            known = [st for st in plan if st["tool"]]
            if len(plan) > 1 and known:
                self._start_plan(plan)
                return
            if len(plan) == 1 and known:
                # Deterministic match -> no LLM in the loop at all.
                self._execute_tool_and_reply(
                    known[0]["tool"], known[0]["args"],
                    narrate=not bool(self.cfg.get("fast_tools", True)))
                return
            if looks_like_smalltalk(user_text):
                self._start_chat_completion()
            elif bool(self.cfg.get("agent_mode", True)):
                self._start_agent(user_text)
            else:
                self._route_intent(user_text)
        else:
            self._start_chat_completion()

    # ---- Planned multi-step execution (no LLM narration) ----
    def _start_plan(self, plan: list):
        print("[plan] " + " | ".join(
            f"{st['segment']} -> {st['tool'] or 'LLM decide'}" for st in plan))
        self._agent_worker = PlanExecutorWorker(
            steps=plan,
            cfg=self.cfg,
            options=self._build_llm_options(),
            confirm_risky=bool(self.cfg.get("confirm_risky_tools", True)),
        )
        self._agent_worker.step_note.connect(self._on_agent_step)
        self._agent_worker.permission_needed.connect(self._on_agent_permission)
        self._agent_worker.finished_reply.connect(self._on_agent_finished)
        self._agent_worker.error_occurred.connect(self._on_agent_error)
        self._agent_worker.finished.connect(self._agent_worker.deleteLater)
        self._agent_worker.finished.connect(lambda: setattr(self, "_agent_worker", None))
        self._agent_worker.start()

    # ---- Agentic execution (plan -> act -> observe) ----
    def _start_agent(self, user_text: str):
        sys_prompt = AGENT_SYSTEM_PROMPT + "\n\n" + build_runtime_context(self.cfg)
        cap = str(self.cfg.get("capability_note", "")).strip()
        if cap:
            sys_prompt += "\n\n" + cap
        extra = str(self.cfg.get("system_prompt", "")).strip()
        if extra:
            sys_prompt += "\n\n" + extra

        for block in (LONG_MEMORY.prompt_block(user_text), WORKING.summary()):
            if block:
                sys_prompt += "\n\n" + block
        # Long-term memory, same as the plain chat path uses.
        if _vector_memory:
            try:
                mems = _vector_memory.search_memory(user_text, top_k=3)
                if mems:
                    joined = "\n".join(f"- {m['text']}" for m in mems)
                    sys_prompt += f"\n\n[RELEVANT CONTEXT FROM LONG-TERM MEMORY]:\n{joined}"
            except Exception:
                pass

        messages = [{"role": "system", "content": sys_prompt}] + list(self._conv_history)

        run_cfg = pick_cfg_for(user_text, self.cfg)
        if run_cfg.get("gguf_model_path") != self.cfg.get("gguf_model_path"):
            print(f"[route] coding task -> {resolve_gguf_path(run_cfg).name}")

        self._agent_worker = AgentWorker(
            messages=messages,
            cfg=run_cfg,
            options=self._build_llm_options(),
            max_steps=int(self.cfg.get("agent_max_steps", 8)),
            confirm_risky=bool(self.cfg.get("confirm_risky_tools", True)),
        )
        self._agent_worker.step_note.connect(self._on_agent_step)
        self._agent_worker.permission_needed.connect(self._on_agent_permission)
        self._agent_worker.finished_reply.connect(self._on_agent_finished)
        self._agent_worker.error_occurred.connect(self._on_agent_error)
        self._agent_worker.finished.connect(self._agent_worker.deleteLater)
        self._agent_worker.finished.connect(lambda: setattr(self, "_agent_worker", None))
        self._agent_worker.start()

    def _on_agent_step(self, note: str):
        """Progress line. Deliberately NOT spoken and not written to history —
        it is scaffolding, not something ISHA said."""
        print(f"[agent] {note}")
        short = note[:52] + ("…" if len(note) > 52 else "")
        self.search_bar.setPlaceholderText(short)
        if self._comm_dock and self._comm_dock.isVisible():
            ts = datetime.now().strftime("%I:%M %p")
            self._comm_dock._log_area.append(f"[{ts}] ISHA: {note}")

    def _on_agent_permission(self, req):
        """Runs on the UI thread; the agent thread is blocked until we answer.

        This is the whole point of the risk tiers — ISHA can drive the machine,
        so a wrong tool pick has to be stoppable before it lands, not explained
        afterwards."""
        approved = False
        try:
            if req.risk == "critical":
                title = "ISHA — critical action"
                head  = "ISHA yeh CRITICAL action chalana chahti hai:"
                icon  = QMessageBox.Critical
            else:
                title = "ISHA — confirm"
                head  = "ISHA yeh action chalana chahti hai:"
                icon  = QMessageBox.Warning

            box = QMessageBox(self)
            box.setWindowTitle(title)
            box.setIcon(icon)
            box.setText(f"{head}\n\n{req.summary()}")
            box.setInformativeText("Allow karein?")
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            approved = (box.exec_() == QMessageBox.Yes)
        except Exception as e:
            print(f"[agent] permission dialog failed: {e}")
            approved = False
        finally:
            if not req.answered:
                req.answer(approved)
        print(f"[agent] {req.tool_name} -> {'ALLOWED' if req.approved else 'DENIED'}")

    def _on_agent_finished(self, text: str):
        self._set_busy(False)
        self.dots.stop()
        self._update_glow_boost()
        text = strip_thinking(text) or "Ho gaya."

        # Fold what the agent actually DID into the conversation, not just what
        # it said about it. Without this, a follow-up like "usko delete kar do"
        # has no idea which file the previous turn found.
        trace = []
        try:
            worker = getattr(self, "_agent_worker", None)
            trace = list(getattr(worker, "trace", []) or [])
        except Exception:
            trace = []
        if trace:
            lines = []
            for step in trace[-6:]:
                args = describe_tool_call(step["tool"], step.get("args") or {})
                result = str(step.get("result", ""))[:400]
                lines.append(f"{args} -> {result}")
            self._conv_history.append({
                "role": "system",
                "content": "[TOOL EXECUTION LOG — these actions really ran]\n"
                           + "\n".join(lines),
            })

        self._conv_history.append({"role": "assistant", "content": text})
        self.respond(text, "isha")

    def _on_agent_error(self, msg: str):
        self._set_busy(False)
        self.dots.stop()
        self._update_glow_boost()
        self.respond(msg, "isha")

    def _router_options(self) -> dict:
        """The router only ever emits ~40 tokens of JSON, so it does not need
        the chat model's creative sampling or its 512-token budget. Sharing
        _build_llm_options() meant every routing decision ran at
        temperature 0.8 with room to ramble - slow and less accurate."""
        opts = dict(self._build_llm_options())
        opts.update({
            "temperature": 0.0,
            "top_p":       1.0,
            "top_k":       1,
            "max_tokens":  96,
        })
        opts.pop("presence_penalty", None)
        opts.pop("frequency_penalty", None)
        return opts

    def _route_intent(self, user_text: str):
        """Ask the model (JSON-mode, non-streaming) whether this message maps
        to a registered tool."""
        router_messages = [
            {"role": "system", "content": build_tool_system_prompt()},
            {"role": "user", "content": user_text},
        ]
        self._router_worker = GGUFWorker(
            cfg=self.cfg,
            messages=router_messages,
            options=self._router_options(),
            streaming=False,
            json_mode=True,
        )
        self._router_worker.finished_reply.connect(self._on_router_finished)
        self._router_worker.error_occurred.connect(self._on_router_error)
        self._router_worker.finished.connect(self._router_worker.deleteLater)
        self._router_worker.finished.connect(lambda: setattr(self, "_router_worker", None))
        self._router_worker.start()

    def _on_router_error(self, msg: str):
        self._start_chat_completion()

    def _on_router_finished(self, raw_text: str):
        decision = parse_router_json(raw_text)

        if not decision or decision.get("action") != "tool_call" or not decision.get("tool"):
            self._start_chat_completion()
            return

        tool_name = str(decision.get("tool"))
        args = decision.get("arguments") or {}
        self._execute_tool_and_reply(tool_name, args)

    def _confirm_tool(self, tool_name: str, args: dict):
        """Same permission gateway as the agent uses, for the non-agent paths.
        Returns "auto" (no prompt needed), "user" (approved in the dialog) or
        False (denied / blocked)."""
        resolved = resolve_tool_name(tool_name) or tool_name
        risk = tool_risk(resolved)
        decision = isha_perm.decide(resolved, risk, self.cfg)
        if decision.action == isha_perm.ALLOW:
            return "auto"
        if decision.action == isha_perm.DENY:
            return False
        box = QMessageBox(self)
        box.setWindowTitle("ISHA — confirm")
        box.setIcon(QMessageBox.Critical if risk == "critical" else QMessageBox.Warning)
        box.setText(f"ISHA yeh action chalana chahti hai:\n\n{describe_tool_call(resolved, args)}")
        box.setInformativeText("Allow karein?")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        ok = box.exec_() == QMessageBox.Yes
        ACTION_LOG.record("approval", tool=resolved, args=args, risk=risk,
                          approval="approved" if ok else "denied")
        return "user" if ok else False

    def _execute_tool_and_reply(self, tool_name: str, args: dict, narrate: bool = True):
        """Run tool execution in OSTaskWorker off the main thread to prevent UI freezing.

        narrate=False skips the follow-up chat completion and speaks the tool's
        own return string instead. A deterministic quick command already knows
        exactly what happened, so paying for a second full generation just to
        have the model rephrase "Notepad khol diya." is pure latency - it was
        roughly doubling the wait on every single command."""
        approval = self._confirm_tool(tool_name, args)
        if not approval:
            self.dots.stop()
            self._update_glow_boost()
            self._set_busy(False)
            self.respond("Theek hai, cancel kar diya.", "isha")
            return

        def _do_execute():
            ok, out, _ = run_tool_gated(tool_name, args, cfg=self.cfg,
                                        pre_approved=(approval == "user"))
            if not ok:
                raise ToolError(out.split(":", 1)[1].strip() if out.startswith(("ERROR:", "DENIED:", "CANCELLED:")) else out)
            return out

        def _reply_direct(msg: str):
            self.dots.stop()
            self._update_glow_boost()
            self._set_busy(False)
            self._conv_history.append({"role": "assistant", "content": msg})
            self.respond(msg, "isha")

        def _on_tool_done(result):
            res_str = result.get("message", str(result)) if isinstance(result, dict) else str(result)
            if not narrate:
                _reply_direct(res_str)
                return
            note = f"[TOOL RESULT] '{tool_name}' ran successfully -> {res_str}"
            self._conv_history.append({"role": "system", "content": note})
            self._start_chat_completion()

        def _on_tool_err(err_msg: str):
            if not narrate:
                _reply_direct(err_msg)
                return
            note = f"[TOOL ERROR] '{tool_name}' could not run -> {err_msg}"
            self._conv_history.append({"role": "system", "content": note})
            self._start_chat_completion()

        self._run_os_task(_do_execute, on_success=_on_tool_done, on_error=_on_tool_err)

    # ---- Step 2: normal streaming chat completion ----
    def _start_chat_completion(self):
        messages = []
        # Same identity as the agent path. These used to be two different
        # prompts, so ISHA had two different personalities depending on which
        # branch handled the message.
        sys_prompt = ISHA_IDENTITY + "\n\n" + build_runtime_context(self.cfg)
        cap = str(self.cfg.get("capability_note", "")).strip()
        if cap:
            sys_prompt += "\n\n" + cap
        extra = str(self.cfg.get("system_prompt", "")).strip()
        if extra:
            sys_prompt += "\n\n" + extra

        # Contextual Vector Memory Retrieval
        last_user_msg = next((m["content"] for m in reversed(self._conv_history) if m["role"] == "user"), "")
        for block in (LONG_MEMORY.prompt_block(last_user_msg), WORKING.summary()):
            if block:
                sys_prompt += "\n\n" + block
        if last_user_msg and _vector_memory:
            mems = _vector_memory.search_memory(last_user_msg, top_k=3)
            if mems:
                mem_str = "\n".join(f"- {m['text']}" for m in mems)
                sys_prompt += f"\n\n[RELEVANT CONTEXT FROM LONG-TERM MEMORY]:\n{mem_str}"

        messages.append({"role": "system", "content": sys_prompt})
        messages.extend(self._conv_history)

        streaming = bool(self.cfg.get("streaming", True))
        if streaming:
            self._bubble.begin_stream()
            self._place_bubble(" ")

        # Fresh sentence buffer per reply, otherwise the tail of the previous
        # answer leaks into the first thing spoken for this one.
        self._sent_streamer.reset()
        self._stream_spoke = False

        self._llm_worker = GGUFWorker(
            messages=messages,
            options=self._build_llm_options(),
            cfg=pick_cfg_for(last_user_msg, self.cfg),
            streaming=streaming,
            json_mode=bool(self.cfg.get("json_mode", False)),
        )
        self._llm_worker.chunk_received.connect(self._on_llm_chunk)
        if streaming and bool(self.cfg.get("stream_tts", True)):
            # Speech runs off the raw deltas, not the coalesced GUI signal, so
            # a sentence can be queued the moment it closes.
            self._llm_worker.delta_received.connect(self._on_llm_delta)
        self._llm_worker.model_loading.connect(self._on_model_loading)
        self._llm_worker.finished_reply.connect(self._on_llm_finished)
        self._llm_worker.error_occurred.connect(self._on_llm_error)
        self._llm_worker.finished.connect(self._llm_worker.deleteLater)
        self._llm_worker.finished.connect(lambda: setattr(self, "_llm_worker", None))
        self._llm_worker.start()

    def _on_llm_chunk(self, text: str):
        """Cumulative text, already coalesced to ~25 fps by the worker."""
        text = strip_thinking(text)
        self._bubble.update_streaming_text(text)
        self._place_bubble(text or " ")

    def _on_llm_delta(self, delta: str):
        """Raw token delta -> sentence buffer -> TTS queue.

        Runs on the UI thread but does almost nothing: append, regex-scan for a
        sentence end, and hand any finished sentence to the speech queue. The
        actual synthesis happens in TTSWorker on its own thread, so generation
        never waits for speech.
        """
        try:
            for sentence in self._sent_streamer.feed(delta):
                self._speak_stream(sentence)
        except Exception as e:
            print(f"[tts-stream] {type(e).__name__}: {e}")

    def _on_model_loading(self, note: str):
        self.search_bar.setPlaceholderText(f"⟳  {note}")

    def _on_llm_finished(self, text: str):
        self._set_busy(False)
        self.dots.stop()
        self._update_glow_boost()
        text = strip_thinking(text) or (
            "Khali jawab mila — GGUF model path aur context size check karo.")

        # Whatever is still sitting in the sentence buffer has to be spoken too,
        # otherwise the last clause of every reply goes missing.
        tail = self._sent_streamer.flush()
        if tail and self._stream_spoke:
            self._speak_stream(tail)

        self._conv_history.append({"role": "assistant", "content": text})
        # Streaming already voiced this reply sentence by sentence; respond()
        # must not queue the whole thing a second time.
        self._suppress_speak = self._stream_spoke
        try:
            self.respond(text, "isha")
        finally:
            self._suppress_speak = False
            self._stream_spoke = False
            self._sent_streamer.reset()

    def _on_llm_error(self, msg: str):
        self._set_busy(False)
        self.dots.stop()
        self._update_glow_boost()
        self.respond(msg, "isha")

    # ---- Voice output ----
    def _speak(self, text: str):
        if not self.cfg.get("tts_enabled", True):
            return
        clean = _strip_for_speech(text)
        if not clean:
            return
        if not _TTS_OK:
            self._warn_tts_once(
                "no voice engine — run:  pip install edge-tts pygame   "
                + _tts_diagnostics())
            return
        self._tts_queue.append(clean)
        self._process_tts_queue()

    def _process_tts_queue(self):
        if self._tts_busy or not self._tts_queue:
            return
        text = self._tts_queue.pop(0)
        self._tts_busy = True
        self._tts_worker = TTSWorker(
            text,
            voice=self.cfg.get("tts_voice", "en-US-AriaNeural"),
            rate=self.cfg.get("tts_rate", "+0%"),
        )
        self._tts_worker.error_occurred.connect(self._on_tts_error)
        self._tts_worker.finished.connect(self._tts_worker.deleteLater)
        self._tts_worker.finished.connect(self._on_tts_worker_finished)
        self._tts_worker.start()

    def _on_tts_error(self, msg: str):
        self._warn_tts_once(msg)

    def _warn_tts_once(self, msg: str):
        """Speech problems used to only ever reach a print() nobody reads, so a
        mute ISHA looked like a mystery. Log every time, surface it in the bar once."""
        print(f"[TTS] {msg}")
        if getattr(self, "_tts_warned", False):
            return
        self._tts_warned = True
        self.search_bar.setPlaceholderText("⚠  Voice failed — check console for [TTS]")
        QTimer.singleShot(7000, lambda:
            self.search_bar.setPlaceholderText("Ask ISHA anything…"))

    def _on_tts_worker_finished(self):
        self._tts_busy = False
        self._tts_worker = None
        self._process_tts_queue()

    def _speak_stream(self, sentence: str):
        """Queue one sentence produced mid-generation.

        Deliberately reuses the existing _tts_queue / _process_tts_queue pair,
        so the engine fallback chain (edge-tts -> pyttsx3 -> native) and every
        error path stay exactly as they were. The only change is *when* text
        arrives: during generation instead of after it.
        """
        if not self.cfg.get("tts_enabled", True):
            return
        clean = _strip_for_speech(sentence)
        if not clean:
            return
        if not _TTS_OK:
            # No voice engine is not a generation failure — say so once and let
            # the model keep streaming to the GUI.
            self._warn_tts_once("no voice engine — run:  pip install edge-tts pygame   "
                                + _tts_diagnostics())
            self._stream_spoke = True      # stops the duplicate attempt in respond()
            return
        self._stream_spoke = True
        self._tts_queue.append(clean)
        self._process_tts_queue()

    def _stop_generation(self):
        """Stop everything this reply is doing: tokens, queued speech, current
        speech. Anything already queued must NOT keep talking after a stop."""
        stopped = ISHA_STOP.trigger() > 0 or self._os_worker is not None
        ACTION_LOG.record("stop", source="user")
        for attr in ("_llm_worker", "_router_worker", "_agent_worker"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.stop()
                    stopped = True
                except Exception:
                    pass
        self._sent_streamer.reset()
        self._stream_spoke = False
        self._stop_speaking()
        self._set_busy(False)
        if stopped:
            self.dots.stop()
            self._update_glow_boost()
            self.search_bar.setPlaceholderText("■  Roka gaya")
            QTimer.singleShot(2500, lambda:
                self.search_bar.setPlaceholderText("Ask ISHA anything…"))
        return stopped

    def keyPressEvent(self, event):
        """Esc = stop generating and stop speaking. No new widgets, no layout
        change — just a key that was previously unused."""
        try:
            if event.key() == Qt.Key_Escape:
                if self._stop_generation():
                    return
        except Exception:
            pass
        super().keyPressEvent(event)

    def _stop_speaking(self):
        """Cut off whatever is being said and drop anything queued behind it."""
        self._tts_queue.clear()
        w = self._tts_worker
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass

    # ---- Voice input (microphone) ----
    def _start_mic_listening(self):
        if self._mic_thread is not None:
            return
        self._mic_thread = MicListenerThread(language="en-IN")
        self._mic_thread.speech_recognized.connect(self._on_speech_recognized)
        self._mic_thread.error_occurred.connect(self._on_speech_error)
        self._mic_thread.start()

    def _stop_mic_listening(self):
        if self._mic_thread is not None:
            self._mic_thread.stop()
            self._mic_thread = None

    def _on_speech_recognized(self, text: str):
        text = text.strip()
        if not text:
            return
        self.respond(text, "user")
        self._send_to_llm(text)

    def _on_speech_error(self, msg: str):
        print(f"[STT] {msg}")

    def _toggle_mic(self):
        self._mic_on = not self._mic_on
        self.mic_btn.set_active(self._mic_on)
        self.wave_bar.set_active(self._mic_on)
        if self._mic_on and not bool(self.cfg.get("stt_enabled", True)):
            self.respond("Speech recognition setup me band hai (Settings / first-run wizard se on karo).", "isha")
            self._mic_on = False
            self.mic_btn.set_active(False)
            self.wave_bar.set_active(False)
            return
        if self._mic_on:
            if not _STT_OK:
                self.respond(
                    "Microphone input ke liye install karo: pip install SpeechRecognition PyAudio",
                    "isha")
                self._mic_on = False
                self.mic_btn.set_active(False)
                self.wave_bar.set_active(False)
                self._update_glow_boost()
                return
            self._stop_speaking()      # don't let ISHA transcribe its own voice
            self.respond("Microphone activated. Boliye…", "isha")
            self._start_mic_listening()
        else:
            self.respond("Microphone off.", "isha")
            self._stop_mic_listening()
        self._update_glow_boost()

    def _toggle_qs(self):
        if not self._qs_popup:
            self._qs_popup = QuickSettingsPopup(self)
        btn_g = self.mapToGlobal(self.settings_btn.pos())
        popup_w = self._qs_popup.sizeHint().width() or 290
        popup_h = self._qs_popup.sizeHint().height() or 420
        px = btn_g.x() - popup_w + 54
        py = btn_g.y() - popup_h - 12
        self._qs_popup.toggle(QPoint(px, py))

    def _greet(self):
        self.respond("Good to see you, Boss!", "isha")

    def _tick(self):
        for s in self._stars: s.update()
        self._glow_phase = (self._glow_phase + 0.045) % (2 * math.pi)
        self.update()

    def _update_glow_boost(self):
        """Recompute how 'lit up' the cyberpunk border glow should be, based on
        what ISHA is actively doing right now. Called whenever one of those
        activity states changes (mic toggle, thinking start/stop, hand control
        on/off) rather than every frame, since it only needs to change on
        state transitions."""
        boost = 0.0
        if getattr(self, "_mic_on", False):
            boost += 0.4
        if getattr(self.dots, "_on", False):
            boost += 0.35
        if getattr(self, "_hand_active", False):
            boost += 0.35
        self._glow_boost = min(1.0, boost)

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        cap = QPainterPath()
        cap.addRoundedRect(QRectF(0, H - 82, W, 82), 41, 41)
        p.setClipPath(cap)
        bg = QLinearGradient(0, H - 82, W, H)
        bg.setColorAt(0.0, QColor(  7, 10, 20, 254))
        bg.setColorAt(0.5, QColor( 18, 23, 38, 252))
        bg.setColorAt(1.0, QColor(  7, 10, 20, 254))
        p.fillPath(cap, QBrush(bg))
        stars = self._stars
        for i in range(len(stars)):
            for j in range(i + 1, min(i + 10, len(stars))):
                d = math.hypot(stars[i].x - stars[j].x, stars[i].y - stars[j].y)
                if d < 70:
                    a = int(50 * (1 - d / 70))
                    p.setPen(QPen(QColor(0, 180, 220, a), 0.5))
                    p.drawLine(QPointF(stars[i].x, H - 82 + stars[i].y),
                               QPointF(stars[j].x, H - 82 + stars[j].y))
        p.setPen(Qt.NoPen)
        for s in stars:
            p.setBrush(QBrush(s.color))
            p.drawEllipse(QPointF(s.x, H - 82 + s.y), s.sz, s.sz)
        p.setClipping(False)

        # --- Cyberpunk pulsing glow border ---
        # Idle: a slow cyan breathing pulse. Active (listening/thinking/hand
        # control on): brighter, and shifted slightly toward magenta (mixing
        # in C_PINK) for a two-tone neon look instead of one flat color.
        pulse = 0.5 + 0.5 * math.sin(self._glow_phase)
        glow_level = max(0.0, min(1.6, 0.32 + 0.68 * pulse + self._glow_boost * 0.6))
        mix = min(1.0, self._glow_boost)
        glow_r = int(0 + mix * 140)
        glow_g = int(200 - mix * 40)
        glow_b = 255
        for width, base_alpha in ((14, 8), (10, 14), (6, 24), (3, 42)):
            alpha = max(0, min(255, int(base_alpha * glow_level)))
            if alpha <= 0:
                continue
            p.setPen(QPen(QColor(glow_r, glow_g, glow_b, alpha), width))
            p.setBrush(Qt.NoBrush)
            p.drawPath(cap)

        p.setPen(QPen(QColor(0, 240, 255, 200), 2.0))
        p.setBrush(Qt.NoBrush); p.drawPath(cap)
        sheen = QPainterPath()
        sheen.addRoundedRect(QRectF(3, H - 82 + 3, W - 6, 28), 38, 38)
        p.fillPath(sheen, QColor(255, 255, 255, 6))
        cx = W // 2
        lg = QLinearGradient(cx - 120, H - 82, cx + 120, H - 82)
        lg.setColorAt(0.0, QColor(0, 0, 0, 0))
        lg.setColorAt(0.5, QColor(0, 240, 255, 60))
        lg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(QPen(QBrush(lg), 1))
        p.drawLine(QPointF(cx - 120, H - 41), QPointF(cx + 120, H - 41))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not _start_native_drag(self, e):
                self._drag_pos = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):

        if e.buttons() == Qt.LeftButton and self._drag_pos:
            self.move(e.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def _centre(self):
        sc = QApplication.primaryScreen().geometry()
        self.move(
            (sc.width()  - self.width())  // 2,
             sc.height() - self.height()  - 60,
        )

    def closeEvent(self, event):
        self._stop_mic_listening()
        self._stop_speaking()
        ISHA_STOP.trigger()
        try:
            close_file_index()
        except Exception:
            pass
        if self._agent_panel is not None:
            self._agent_panel.shutdown()
            self._agent_panel.deleteLater()
        b = getattr(self, "_bubble", None)
        if b is not None:
            b.hide()
        if self._llm_worker is not None:
            self._llm_worker.stop()
        if self._router_worker is not None:
            self._router_worker.stop()
        if self._agent_worker is not None:
            self._agent_worker.stop()
            self._agent_worker.wait(2000)
        try:
            GGUF.unload_all()       # free the weights instead of leaking them
        except Exception:
            pass
        if self.stats_monitor is not None:
            self.stats_monitor.stop()
            self.stats_monitor.wait(1000)
        if self._os_worker is not None:
            self._os_worker.wait(1000)
        if self.hand_worker is not None:
            self.hand_worker.stop()
            self.hand_worker.wait(1500)
        super().closeEvent(event)


# ------------------------------------------------
# Entry point
def _maybe_first_run():
    """Fresh installs without a config get the installer's first-run wizard.
    Runs in a subprocess (it owns its own QApplication)."""
    if os.environ.get("ISHA_NO_FIRST_RUN") == "1" or not (PROJECT_ROOT / "installer").is_dir():
        return
    try:
        from installer.installer import needs_first_run
        if needs_first_run(PROJECT_ROOT):
            subprocess.run([sys.executable, "-m", "installer", "--first-run", "--install-dir", str(PROJECT_ROOT)],
                           cwd=str(PROJECT_ROOT), timeout=3600)
    except Exception as e:
        print(f"[ISHA] first-run wizard skipped: {e}")


def main():
    _maybe_first_run()
    try:
        _cfg_probe = load_config()
        if bool(_cfg_probe.get("startup_self_check", True)):
            print_self_diagnostics(_cfg_probe)
    except Exception as _e:
        print(f"[ISHA] self-check skipped: {_e}")

    _force_pyqt5_qt_plugin_path()   # final word, right before Qt reads the env var
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,    True)
    app = QApplication(sys.argv)
    _init_colors()
    _init_fonts()
    app.setApplicationName("ISHA")
    win = ISHAWindow()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print("[ISHA] Fatal error on startup:\n" + tb)
        try:
            log_path = Path(__file__).resolve().parent / "isha_crash.log"
            log_path.write_text(f"{datetime.now().isoformat()}\n{tb}", encoding="utf-8")
            print(f"[ISHA] Full error saved to: {log_path}")
        except Exception:
            pass
        if _IS_WINDOWS:
            try:
                input("\nPress Enter to close this window...")
            except Exception:
                pass
        sys.exit(1)


        