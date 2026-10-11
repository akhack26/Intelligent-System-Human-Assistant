"""Local file-search engine: an optional SQLite index + a bounded fallback scan.

Why an index: "saari MP4 files dhundo" over a whole home folder takes tens of
seconds as a recursive walk, every single time. With the index it is one
SQL query.

Incremental updates: every directory's mtime is stored. A directory whose
mtime has not changed since the last pass has had no entries added, removed
or renamed, so its file rows are kept as-is without re-statting each file.
A ``full`` refresh ignores that shortcut (catches in-place content edits).

Privacy: the database is a local file (``isha_file_index.db``). Nothing is
uploaded anywhere. Excluded folders are never entered.
"""
from __future__ import annotations

import fnmatch
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MEDIA_TYPES = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".tiff", ".tif", ".heic",
              ".ico", ".raw", ".cr2", ".nef"},
    "video": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".mpeg", ".mpg"},
    "audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus", ".amr"},
    "pdf": {".pdf"},
    "document": {".doc", ".docx", ".txt", ".md", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".rtf",
                 ".odt", ".ods", ".odp", ".epub", ".pdf"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".json", ".java", ".c",
             ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".kt",
             ".swift", ".sql", ".yaml", ".yml", ".toml", ".ipynb", ".lua", ".dart"},
    "application": {".exe", ".msi", ".appimage", ".deb", ".rpm", ".lnk", ".desktop", ".apk", ".dmg"},
}

DEFAULT_EXCLUDES = ["node_modules", "__pycache__", ".git", ".svn", ".hg", "venv", ".venv", "env",
                    ".isha_runtime", "site-packages", "$Recycle.Bin", "System Volume Information",
                    "AppData", ".cache", ".local/share/Trash", "Library/Caches", ".npm", ".cargo",
                    ".rustup", ".gradle", ".m2", "snap"]


def classify(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext == ".pdf":
        return "pdf"
    for kind, exts in MEDIA_TYPES.items():
        if kind in ("pdf",):
            continue
        if ext in exts:
            return kind
    return "other"


def media_extensions(kind: str) -> set:
    kind = (kind or "").lower()
    if kind == "document":
        return set(MEDIA_TYPES["document"])
    return set(MEDIA_TYPES.get(kind, set()))


@dataclass
class SearchQuery:
    name_terms: list = field(default_factory=list)   # all must appear in the filename
    glob: str = ""                                    # optional fnmatch pattern
    extensions: list = field(default_factory=list)   # [".pdf", ".docx"]
    media_type: str = ""                              # image/video/audio/pdf/document/...
    min_size: int | None = None                       # bytes
    max_size: int | None = None
    modified_after: float | None = None               # epoch seconds
    modified_before: float | None = None
    locations: list = field(default_factory=list)     # absolute folder paths
    kind: str = "file"                                # file | dir | any
    sort: str = "recent"                              # recent | size | name | oldest
    limit: int = 30

    def describe(self) -> str:
        bits = []
        if self.name_terms:
            bits.append("name~" + " ".join(self.name_terms))
        if self.glob:
            bits.append(f"glob={self.glob}")
        if self.extensions:
            bits.append("ext=" + ",".join(self.extensions))
        if self.media_type:
            bits.append(f"type={self.media_type}")
        if self.min_size:
            bits.append(f">{self.min_size / 1048576:.0f}MB")
        if self.max_size:
            bits.append(f"<{self.max_size / 1048576:.0f}MB")
        if self.modified_after:
            bits.append("after " + datetime.fromtimestamp(self.modified_after).strftime("%Y-%m-%d"))
        if self.modified_before:
            bits.append("before " + datetime.fromtimestamp(self.modified_before).strftime("%Y-%m-%d"))
        if self.locations:
            bits.append("in " + ", ".join(Path(p).name or str(p) for p in self.locations))
        if self.kind != "file":
            bits.append(f"kind={self.kind}")
        return " ".join(bits) or "everything"


def _ext_ok(name: str, q: SearchQuery) -> bool:
    ext = os.path.splitext(name)[1].lower()
    if q.extensions and ext not in {e.lower() for e in q.extensions}:
        return False
    if q.media_type:
        allowed = media_extensions(q.media_type)
        if allowed and ext not in allowed:
            return False
    return True


def _matches(name: str, is_dir: bool, size: int, mtime: float, path: str, q: SearchQuery) -> bool:
    if q.kind == "file" and is_dir:
        return False
    if q.kind == "dir" and not is_dir:
        return False
    low = name.lower()
    if any(t.lower() not in low for t in q.name_terms):
        return False
    if q.glob and not fnmatch.fnmatch(low, q.glob.lower()):
        return False
    if not is_dir and not _ext_ok(name, q):
        return False
    if q.min_size is not None and size < q.min_size:
        return False
    if q.max_size is not None and size > q.max_size:
        return False
    if q.modified_after is not None and mtime < q.modified_after:
        return False
    if q.modified_before is not None and mtime > q.modified_before:
        return False
    if q.locations:
        lp = os.path.normcase(path)
        if not any(lp.startswith(os.path.normcase(str(loc)).rstrip("\\/") + os.sep) for loc in q.locations):
            return False
    return True


def _row(path, name, is_dir, size, mtime, ctime=None) -> dict:
    return {"path": path, "name": name, "is_dir": bool(is_dir), "size": int(size or 0),
            "size_mb": round((size or 0) / 1048576, 2), "mtime": mtime,
            "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
            "type": "folder" if is_dir else classify(name)}


def _sort(rows: list, how: str) -> list:
    if how == "size":
        return sorted(rows, key=lambda r: r["size"], reverse=True)
    if how == "name":
        return sorted(rows, key=lambda r: r["name"].lower())
    if how == "oldest":
        return sorted(rows, key=lambda r: r["mtime"] or 0)
    return sorted(rows, key=lambda r: r["mtime"] or 0, reverse=True)


class FileIndex:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, name TEXT, name_lower TEXT, ext TEXT, size INTEGER,
        ctime REAL, mtime REAL, media_type TEXT, is_dir INTEGER, parent TEXT, gen INTEGER);
    CREATE INDEX IF NOT EXISTS ix_name ON files(name_lower);
    CREATE INDEX IF NOT EXISTS ix_ext ON files(ext);
    CREATE INDEX IF NOT EXISTS ix_media ON files(media_type);
    CREATE INDEX IF NOT EXISTS ix_mtime ON files(mtime);
    CREATE INDEX IF NOT EXISTS ix_parent ON files(parent);
    CREATE TABLE IF NOT EXISTS dirs(path TEXT PRIMARY KEY, mtime REAL, gen INTEGER);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """

    def __init__(self, db_path: Path, roots: list, excludes: list | None = None,
                 max_entries: int = 1_500_000):
        self.db_path = Path(db_path)
        self.roots = [str(Path(r)) for r in roots]
        self.excludes = list(excludes if excludes is not None else DEFAULT_EXCLUDES)
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.state = "idle"          # idle | indexing | paused | error
        self.progress = 0
        self.last_error = ""

    # ---------------- meta ----------------
    def _meta(self, key, default=None):
        with self._lock:
            r = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def _set_meta(self, key, value):
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    @property
    def ready(self) -> bool:
        return self._meta("last_complete") is not None and self.count() > 0

    def status(self) -> dict:
        last = self._meta("last_complete")
        return {"state": self.state, "entries": self.count(), "ready": self.ready,
                "last_complete": (datetime.fromtimestamp(float(last)).strftime("%Y-%m-%d %H:%M")
                                  if last else None),
                "last_duration_s": self._meta("last_duration"),
                "progress": self.progress, "roots": self.roots, "excludes": self.excludes,
                "paused": self._pause.is_set(), "db": str(self.db_path), "error": self.last_error}

    # ---------------- exclusion ----------------
    def is_excluded(self, path: str, name: str) -> bool:
        if name.startswith(".") and name not in (".",):
            return True
        norm = path.replace("\\", "/")
        for ex in self.excludes:
            ex_n = ex.replace("\\", "/").strip("/")
            if not ex_n:
                continue
            if "/" in ex_n:
                if norm.endswith("/" + ex_n) or ("/" + ex_n + "/") in norm + "/":
                    return True
            elif name.lower() == ex_n.lower():
                return True
        return False

    # ---------------- indexing ----------------
    def update(self, full: bool = False, time_budget_s: float | None = None) -> dict:
        """Synchronous (in the calling thread) incremental or full pass."""
        t0 = time.time()
        with self._lock:
            gen = int(self._meta("gen", "0")) + 1
        self.state, self.progress, self.last_error = "indexing", 0, ""
        added = changed = 0
        batch = []
        stack = [r for r in self.roots if os.path.isdir(r)]
        seen = 0
        try:
            while stack:
                if self._stop.is_set():
                    break
                while self._pause.is_set() and not self._stop.is_set():
                    self.state = "paused"
                    time.sleep(0.2)
                self.state = "indexing"
                if time_budget_s and time.time() - t0 > time_budget_s:
                    break
                d = stack.pop()
                try:
                    dst = os.stat(d)
                except OSError:
                    continue
                with self._lock:
                    prev = self._conn.execute("SELECT mtime FROM dirs WHERE path=?", (d,)).fetchone()
                unchanged = (not full) and prev is not None and abs(prev[0] - dst.st_mtime) < 1e-6
                try:
                    entries = list(os.scandir(d))
                except (PermissionError, OSError):
                    continue
                if unchanged:
                    # keep file rows, just descend and mark them seen in this generation
                    with self._lock:
                        self._conn.execute("UPDATE files SET gen=? WHERE parent=?", (gen, d))
                        self._conn.execute("UPDATE dirs SET gen=? WHERE path=?", (gen, d))
                    for e in entries:
                        try:
                            if e.is_dir(follow_symlinks=False) and not self.is_excluded(e.path, e.name):
                                stack.append(e.path)
                        except OSError:
                            continue
                    seen += len(entries)
                    self.progress = seen
                    continue
                for e in entries:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        if not is_dir and not e.is_file(follow_symlinks=False):
                            continue
                        if self.is_excluded(e.path, e.name):
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    seen += 1
                    if seen > self.max_entries:
                        stack.clear()
                        break
                    ext = "" if is_dir else os.path.splitext(e.name)[1].lower()
                    batch.append((e.path, e.name, e.name.lower(), ext, 0 if is_dir else st.st_size,
                                  getattr(st, "st_ctime", 0.0), st.st_mtime,
                                  "folder" if is_dir else classify(e.name), int(is_dir), d, gen))
                    if is_dir:
                        stack.append(e.path)
                with self._lock:
                    self._conn.execute("INSERT OR REPLACE INTO dirs(path,mtime,gen) VALUES(?,?,?)",
                                       (d, dst.st_mtime, gen))
                if len(batch) >= 2000:
                    a, c = self._flush(batch)
                    added, changed = added + a, changed + c
                    batch = []
                self.progress = seen
            a, c = self._flush(batch)
            added, changed = added + a, changed + c
            completed = not stack and not self._stop.is_set()
            removed = 0
            if completed:
                with self._lock:
                    cur = self._conn.execute("DELETE FROM files WHERE gen<?", (gen,))
                    removed = cur.rowcount
                    self._conn.execute("DELETE FROM dirs WHERE gen<?", (gen,))
                    self._conn.commit()
                self._set_meta("gen", gen)
                self._set_meta("last_complete", time.time())
                self._set_meta("last_duration", round(time.time() - t0, 1))
            self.state = "idle"
            return {"success": True, "completed": completed, "added_or_updated": added + changed,
                    "removed": removed, "entries": self.count(),
                    "seconds": round(time.time() - t0, 2)}
        except Exception as e:  # pragma: no cover - defensive
            self.state, self.last_error = "error", f"{type(e).__name__}: {e}"
            return {"success": False, "error": self.last_error}

    def _flush(self, batch):
        if not batch:
            return 0, 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO files(path,name,name_lower,ext,size,ctime,mtime,media_type,"
                "is_dir,parent,gen) VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
            self._conn.commit()
        return len(batch), 0

    def start_background(self, full: bool = False, delay_s: float = 0.0):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()

        def run():
            if delay_s and self._stop.wait(delay_s):
                return
            self.update(full=full)

        self._thread = threading.Thread(target=run, daemon=True, name="isha-file-index")
        self._thread.start()
        return True

    def pause(self):
        self._pause.set()
        if self.state == "indexing":
            self.state = "paused"

    def resume(self):
        self._pause.clear()

    def stop(self):
        self._stop.set()
        self._pause.clear()

    def rebuild(self, background: bool = True):
        self.stop()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self._conn.execute("DELETE FROM files")
            self._conn.execute("DELETE FROM dirs")
            self._conn.execute("DELETE FROM meta")
            self._conn.commit()
        self._stop.clear()
        if background:
            return self.start_background(full=True)
        return self.update(full=True)

    def close(self):
        self.stop()
        try:
            self._conn.close()
        except Exception:
            pass

    # ---------------- search ----------------
    def search(self, q: SearchQuery) -> list:
        where, params = [], []
        if q.kind == "file":
            where.append("is_dir=0")
        elif q.kind == "dir":
            where.append("is_dir=1")
        for t in q.name_terms:
            where.append("name_lower LIKE ? ESCAPE '\\'")
            params.append("%" + t.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        exts = {e.lower() for e in q.extensions}
        if q.media_type:
            m = media_extensions(q.media_type)
            exts = (exts & m) if exts else m
        if exts:
            where.append("(is_dir=1 OR ext IN (%s))" % ",".join("?" * len(exts)))
            params.extend(sorted(exts))
        if q.min_size is not None:
            where.append("size>=?"); params.append(int(q.min_size))
        if q.max_size is not None:
            where.append("size<=?"); params.append(int(q.max_size))
        if q.modified_after is not None:
            where.append("mtime>=?"); params.append(float(q.modified_after))
        if q.modified_before is not None:
            where.append("mtime<=?"); params.append(float(q.modified_before))
        if q.locations:
            ors = []
            for loc in q.locations:
                base = str(loc).rstrip("\\/")
                ors.append("path LIKE ?")
                esc = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                sep = "\\\\" if os.sep == "\\" else os.sep
                params.append(esc + sep + "%")
            where.append("(" + " OR ".join(ors) + ")")
        order = {"size": "size DESC", "name": "name_lower ASC", "oldest": "mtime ASC"}.get(q.sort, "mtime DESC")
        sql = ("SELECT path,name,is_dir,size,mtime,ctime FROM files"
               + (" WHERE " + " AND ".join(where) if where else "")
               + f" ORDER BY {order} LIMIT ?")
        # LIKE with ESCAPE for the location clauses too
        sql = sql.replace("path LIKE ?", "path LIKE ? ESCAPE '\\'")
        params.append(int(max(q.limit * 4, q.limit)) if q.glob else int(q.limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for path, name, is_dir, size, mtime, ctime in rows:
            if q.glob and not fnmatch.fnmatch(name.lower(), q.glob.lower()):
                continue
            out.append(_row(path, name, is_dir, size, mtime, ctime))
            if len(out) >= q.limit:
                break
        return out


def scan_search(q: SearchQuery, roots: list, excludes: list | None = None,
                max_scanned: int = 200_000, time_budget_s: float = 25.0, stop_flag=None) -> dict:
    """Fallback when no index is available: bounded directory walk."""
    probe = FileIndex.__new__(FileIndex)          # reuse is_excluded without a DB
    probe.excludes = list(excludes if excludes is not None else DEFAULT_EXCLUDES)
    t0 = time.time()
    scanned, out, truncated = 0, [], False
    stack = [str(r) for r in (q.locations or roots) if os.path.isdir(str(r))]
    q_all = SearchQuery(**{**q.__dict__, "locations": []})
    while stack:
        if stop_flag is not None and stop_flag():
            truncated = True
            break
        if scanned > max_scanned or time.time() - t0 > time_budget_s:
            truncated = True
            break
        d = stack.pop()
        try:
            it = list(os.scandir(d))
        except (PermissionError, OSError):
            continue
        for e in it:
            scanned += 1
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                if FileIndex.is_excluded(probe, e.path, e.name):
                    continue
                if is_dir:
                    stack.append(e.path)
                elif not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            size = 0 if is_dir else st.st_size
            if _matches(e.name, is_dir, size, st.st_mtime, e.path, q_all):
                out.append(_row(e.path, e.name, is_dir, size, st.st_mtime))
    out = _sort(out, q.sort)[:q.limit]
    return {"results": out, "scanned": scanned, "truncated": truncated,
            "seconds": round(time.time() - t0, 2)}
