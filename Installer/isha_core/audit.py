"""Action log: every tool run, model choice and approval, in one place.

Kept in memory (ring buffer, for the GUI Activity panel) and appended to a
local JSONL file (for debugging after the fact). Secrets are redacted before
anything is stored, and message/file bodies are truncated.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

_SECRET_KEYS = re.compile(r"(pass(word)?|pwd|secret|token|api[_-]?key|auth|cookie|otp|pin)", re.I)
_SECRET_VALUE = re.compile(r"\b(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")


def redact(value, key: str = ""):
    if key and _SECRET_KEYS.search(key):
        return "***redacted***"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        v = _SECRET_VALUE.sub("***redacted***", value)
        return v if len(v) <= 300 else v[:297] + "..."
    return value


class ActionLog:
    def __init__(self, path: Path | None = None, capacity: int = 500):
        self.path = Path(path) if path else None
        self._items = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._listeners = []
        self.current = {"task": "", "model": "", "tool": "", "step": ""}

    def subscribe(self, fn):
        self._listeners.append(fn)

    def unsubscribe(self, fn):
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def set_current(self, **kw):
        self.current.update({k: str(v) for k, v in kw.items() if v is not None})
        self._notify({"kind": "status", **self.current})

    def record(self, kind: str, **fields) -> dict:
        entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time(),
                 "kind": kind}
        for k, v in fields.items():
            entry[k] = redact(v, k)
        with self._lock:
            self._items.append(entry)
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                except Exception:
                    pass
        self._notify(entry)
        return entry

    def _notify(self, entry):
        for fn in list(self._listeners):
            try:
                fn(entry)
            except Exception:
                pass

    def recent(self, n: int = 50, kind: str | None = None) -> list:
        with self._lock:
            items = list(self._items)
        if kind:
            items = [i for i in items if i.get("kind") == kind]
        return items[-n:]

    def format_recent(self, n: int = 15) -> str:
        rows = []
        for e in self.recent(n):
            if e["kind"] == "tool":
                ok = "OK" if e.get("success") else "FAIL"
                rows.append(f"{e['time']}  {e.get('tool')}  [{e.get('risk')}/{e.get('approval')}]  "
                            f"{ok}  {e.get('duration_ms', 0)}ms  {str(e.get('result', ''))[:80]}")
            else:
                rows.append(f"{e['time']}  {e['kind']}: " +
                            ", ".join(f"{k}={v}" for k, v in e.items()
                                      if k not in ("time", "ts", "kind"))[:160])
        return "\n".join(rows) or "No activity yet."
