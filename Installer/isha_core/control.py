"""Emergency STOP plumbing shared by the agent loop, tools and subprocesses.

``STOP.trigger()`` is what the GUI Stop button / Esc key calls. Long-running
tools call ``STOP.check()`` between steps and child processes started via
``STOP.track(proc)`` are terminated on stop. ``STOP.reset()`` runs at the
start of each new user request.
"""
from __future__ import annotations

import threading


class Cancelled(Exception):
    """Raised inside a tool when the user pressed Stop."""


class StopController:
    def __init__(self):
        self._event = threading.Event()
        self._procs = []
        self._lock = threading.Lock()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def reset(self):
        self._event.clear()
        with self._lock:
            self._procs = [p for p in self._procs if p.poll() is None]

    def trigger(self) -> int:
        """Set the flag and kill tracked child processes. Returns #killed."""
        self._event.set()
        killed = 0
        with self._lock:
            procs, self._procs = list(self._procs), []
        for p in procs:
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=2)
                    except Exception:
                        p.kill()
                    killed += 1
            except Exception:
                pass
        return killed

    def check(self):
        if self._event.is_set():
            raise Cancelled("Stopped by user.")

    def track(self, proc):
        with self._lock:
            self._procs.append(proc)
        return proc

    def untrack(self, proc):
        with self._lock:
            try:
                self._procs.remove(proc)
            except ValueError:
                pass

    def wait(self, seconds: float) -> bool:
        """Sleep that wakes early on stop. Returns True if stopped."""
        return self._event.wait(seconds)


STOP = StopController()
