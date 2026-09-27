"""Resumable, verifying downloads.

* HTTPS only (plain http is allowed solely for 127.0.0.1 in tests).
* Writes to ``<name>.part`` plus a ``.part.json`` sidecar (url, expected size,
  sha256, ETag). A later run resumes with an HTTP Range request - validated
  with If-Range so a changed file restarts instead of producing garbage.
* SHA-256 is computed while streaming (the existing part is hashed first on
  resume). The final file only appears - via an atomic rename - after the
  size and checksum match. A mismatch deletes the part; nothing half-written
  is ever called "installed".
* pause() / resume() / cancel() from any thread; progress callback gets
  done, total, speed (bytes/s) and ETA (s).
* Disk space is checked before a single byte is downloaded.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20
UA = {"User-Agent": "ISHA-Installer/1.0"}


class DownloadError(Exception):
    def __init__(self, message: str, kind: str = "error", retryable: bool = True):
        super().__init__(message)
        self.kind = kind            # network | checksum | disk | cancelled | http | insecure
        self.retryable = retryable


@dataclass
class Progress:
    done: int
    total: int
    speed: float
    eta: float | None
    state: str                      # downloading | paused | verifying | done

    @property
    def percent(self) -> float:
        return (100.0 * self.done / self.total) if self.total else 0.0


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def check_url(url: str):
    p = urllib.parse.urlparse(url)
    if p.scheme == "https":
        return
    if p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost"):
        return
    raise DownloadError(f"Refusing non-HTTPS download: {url}", "insecure", retryable=False)


def free_space(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def sha256_file(path: Path, stop=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if stop is not None and stop():
                raise DownloadError("cancelled", "cancelled", retryable=True)
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def finalize_file(part: Path, dest: Path, expected_size: int | None, sha256: str | None,
                  digest: str | None = None) -> str:
    """Verify a completed temp file and atomically move it into place.
    Deletes the temp file on mismatch. Returns the SHA-256."""
    part, dest = Path(part), Path(dest)
    size = part.stat().st_size
    if expected_size and size != expected_size:
        part.unlink(missing_ok=True)
        raise DownloadError(f"Size mismatch: got {size} bytes, expected {expected_size}.", "checksum")
    digest = digest or sha256_file(part)
    if sha256 and digest != sha256.lower():
        part.unlink(missing_ok=True)
        raise DownloadError(f"Checksum mismatch (SHA-256 {digest[:12]}... != {sha256[:12]}...). "
                            f"The file was deleted.", "checksum")
    os.replace(part, dest)
    return digest


class Downloader:
    def __init__(self, url: str, dest: Path, expected_size: int | None = None,
                 sha256: str | None = None, progress=None, timeout: float = 30.0,
                 free_space_fn=free_space, retries: int = 3, headers: dict | None = None):
        check_url(url)
        self.url = url
        self.dest = Path(dest)
        self.part = self.dest.with_name(self.dest.name + ".part")
        self.meta = self.dest.with_name(self.dest.name + ".part.json")
        self.expected_size = int(expected_size) if expected_size else None
        self.sha256 = (sha256 or "").lower() or None
        self.progress = progress
        self.timeout = timeout
        self.retries = retries
        self.headers = dict(UA, **(headers or {}))
        self._free = free_space_fn
        self._run = threading.Event()
        self._run.set()
        self._cancel = threading.Event()
        self.state = "idle"

    # ---- control (thread-safe) ----
    def pause(self):
        self._run.clear()
        self.state = "paused"

    def resume(self):
        self._run.set()

    def cancel(self):
        self._cancel.set()
        self._run.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ---- helpers ----
    def _load_meta(self) -> dict:
        try:
            m = json.loads(self.meta.read_text(encoding="utf-8"))
            return m if m.get("url") == self.url else {}
        except Exception:
            return {}

    def _save_meta(self, etag: str, total: int):
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        self.meta.write_text(json.dumps({"url": self.url, "etag": etag, "total": total,
                                         "sha256": self.sha256}), encoding="utf-8")

    def _emit(self, done, total, speed, state):
        if self.progress:
            eta = ((total - done) / speed) if (speed > 0 and total) else None
            try:
                self.progress(Progress(done, total, speed, eta, state))
            except Exception:
                pass

    def _check_disk(self, remaining: int):
        need = int(remaining * 1.02) + 64 * 1024 * 1024
        avail = self._free(self.dest.parent)
        if avail < need:
            raise DownloadError(f"Not enough disk space: need {human_bytes(need)}, "
                                f"{human_bytes(avail)} available at {self.dest.parent}", "disk", retryable=False)

    def _discard(self):
        for p in (self.part, self.meta):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    # ---- main ----
    def run(self) -> Path:
        """Blocking. Returns the verified final path or raises DownloadError."""
        if self.dest.exists() and self._final_ok(self.dest):
            self._emit(self.dest.stat().st_size, self.dest.stat().st_size, 0, "done")
            return self.dest
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        if self.expected_size:
            have = self.part.stat().st_size if self.part.exists() else 0
            self._check_disk(self.expected_size - have)
        attempt = 0
        while True:
            try:
                self._transfer()
                break
            except DownloadError as e:
                if e.kind in ("cancelled", "disk", "checksum", "insecure", "http") or not e.retryable:
                    raise
                attempt += 1
                if attempt > self.retries:
                    raise
                if self._cancel.wait(min(2 ** attempt, 10)):
                    raise DownloadError("Download cancelled.", "cancelled")
        return self._finish()

    def _final_ok(self, path: Path) -> bool:
        if self.expected_size and path.stat().st_size != self.expected_size:
            return False
        if self.sha256:
            return sha256_file(path) == self.sha256
        return bool(self.expected_size)   # size-only when no checksum is published

    def _transfer(self):
        meta = self._load_meta()
        have = self.part.stat().st_size if self.part.exists() else 0
        if have and not meta:
            self._discard()           # orphan part from a different URL
            have = 0
        hasher = hashlib.sha256()
        if have:
            with open(self.part, "rb") as f:
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    hasher.update(b)
        headers = dict(self.headers)
        if have:
            headers["Range"] = f"bytes={have}-"
            if meta.get("etag"):
                headers["If-Range"] = meta["etag"]
        req = urllib.request.Request(self.url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=ssl.create_default_context())
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:                 # range not satisfiable: part is complete or stale
                if self.expected_size and have == self.expected_size:
                    self._hash_so_far = hasher.hexdigest()
                    return
                self._discard()
                raise DownloadError("Stale partial file discarded; retrying.", "network")
            raise DownloadError(f"HTTP {e.code} from server for {self.url}", "http",
                                retryable=e.code >= 500)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise DownloadError(f"Connection problem: {getattr(e, 'reason', e)}", "network")
        with resp:
            status = getattr(resp, "status", 200)
            if have and status == 200:                # server ignored Range or file changed
                have = 0
                hasher = hashlib.sha256()
                mode = "wb"
            else:
                mode = "ab" if have else "wb"
            length = int(resp.headers.get("Content-Length") or 0)
            total = (have + length) if length else (self.expected_size or 0)
            if self.expected_size and total and total != self.expected_size:
                raise DownloadError(f"Server reports {total} bytes but {self.expected_size} were expected.",
                                    "checksum", retryable=False)
            if total and not have:
                self._check_disk(total)
            etag = resp.headers.get("ETag") or ""
            self._save_meta(etag, total)
            self.state = "downloading"
            done = have
            t_last, d_last, speed = time.time(), done, 0.0
            with open(self.part, mode) as f:
                while True:
                    if not self._run.is_set():
                        self._emit(done, total, 0.0, "paused")
                        f.flush()
                        self._run.wait()
                        t_last, d_last = time.time(), done
                    if self._cancel.is_set():
                        raise DownloadError("Download cancelled.", "cancelled")
                    try:
                        chunk = resp.read(CHUNK)
                    except (OSError, TimeoutError) as e:
                        raise DownloadError(f"Connection interrupted: {e}", "network")
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - t_last >= 0.5:
                        inst = (done - d_last) / (now - t_last)
                        speed = inst if speed == 0 else 0.7 * speed + 0.3 * inst
                        t_last, d_last = now, done
                        self._emit(done, total, speed, "downloading")
            if total and done < total:
                raise DownloadError(f"Connection interrupted at {human_bytes(done)} of {human_bytes(total)}.",
                                    "network")
            self._hash_so_far = hasher.hexdigest()
            self._emit(done, total or done, speed, "verifying")

    def _finish(self) -> Path:
        self.state = "verifying"
        size = self.part.stat().st_size
        try:
            digest = finalize_file(self.part, self.dest, self.expected_size, self.sha256,
                                   getattr(self, "_hash_so_far", None))
        except DownloadError:
            self._discard()
            raise
        try:
            self.meta.unlink()
        except FileNotFoundError:
            pass
        self.state = "done"
        self.digest = digest
        self._emit(size, size, 0.0, "done")
        return self.dest

    def discard_partial(self):
        self._discard()
