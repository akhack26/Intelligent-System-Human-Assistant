"""llama.cpp runtime resolution — shared by mani.py and the ISHA installer.

Moved verbatim out of mani.py (where it was the "GGUF BACKEND RESOLVER"
block) so the installer can drive the *same* code without importing the
GUI. mani.py re-imports every name, so its behaviour is unchanged.

Three layers, none of which compiles anything:
  1. llama_cpp already importable            -> use it (native)
  2. prebuilt llama-cpp-python wheel         -> pip --only-binary=:all:
  3. official prebuilt llama-server binary   -> localhost HTTP shim (LlamaServer)

Additions for the installer: a persisted GPU variant (variant.txt), download
progress callbacks, offline archives, safe archive extraction, and a CLI:

    python -m isha_core.llama_runtime --setup [--variant vulkan] [--archive F] [--json]
"""
from __future__ import annotations

import atexit
import importlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

_OS_NAME = platform.system()
_IS_WINDOWS = _OS_NAME == "Windows"
_IS_LINUX = _OS_NAME == "Linux"
_IS_MACOS = _OS_NAME == "Darwin"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_DIR = PROJECT_ROOT / ".isha_runtime"


LLAMACPP_DIR = _RUNTIME_DIR / "llamacpp"          # prebuilt binaries live here
_STAMP = LLAMACPP_DIR / ".installed.json"


# llama-cpp-python ke apne prebuilt wheel indexes (PyPI par sirf sdist hota hai)
_WHEEL_INDEXES = [
    "https://abetlen.github.io/llama-cpp-python/whl/cpu",
    "https://abetlen.github.io/llama-cpp-python/whl/metal",
]

_GH_REPO = "https://github.com/ggml-org/llama.cpp"
_GH_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
_GH_DL = "https://github.com/ggml-org/llama.cpp/releases/download"
# Agar GitHub API rate-limit kare (60 req/hour without token), yeh known-good
# tags try kiye jaate hain. Inhe kabhi delete mat karna.
_FALLBACK_TAGS = ["b10964", "b6996", "b6591"]

_UA = {"User-Agent": "ISHA-GGUF-Backend/1.0"}

# Backend variant: cpu (default, sabse safe) | vulkan | cuda-12.4 | cuda-13.3 | hip-radeon
VARIANT_FILE = LLAMACPP_DIR / "variant.txt"      # written by the ISHA installer
_KNOWN_VARIANTS = ("cpu", "vulkan", "cuda-12.4", "cuda-13.3", "hip-radeon")


def _read_variant() -> str:
    env = (os.environ.get("ISHA_LLAMA_VARIANT") or "").strip().lower()
    if env:
        return env
    try:
        v = VARIANT_FILE.read_text(encoding="utf-8").strip().lower()
        return v if v in _KNOWN_VARIANTS else "cpu"
    except Exception:
        return "cpu"


_VARIANT = _read_variant()

DIAGNOSTIC = {
    "python": sys.executable,
    "python_version": platform.python_version(),
    "platform": f"{platform.system()} {platform.release()}",
    "architecture": platform.machine(),
    "mode": "none",          # "native" | "server" | "none"
    "installed": False,
    "error": "",
    "notes": [],
}


def _log(msg: str):
    print(f"[gguf] {msg}", flush=True)


def _note(msg: str):
    DIAGNOSTIC["notes"].append(msg)
    _log(msg)


# ======================================================================
# LAYER 1 + 2 — llama-cpp-python, but NEVER from source
# ======================================================================

def _try_import_llama():
    try:
        importlib.invalidate_caches()
        from llama_cpp import Llama  # noqa: F401
        return Llama, ""
    except Exception as e:                       # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _pip(args, timeout=1800):
    cmd = [sys.executable, "-m", "pip", "install",
           "--disable-pip-version-check", "--no-input"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return False, f"pip timed out after {timeout}s"
    except Exception as e:                       # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _try_prebuilt_wheel():
    """--only-binary=:all: ka matlab: pip ko source build karne ki IJAAZAT hi
    nahi hai. Wheel mila to install, nahi mila to 2 second me clean fail —
    CMake/nmake wali 5-minute crash story dobara kabhi nahi."""
    base = ["--only-binary=:all:", "--upgrade-strategy", "only-if-needed"]
    for index in _WHEEL_INDEXES:
        ok, out = _pip(base + ["--extra-index-url", index, "llama-cpp-python"], timeout=900)
        if ok:
            Llama, err = _try_import_llama()
            if Llama is not None:
                return Llama, ""
            _note(f"wheel install hua par import fail: {err}")
        else:
            tail = out.strip().splitlines()[-3:] if out else []
            _note(f"prebuilt wheel nahi mila ({index.rsplit('/', 1)[-1]}): "
                  + " | ".join(t.strip() for t in tail))
    return None, "koi prebuilt llama-cpp-python wheel is Python version ke liye available nahi"


# ======================================================================
# LAYER 3 — official llama.cpp prebuilt binary + HTTP shim
# ======================================================================

def _asset_suffix():
    """Is machine ke liye llama.cpp release asset ka naam-suffix."""
    m = platform.machine().lower()
    arm = m in ("arm64", "aarch64")
    if _IS_WINDOWS:
        if _VARIANT != "cpu" and not arm:
            return f"bin-win-{_VARIANT}-x64.zip"
        return "bin-win-cpu-arm64.zip" if arm else "bin-win-cpu-x64.zip"
    if _IS_MACOS:
        return "bin-macos-arm64.tar.gz" if arm else "bin-macos-x64.tar.gz"
    # Linux
    if _VARIANT == "vulkan":
        return "bin-ubuntu-vulkan-arm64.tar.gz" if arm else "bin-ubuntu-vulkan-x64.tar.gz"
    return "bin-ubuntu-arm64.tar.gz" if arm else "bin-ubuntu-x64.tar.gz"


def _http_json(url, timeout=30):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_text(url, timeout=30):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _assets_of(tag):
    """Ek release tag ke asset naam — GitHub ke public HTML se, API se nahi.
    (API par 60 req/hour ka rate limit hai; HTML par nahi.)"""
    try:
        html = _http_text(f"{_GH_REPO}/releases/expanded_assets/{tag}")
    except Exception:                            # noqa: BLE001
        return []
    return sorted(set(re.findall(r'/releases/download/[^"]+?/([^"/]+)"', html)))


def _latest_tag():
    """Latest llama.cpp build tag. /releases/latest ek stable version tag par
    redirect karta hai (e.g. v0.4.1); uske andar 'nightly-tag.txt' hota hai
    jisme actual build tag (bNNNNN) likha hota hai."""
    tags = []
    try:
        req = urllib.request.Request(f"{_GH_REPO}/releases/latest", headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            tags.append(r.geturl().rstrip("/").rsplit("/", 1)[-1])
    except Exception:                            # noqa: BLE001
        pass
    for t in list(tags):
        if "nightly-tag.txt" in _assets_of(t):
            try:
                tags.insert(0, _http_text(f"{_GH_DL}/{t}/nightly-tag.txt").strip())
            except Exception:                    # noqa: BLE001
                pass
    return tags


def _find_asset_url():
    """(url, tag) — pehla release jisme is machine ka asset mile."""
    suffix = _asset_suffix()
    for tag in _latest_tag() + _FALLBACK_TAGS:
        if not tag:
            continue
        for name in _assets_of(tag):
            if name.startswith("llama-") and name.endswith(suffix):
                return f"{_GH_DL}/{tag}/{name}", tag
    # last resort: GitHub API (rate-limited, isliye sabse aakhir me)
    try:
        for rel in _http_json(f"{_GH_API}?per_page=10"):
            tag = rel.get("tag_name") or ""
            for a in rel.get("assets") or []:
                n = a.get("name") or ""
                if n.startswith("llama-") and n.endswith(suffix):
                    return a.get("browser_download_url"), tag
    except Exception:                            # noqa: BLE001
        pass
    return None, None


def _server_exe():
    """Installed llama-server dhoondo. (path, needs_serve_subcommand)"""
    if not LLAMACPP_DIR.is_dir():
        return None, False
    names_server = ("llama-server.exe", "llama-server")
    names_cli = ("llama.exe", "llama")
    found_cli = None
    for p in LLAMACPP_DIR.rglob("*"):
        if not p.is_file():
            continue
        n = p.name.lower()
        if n in names_server:
            return p, False
        if n in names_cli and found_cli is None:
            found_cli = p
    if found_cli is not None:
        return found_cli, True          # naye builds: `llama serve`
    return None, False


def _download(url, dest: Path, timeout=900, progress=None):
    _log(f"downloading {url.rsplit('/', 1)[-1]} ...")
    req = urllib.request.Request(url, headers=_UA)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and time.time() - last > 1.0:
                last = time.time()
                _log(f"  {done / 1e6:.1f} / {total / 1e6:.1f} MB")
                if progress:
                    try:
                        progress(done, total)
                    except Exception:            # noqa: BLE001
                        pass
    tmp.replace(dest)
    return dest


def _install_llamacpp(local_archive=None, progress=None):
    """Prebuilt llama.cpp binary download + extract. Ek baar; phir cached.

    local_archive: an already-downloaded official release archive (offline
    installer). progress(done_bytes, total_bytes) is called while downloading.
    """
    exe, _ = _server_exe()
    if exe is not None:
        return exe

    LLAMACPP_DIR.mkdir(parents=True, exist_ok=True)
    if local_archive:
        src = Path(local_archive)
        if not src.is_file() or not src.name.startswith("llama-"):
            raise RuntimeError(f"Offline llama.cpp archive invalid: {src}")
        archive = LLAMACPP_DIR / src.name
        shutil.copy2(src, archive)
        tag = src.name.split("-")[1] if "-" in src.name else "offline"
    else:
        url, tag = _find_asset_url()
        if not url:
            raise RuntimeError(
                "llama.cpp ka prebuilt binary nahi mila (internet/GitHub reachable nahi?). "
                "Manually download karo: https://github.com/ggml-org/llama.cpp/releases "
                f"— asset '*{_asset_suffix()}' — aur usko extract karo yahan:\n  {LLAMACPP_DIR}")
        archive = LLAMACPP_DIR / url.rsplit("/", 1)[-1]
        _download(url, archive, progress=progress)

    _log("extracting ...")
    base = LLAMACPP_DIR.resolve()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                if not (base / n).resolve().is_relative_to(base):
                    raise RuntimeError(f"unsafe path in archive: {n}")
            z.extractall(LLAMACPP_DIR)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                if not (base / m.name).resolve().is_relative_to(base) or m.issym() and \
                        not (base / m.name).parent.joinpath(m.linkname).resolve().is_relative_to(base):
                    raise RuntimeError(f"unsafe path in archive: {m.name}")
            t.extractall(LLAMACPP_DIR)
    try:
        archive.unlink()
    except Exception:                            # noqa: BLE001
        pass

    if not _IS_WINDOWS:
        for p in LLAMACPP_DIR.rglob("*"):
            if p.is_file() and (p.suffix == "" or p.suffix == ".so" or ".so." in p.name):
                try:
                    p.chmod(p.stat().st_mode | 0o755)
                except Exception:                # noqa: BLE001
                    pass

    exe, _ = _server_exe()
    if exe is None:
        raise RuntimeError(f"Download to hua par llama-server binary nahi mila andar: {LLAMACPP_DIR}")

    try:
        _STAMP.write_text(json.dumps({"tag": tag, "variant": _VARIANT,
                                      "installed": time.time()}), encoding="utf-8")
    except Exception:                            # noqa: BLE001
        pass
    _log(f"llama.cpp {tag} ready: {exe}")
    return exe


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_LIVE_SERVERS = []


def _kill(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:                            # noqa: BLE001
        try:
            proc.kill()
        except Exception:                        # noqa: BLE001
            pass


@atexit.register
def _kill_all():
    for ref in list(_LIVE_SERVERS):
        _kill(ref)
    _LIVE_SERVERS.clear()


class LlamaServer:
    """llama-cpp-python ke `Llama` ka drop-in replacement.

    Andar se yeh llama.cpp ka official `llama-server` binary localhost par
    chalata hai aur uske OpenAI-compatible endpoint se baat karta hai.
    Bahar se signature aur return shapes bilkul same hain, isliye ISHA ka
    baaki code change nahi hota.
    """

    START_TIMEOUT = 900          # bade models ko load hone do

    def __init__(self, model_path, n_ctx=4096, n_threads=None, n_batch=512,
                 n_gpu_layers=0, verbose=False, chat_format=None, **_ignored):
        self.model_path = str(model_path)
        self.n_ctx = int(n_ctx)
        self.verbose = bool(verbose)
        self._proc = None
        self._port = None
        self._closed = False
        self._lock = threading.Lock()
        self.supports_tools = False

        exe, needs_serve = _server_exe()
        if exe is None:
            exe = _install_llamacpp()
            _, needs_serve = _server_exe()
        self._exe, self._needs_serve = exe, needs_serve

        self._args = [
            "-m", self.model_path,
            "-c", str(self.n_ctx),
            "-b", str(int(n_batch)),
            "-ngl", str(int(n_gpu_layers)),
            "--host", "127.0.0.1",
        ]
        if n_threads:
            self._args += ["-t", str(int(n_threads))]

        # --jinja = model ka apna chat template + native tool-calling.
        # Bahut purane builds me yeh flag nahi hota, isliye bina-jinja retry.
        last_err = ""
        for extra in (["--jinja", "--no-webui"], ["--jinja"],
                      ["--no-jinja", "--no-webui"], []):
            try:
                self._start(extra)
                return
            except Exception as e:               # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                _kill(self._proc)
                self._proc = None
        raise RuntimeError(f"llama-server start nahi hua: {last_err}")

    # ---- process ----
    def _start(self, extra):
        self._port = _free_port()
        cmd = [str(self._exe)] + (["serve"] if self._needs_serve else []) \
            + self._args + ["--port", str(self._port)] + list(extra)
        self._spawn(cmd)
        self._wait_ready()
        self.supports_tools = "--jinja" in extra
        self._extra = list(extra)
        _LIVE_SERVERS.append(self._proc)
        _log(f"llama-server up on 127.0.0.1:{self._port} "
             f"({Path(self.model_path).name}, ctx={self.n_ctx}"
             f"{', jinja' if self.supports_tools else ''})")

    def _restart_plain(self):
        """Jinja/tool-parsing mode me model ka output reject ho raha hai to
        server ko plain mode me dobara uthao. Ek baar hi hota hai, phir yaad
        rehta hai — isliye user ko sirf ek dheema jawab dikhta hai, error nahi."""
        _log("jinja/tool mode me model ka output parse nahi hua — "
             "plain mode me restart kar raha hoon")
        try:
            _LIVE_SERVERS.remove(self._proc)
        except ValueError:
            pass
        _kill(self._proc)
        self._proc = None
        # NOTE: naye llama.cpp builds me --jinja DEFAULT ON hai, isliye
        # "koi flag nahi" ka matlab plain mode nahi hota — --no-jinja
        # explicitly dena padta hai. Purane builds wo flag nahi jaante,
        # isliye khaali list fallback me rehti hai.
        last = ""
        for extra in (["--no-jinja", "--no-webui"], ["--no-jinja"], []):
            try:
                self._start(extra)
                return
            except Exception as e:               # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                _kill(self._proc)
                self._proc = None
        raise RuntimeError(f"plain mode restart fail: {last}")

    def _spawn(self, cmd):
        kwargs = {}
        if _IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000 | 0x00000200   # NO_WINDOW | NEW_PROC_GROUP
        self._proc = subprocess.Popen(
            cmd,
            stdout=(None if self.verbose else subprocess.DEVNULL),
            stderr=(None if self.verbose else subprocess.DEVNULL),
            cwd=str(Path(cmd[0]).parent),
            **kwargs,
        )

    def _wait_ready(self):
        url = f"http://127.0.0.1:{self._port}/health"
        deadline = time.time() + self.START_TIMEOUT
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"server process exit ho gaya (code {self._proc.returncode})")
            try:
                req = urllib.request.Request(url, headers=_UA)
                with urllib.request.urlopen(req, timeout=5) as r:
                    if r.status == 200:
                        return
            except urllib.error.HTTPError as e:
                if e.code == 503:        # abhi model load ho raha hai
                    pass
            except Exception:            # noqa: BLE001
                pass
            time.sleep(0.4)
        raise RuntimeError(f"server {self.START_TIMEOUT}s me ready nahi hua")

    # ---- the one method ISHA actually calls ----
    _PARSE_FAIL = ("peg-native", "does not match the expected",
                   "failed to parse", "common_chat_parse")

    def _is_parse_fail(self, msg: str) -> bool:
        m = (msg or "").lower()
        return self.supports_tools and any(k in m for k in self._PARSE_FAIL)

    def create_chat_completion(self, messages, stream=False, **kw):
        """llama-cpp-python ke Llama.create_chat_completion jaisa hi.

        Agar jinja/tool-parsing mode me model ka output reject ho jaaye, to
        server ko ek baar plain mode me restart karke wahi request dobara
        bheji jaati hai — user ko error nahi, jawab milta hai."""
        if stream:
            return self._stream_with_retry(messages, kw)
        try:
            return self._call(messages, False, kw)
        except RuntimeError as e:
            if not self._is_parse_fail(str(e)):
                raise
            self._restart_plain()
            kw.pop("tools", None); kw.pop("tool_choice", None)
            return self._call(messages, False, kw)

    def _stream_with_retry(self, messages, kw):
        """Parse-fail stream ke beech me bhi aa sakta hai. Jab tak koi asli
        content bahar nahi gaya, restart karke poora stream dobara chalana
        safe hai — user ko sirf thoda rukna dikhta hai, error nahi."""
        emitted = False
        try:
            for chunk in self._call(messages, True, kw):
                try:
                    if chunk["choices"][0]["delta"].get("content"):
                        emitted = True
                except (KeyError, IndexError, TypeError):
                    pass
                yield chunk
            return
        except RuntimeError as e:
            if emitted or not self._is_parse_fail(str(e)):
                raise
        self._restart_plain()
        kw.pop("tools", None); kw.pop("tool_choice", None)
        for chunk in self._call(messages, True, kw):
            yield chunk

    def _call(self, messages, stream, kw):
        body = {"messages": messages, "stream": bool(stream)}
        for k in ("temperature", "top_p", "top_k", "min_p", "repeat_penalty",
                  "presence_penalty", "frequency_penalty", "max_tokens",
                  "stop", "seed", "response_format", "tools", "tool_choice"):
            if k in kw and kw[k] is not None:
                body[k] = kw[k]
        if not getattr(self, "supports_tools", True):
            body.pop("tools", None)
            body.pop("tool_choice", None)

        url = f"http://127.0.0.1:{self._port}/v1/chat/completions"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={**_UA, "Content-Type": "application/json",
                     "Accept": "text/event-stream" if stream else "application/json"})

        if not stream:
            with self._lock:
                try:
                    with urllib.request.urlopen(req, timeout=3600) as r:
                        return json.loads(r.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    raise RuntimeError(self._err(e)) from None
        return self._sse(req)

    @staticmethod
    def _err(e):
        """llama-server ka asli error message nikalo, bare 'HTTP 400' nahi."""
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            msg = (body.get("error") or {}).get("message") or json.dumps(body)[:400]
        except Exception:                        # noqa: BLE001
            msg = ""
        return f"llama-server HTTP {e.code}" + (f": {msg}" if msg else "")

    def _sse(self, req):
        """SSE stream -> wahi chunk dicts jo llama-cpp-python deta hai."""
        self._lock.acquire()
        resp = None
        try:
            try:
                resp = urllib.request.urlopen(req, timeout=3600)
            except urllib.error.HTTPError as e:
                raise RuntimeError(self._err(e)) from None
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                # llama-server mid-stream errors HTTP 200 ke andar
                # `data: {"error": {...}}` ke roop me bhejta hai. Inhe chup-chaap
                # nigal liya to caller ko khaali reply dikhta hai aur koi wajah
                # nahi — isliye inhe asli exception banao.
                if isinstance(chunk, dict) and chunk.get("error"):
                    err = chunk["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"llama-server: {msg}")
                yield chunk
        finally:
            try:
                if resp is not None:
                    resp.close()
            except Exception:                    # noqa: BLE001
                pass
            self._lock.release()

    # ---- teardown ----
    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            _LIVE_SERVERS.remove(self._proc)
        except ValueError:
            pass
        _kill(self._proc)
        self._proc = None

    def __del__(self):
        try:
            self.close()
        except Exception:                        # noqa: BLE001
            pass


# ======================================================================
# Public resolver
# ======================================================================

_RESOLVED = None


def resolve_backend(force: str = None):
    """(LlamaClass, mode, error). mode = 'native' | 'server' | 'none'.

    force: None (auto) | 'native' | 'server'  (ya env ISHA_LLAMA_BACKEND)
    """
    global _RESOLVED
    if _RESOLVED is not None and force is None:
        return _RESOLVED
    if os.environ.get("ISHA_SKIP_BACKEND") == "1":
        # Headless tests / "no model yet" setups: do not pip-install or
        # download llama.cpp. Diagnostics will honestly report UNAVAILABLE.
        DIAGNOSTIC.update(mode="none", installed=False, error="skipped (ISHA_SKIP_BACKEND=1)")
        _RESOLVED = (None, "none", "skipped (ISHA_SKIP_BACKEND=1)")
        return _RESOLVED

    force = (force or os.environ.get("ISHA_LLAMA_BACKEND") or "").strip().lower() or None
    if force is None and _VARIANT != "cpu":
        force = "server"          # GPU builds only exist as llama-server binaries

    if force != "server":
        # LAYER 1
        Llama, err = _try_import_llama()
        if Llama is not None:
            DIAGNOSTIC.update(mode="native", installed=True, error="")
            _log("backend: llama-cpp-python (already installed)")
            _RESOLVED = (Llama, "native", "")
            return _RESOLVED
        _note(f"llama_cpp import nahi hua: {err}")

        # An installer-provisioned (or earlier downloaded) llama-server is already
        # here: use it instead of trying pip over the network on every launch.
        if force is None and _server_exe()[0] is not None:
            force = "server"

    if force != "server":
        # LAYER 2
        _log("prebuilt wheel dhoondh raha hoon (source build BILKUL nahi karega)...")
        Llama, err2 = _try_prebuilt_wheel()
        if Llama is not None:
            DIAGNOSTIC.update(mode="native", installed=True, error="")
            _log("backend: llama-cpp-python (prebuilt wheel installed)")
            _RESOLVED = (Llama, "native", "")
            return _RESOLVED
        _note(err2)

        if force == "native":
            DIAGNOSTIC.update(mode="none", installed=False, error=err2)
            _RESOLVED = (None, "none", err2)
            return _RESOLVED

    # LAYER 3 — yeh hamesha chalna chahiye
    try:
        _log("llama.cpp prebuilt server binary par switch kar raha hoon "
             "(koi compiler, koi Python-version dependency nahi)...")
        _install_llamacpp()
        DIAGNOSTIC.update(mode="server", installed=True, error="")
        _log("backend: llama.cpp server (prebuilt binary)")
        _RESOLVED = (LlamaServer, "server", "")
        return _RESOLVED
    except Exception as e:                       # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        DIAGNOSTIC.update(mode="none", installed=False, error=err)
        _log(f"llama.cpp server setup fail: {err}")
        _RESOLVED = (None, "none", err)
        return _RESOLVED


def ensure_backend() -> bool:
    return resolve_backend()[0] is not None


def status_text() -> str:
    d = DIAGNOSTIC
    lines = [
        f"Runtime (Python):\n{d['python']}",
        f"Python version:\n{d['python_version']}",
        f"Platform:\n{d['platform']}",
        f"Architecture:\n{d['architecture']}",
        f"Backend mode:\n{d['mode']}",
        f"Status:\n{'OK' if d['installed'] else 'FAILED'}",
    ]
    if d["error"]:
        lines.append(f"Reason:\n{d['error']}")
    if d["notes"]:
        lines.append("Notes:\n  " + "\n  ".join(d["notes"][-6:]))
    return "\n\n".join(lines)


# ----------------------------------------------------------------------
# Installer entry point
# ----------------------------------------------------------------------
def installed_info() -> dict:
    exe, _ = _server_exe()
    stamp = {}
    try:
        stamp = json.loads(_STAMP.read_text(encoding="utf-8"))
    except Exception:
        pass
    native, _err = _try_import_llama()
    return {"server_exe": str(exe) if exe else "", "native": native is not None,
            "variant": _VARIANT, "tag": stamp.get("tag", ""), "dir": str(LLAMACPP_DIR)}


def setup(variant: str = "cpu", archive: str | None = None, emit=print) -> dict:
    """Install the runtime for `variant` (with CPU fallback). Never raises."""
    global _VARIANT
    variant = (variant or "cpu").strip().lower()
    if variant not in _KNOWN_VARIANTS:
        variant = "cpu"
    LLAMACPP_DIR.mkdir(parents=True, exist_ok=True)
    tried = []
    for v in ([variant, "cpu"] if variant != "cpu" else ["cpu"]):
        _VARIANT = v
        emit({"stage": "llama", "message": f"llama.cpp ({v}) setup..."})
        try:
            exe = _install_llamacpp(local_archive=archive if v == variant else None,
                                    progress=lambda d, t: emit({"stage": "llama", "done": d, "total": t}))
            VARIANT_FILE.write_text(v, encoding="utf-8")
            info = installed_info()
            info.update(success=True, variant=v, fallback=(v != variant), exe=str(exe), tried=tried)
            return info
        except Exception as e:  # noqa: BLE001
            tried.append(f"{v}: {type(e).__name__}: {e}")
            # a half-extracted GPU build must not be picked up later
            if v != "cpu":
                for p in LLAMACPP_DIR.iterdir():
                    if p.name != VARIANT_FILE.name:
                        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    return {"success": False, "tried": tried, "variant": variant, **installed_info()}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Set up ISHA's llama.cpp runtime")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--info", action="store_true")
    ap.add_argument("--variant", default="cpu")
    ap.add_argument("--archive", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = (lambda d: print(json.dumps(d), flush=True)) if a.json else (lambda d: print(d, flush=True))
    if a.info:
        out({"final": True, **installed_info()})
    elif a.setup:
        out({"final": True, **setup(a.variant, a.archive, emit=out)})
