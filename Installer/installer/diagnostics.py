"""First-run diagnostics, executed INSIDE the ISHA runtime (subprocess) so
the result reflects exactly what ISHA will see. READY / WARNING / MISSING."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROBE = r'''
import json, os, sys, importlib.util, platform
root = sys.argv[1]; deep = sys.argv[2] == "1"
sys.path.insert(0, root)
R, W, M = "READY", "WARNING", "MISSING"
out = []
def add(name, st, detail): out.append({"name": name, "status": st, "detail": detail})
def has(m):
    try: return importlib.util.find_spec(m) is not None
    except Exception: return False
cfg = {}
try: cfg = json.load(open(os.path.join(root, "isha_config.json"), encoding="utf-8"))
except Exception: pass
feats = cfg.get("features", {})
add("Python runtime", R, f"{platform.python_version()} at {sys.executable}")
core = [m for m in ("PyQt5", "psutil", "requests") if not has(m)]
add("Dependencies", R if not core else M, "core packages present" if not core else "missing: " + ", ".join(core))
try:
    from isha_core import llama_runtime as L
    info = L.installed_info()
    if info["server_exe"] or info["native"]:
        add("llama.cpp", R, ("native wheel" if info["native"] else "llama-server " + info.get("tag", "")) + f" ({info['variant']})")
    else:
        add("llama.cpp", M, "runtime not installed")
except Exception as e:
    add("llama.cpp", M, f"{type(e).__name__}: {e}")
try:
    from isha_core import router
    reg = router.ModelRegistry(cfg, os.path.join(root, "models"), root)
    roles = reg.available_roles()
    add("GGUF model", R if "general" in roles else (W if roles else M),
        ("roles: " + ", ".join(roles)) if roles else "no .gguf model installed")
except Exception as e:
    add("GGUF model", M, str(e))
try:
    os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", ""))
    from PyQt5.QtWidgets import QApplication
    add("GUI", R, "PyQt5 loads")
except Exception as e:
    add("GUI", M, f"PyQt5: {e}")
if cfg.get("tts_enabled", True) is False:
    add("TTS", W, "disabled by you")
else:
    eng = [n for n, m in (("edge-tts", "edge_tts"), ("pyttsx3", "pyttsx3")) if has(m)]
    native = platform.system() == "Windows" or any(__import__("shutil").which(x) for x in ("espeak-ng", "espeak", "spd-say", "say"))
    add("TTS", R if eng or native else M, ", ".join(eng + (["system voice"] if native else [])) or "no TTS engine")
if cfg.get("stt_enabled", True) is False:
    add("STT", W, "disabled by you")
elif not (has("speech_recognition") and has("pyaudio")):
    add("STT", M, "SpeechRecognition/PyAudio missing")
else:
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        n = sum(1 for i in range(pa.get_device_count()) if pa.get_device_info_by_index(i).get("maxInputChannels", 0) > 0)
        pa.terminate()
        add("STT", R if n else W, f"{n} microphone(s)" if n else "no microphone detected")
    except Exception as e:
        add("STT", W, f"microphone check failed: {e}")
if feats.get("vision") is False:
    add("Camera / vision", W, "disabled by you")
else:
    add("Camera / vision", R if has("cv2") else W, "OpenCV present (camera not opened)" if has("cv2") else "OpenCV not installed")
import sqlite3
add("File search", R if cfg.get("file_index_enabled", True) else W,
    "SQLite index available" if cfg.get("file_index_enabled", True) else "index disabled - direct scan only")
try:
    import subprocess, tempfile, shutil as _sh
    scratch = tempfile.mkdtemp(prefix="isha_diag_")
    env = dict(os.environ, ISHA_SKIP_BACKEND="1", QT_QPA_PLATFORM="offscreen", ISHA_DATA_DIR=scratch)
    code = "import sys; sys.argv=['x']; import mani; print('TOOLS', len(mani.TOOL_REGISTRY))"
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=240)
    finally:
        _sh.rmtree(scratch, ignore_errors=True)
    line = [l for l in r.stdout.splitlines() if l.startswith("TOOLS")]
    add("Tools", R if line else M, (line[0].split()[1] + " tools registered; ISHA imports cleanly") if line else (r.stderr or r.stdout)[-300:])
except Exception as e:
    add("Tools", M, str(e))
if deep:
    try:
        from isha_core import llama_runtime as L, router
        reg = router.ModelRegistry(cfg, os.path.join(root, "models"), root)
        e = reg.get("general") or next((reg.get(r) for r in reg.available_roles()), None)
        Llama, mode, err = L.resolve_backend()
        if e is None: add("Model loading", M, "no model to load")
        elif Llama is None: add("Model loading", M, err)
        else:
            llm = Llama(model_path=str(e.path), n_ctx=512, verbose=False)
            res = llm.create_chat_completion(messages=[{"role": "user", "content": "Say OK"}], max_tokens=4)
            txt = res["choices"][0]["message"].get("content") or ""
            add("Model loading", R, f"{e.name} loaded via {mode}, replied: {txt.strip()[:20]!r}")
            try: llm.close()
            except Exception: pass
    except Exception as ex:
        add("Model loading", M, f"{type(ex).__name__}: {ex}")
else:
    add("Model loading", W, "not tested (quick check)")
print("DIAG" + json.dumps(out))
'''


def run(install_dir: Path, python_exe: str | None = None, deep: bool = False, timeout: int = 600) -> list:
    py = python_exe or sys.executable
    try:
        r = subprocess.run([py, "-c", PROBE, str(install_dir), "1" if deep else "0"], cwd=str(install_dir),
                           capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "PYTHONPATH": str(install_dir), "QT_QPA_PLATFORM":
                                os.environ.get("QT_QPA_PLATFORM", "offscreen")},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("DIAG")]
        if line:
            return json.loads(line[-1][4:])
        return [{"name": "Diagnostics", "status": "MISSING", "detail": (r.stderr or r.stdout)[-500:]}]
    except subprocess.TimeoutExpired:
        return [{"name": "Diagnostics", "status": "MISSING", "detail": "timed out"}]


def ready(results: list) -> bool:
    must = {"Python runtime", "Dependencies", "GUI", "Tools"}
    return all(r["status"] != "MISSING" for r in results if r["name"] in must)
