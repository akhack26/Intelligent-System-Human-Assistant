"""Streaming text filter for speech.

Token deltas arrive in arbitrary pieces, so a sentence splitter alone can
hand the TTS engine half a ``<think>`` block or the inside of a code fence
(the closing tag has not arrived yet, so no regex can remove it). This small
state machine drops everything inside ``<think>...</think>`` and inside
fenced blocks *as it streams*:

* fences follow Markdown rules - they open and close only at the start of a
  line (up to three spaces of indent), a closer must use the same character
  and be at least as long as the opener, and nothing but spaces may follow
  it. So ```` blocks that contain ``` code, and inline ``` inside code, never
  end the block early;
* ISHA's own ``isha-visual`` / ``isha-chart`` / ``artifact`` blocks, and a
  fence that ends its line after a language word ("Here: ```python"), are
  recognised even mid-line, because their content must never be read aloud;
* only a few characters that might be the start of a marker are held back.
"""
from __future__ import annotations

import re

_OPEN_THINK = re.compile(r"<think\b[^>]*>", re.I)
_CLOSE_THINK = re.compile(r"</think\s*>", re.I)
_LINE_FENCE = re.compile(r"[ ]{0,3}(`{3,}|~{3,})")
# Mid-line openers: ISHA's own blocks always, and a fence followed only by a
# language word and the end of the line ("Here is the code: ```python").
_MIDLINE_OPEN = re.compile(r"(`{3,}|~{3,})[ \t]*(?:isha-visual|isha-chart|artifact)\b|"
                           r"(`{3,}|~{3,})[ \t]*[\w+#.-]*[ \t]*(?=\r?\n)", re.I)
_MAX_PENDING = 4000


def _could_be_fence_start(s: str) -> bool:
    """True if s (the start of a line) may still grow into a fence opener."""
    return bool(re.fullmatch(r"[ ]{0,3}(?:`{0,2}|~{0,2})", s))


def _partial_marker_len(s: str) -> int:
    """Length of a trailing fragment that could start '<think' or one of ISHA's
    own block openers (which are recognised mid-line)."""
    low = s.lower()
    best = 0
    for mk in ("<think", "```", "~~~"):
        for k in range(min(len(mk) - 1, len(low)), 0, -1):
            if mk.startswith(low[-k:]):
                best = max(best, k)
                break
    i = low.rfind("<think")
    if i >= 0 and ">" not in low[i:]:
        best = max(best, len(low) - i)
    m = re.search(r"(`{3,}|~{3,})[ \t]*[\w+#.-]{0,12}[ \t]*$", s)   # "```isha-vis" / "```python" not finished
    if m:
        best = max(best, len(s) - m.start())
    return best


class SpeakableStream:
    def __init__(self):
        self.reset()

    def reset(self):
        self._buf = ""
        self._state = "text"         # text | think | fence
        self._fence = ""
        self._line_start = True      # is _buf[0] at the start of a line?

    def feed(self, delta: str) -> str:
        self._buf += delta or ""
        out = []
        while self._buf:
            if self._state == "think":
                m = _CLOSE_THINK.search(self._buf)
                if not m:
                    self._buf = self._buf[-9:]
                    break
                self._buf = self._buf[m.end():]
                self._state = "text"
                self._line_start = False
                continue

            if self._state == "fence":
                nl = self._buf.find("\n")
                if nl < 0:
                    if len(self._buf) > _MAX_PENDING and not _LINE_FENCE.match(self._buf):
                        self._buf = ""            # a very long code line: drop it, stay in the block
                        self._line_start = False
                    break
                line = self._buf[:nl]
                self._buf = self._buf[nl + 1:]
                if self._line_start and re.fullmatch(
                        r"[ ]{0,3}" + re.escape(self._fence[0]) + "{%d,}[ \\t]*\\r?" % len(self._fence), line):
                    self._state = "text"
                self._line_start = True
                continue

            # ---- text ----
            if self._line_start:
                if _could_be_fence_start(self._buf):
                    break                           # wait: may become ``` / ~~~
                m = _LINE_FENCE.match(self._buf)
                if m:
                    if m.end() == len(self._buf):
                        break                       # the run may still grow (```` vs ```)
                    self._state = "fence"
                    self._fence = m.group(1)
                    self._buf = self._buf[m.end():]
                    self._line_start = False        # rest of the opener line = info string
                    continue
            mt = _OPEN_THINK.search(self._buf)
            mo = _MIDLINE_OPEN.search(self._buf)
            nl = self._buf.find("\n")
            cands = [(x.start(), "think", x) for x in (mt,) if x] + \
                    [(x.start(), "own", x) for x in (mo,) if x] + \
                    ([(nl, "nl", None)] if nl >= 0 else [])
            if not cands:
                hold = _partial_marker_len(self._buf)
                cut = len(self._buf) - hold
                if cut > 0:
                    out.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    self._line_start = False
                break
            pos, kind, m = min(cands, key=lambda c: c[0])
            if kind == "nl":
                out.append(self._buf[:pos + 1])
                self._buf = self._buf[pos + 1:]
                self._line_start = True
                continue
            out.append(self._buf[:pos])
            if kind == "think":
                self._state = "think"
                self._buf = self._buf[m.end():]
            else:                                   # mid-line opener (see _MIDLINE_OPEN)
                self._state = "fence"
                self._fence = m.group(1) or m.group(2)
                self._buf = self._buf[m.end():]
                self._line_start = False
        return "".join(out)

    def flush(self) -> str:
        rest = self._buf if self._state == "text" else ""
        self.reset()
        return rest
