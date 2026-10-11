"""Artifacts: generated code, documents and designs, pulled out of a reply.

The model marks an artifact with a fenced block::

    ```artifact filename="landing.html" title="Landing page" type="html"
    <!doctype html> ...
    ```

and (only while the Artifacts feature is on) any long ordinary code block is
captured too. Artifacts are shown in their own panel, saved under a
predictable local folder with safe, never-overwriting file names, and are
NEVER executed by ISHA. HTML previews run in a sandbox (see
:func:`sandbox_html`) with a Content-Security-Policy that blocks every
network request and file access.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .projects import safe_file_name, unique_path

AUTO_CAPTURE_LINES = 15          # an ordinary fenced code block this long becomes an artifact
MAX_ARTIFACT_CHARS = 1_500_000

EXT_BY_LANG = {
    "html": ".html", "htm": ".html", "css": ".css", "js": ".js", "javascript": ".js", "jsx": ".jsx",
    "ts": ".ts", "typescript": ".ts", "tsx": ".tsx", "python": ".py", "py": ".py", "java": ".java",
    "c": ".c", "cpp": ".cpp", "c++": ".cpp", "cs": ".cs", "csharp": ".cs", "go": ".go", "rust": ".rs",
    "rs": ".rs", "ruby": ".rb", "php": ".php", "swift": ".swift", "kotlin": ".kt", "sql": ".sql",
    "bash": ".sh", "sh": ".sh", "shell": ".sh", "powershell": ".ps1", "ps1": ".ps1", "bat": ".bat",
    "json": ".json", "yaml": ".yaml", "yml": ".yaml", "xml": ".xml", "svg": ".svg", "markdown": ".md",
    "md": ".md", "text": ".txt", "txt": ".txt", "csv": ".csv", "toml": ".toml", "ini": ".ini",
    "r": ".r", "lua": ".lua", "dart": ".dart", "vue": ".vue", "svelte": ".svelte",
}
ALLOWED_EXTS = set(EXT_BY_LANG.values()) | {".htm", ".log", ".tsv", ".rst", ".cfg", ".conf"}
HTML_EXTS = {".html", ".htm", ".svg"}
DOC_EXTS = {".md", ".txt", ".rst", ".csv", ".tsv", ".log"}


@dataclass
class Artifact:
    title: str
    filename: str
    content: str
    language: str = ""
    incomplete: bool = False
    saved_path: str = ""
    created: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @property
    def ext(self) -> str:
        return os.path.splitext(self.filename)[1].lower()

    @property
    def kind(self) -> str:
        if self.ext in HTML_EXTS:
            return "html"
        if self.ext in (".md",):
            return "markdown"
        if self.ext in DOC_EXTS:
            return "document"
        return "code"


def normalise_filename(name: str, language: str = "", index: int = 1) -> str:
    lang = (language or "").strip().lower()
    base = safe_file_name(name) if name else ""
    stem, ext = os.path.splitext(base)
    if not ext or ext.lower() not in ALLOWED_EXTS:
        ext = EXT_BY_LANG.get(lang, ".txt" if not base else ".txt")
    if not stem:
        stem = {"html": "page", "css": "styles", "markdown": "document", "md": "document",
                "text": "document", "txt": "document"}.get(lang, f"artifact-{index}")
    return safe_file_name(stem + ext.lower())


def _parse_attrs(s: str) -> dict:
    out = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', s or ""):
        out[m.group(1).lower()] = m.group(2) or m.group(3) or m.group(4) or ""
    return out


@dataclass
class Block:
    start: int          # line index of the opening fence
    end: int            # line index of the closing fence (or last line if unclosed)
    lang: str
    info: str
    body: str
    closed: bool


_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([^\s`]*)[ \t]*(.*)$")


def fenced_blocks(text: str) -> list:
    """Line-based fence parser (``` or ~~~, any length >= 3, CommonMark-ish)."""
    lines = (text or "").split("\n")
    blocks, i = [], 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        fence, lang, info = m.group(1), m.group(2).strip().lower(), m.group(3).strip()
        j = i + 1
        closed = False
        while j < len(lines):
            c = lines[j].strip()
            if c and c[0] == fence[0] and set(c) == {fence[0]} and len(c) >= len(fence):
                closed = True
                break
            j += 1
        body = "\n".join(lines[i + 1:j])
        blocks.append(Block(i, j if closed else len(lines) - 1, lang, info, body, closed))
        i = j + 1
    return blocks


def extract(text: str, auto_capture: bool = True, start_index: int = 1):
    """Split a reply into (display_text, [Artifact]).

    Replaces each artifact block with a one-line pointer. ``isha-visual`` blocks
    are left in place for the visuals module."""
    blocks = fenced_blocks(text)
    if not blocks:
        return text, []
    lines = (text or "").split("\n")
    arts, out, cursor, n = [], [], 0, start_index
    for b in blocks:
        is_art = b.lang == "artifact"
        is_code = (auto_capture and b.lang not in ("", "isha-visual", "isha-chart", "artifact")
                   and b.body.count("\n") + 1 >= AUTO_CAPTURE_LINES)
        if not (is_art or is_code):
            continue
        out.extend(lines[cursor:b.start])
        cursor = b.end + 1
        if is_art:
            attrs = _parse_attrs(b.info)
            lang = attrs.get("type") or attrs.get("lang") or attrs.get("language") or ""
            fname = normalise_filename(attrs.get("filename") or attrs.get("file") or attrs.get("name") or "",
                                       lang, n)
            title = attrs.get("title") or fname
        else:
            lang = b.lang
            fname = normalise_filename("", lang, n)
            title = f"{lang.upper() if len(lang) <= 4 else lang.title()} code"
        body = b.body[:MAX_ARTIFACT_CHARS]
        art = Artifact(title=title[:80], filename=fname, content=body, language=lang,
                       incomplete=not b.closed)
        arts.append(art)
        n += 1
        note = " (output was cut off - may be incomplete)" if art.incomplete else ""
        out.append(f"[Artifact: {art.title} - {art.filename}, opened in the Artifacts panel{note}]")
    out.extend(lines[cursor:])
    return "\n".join(out).strip(), arts


_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; "
        "frame-src 'none'; child-src 'none'; worker-src 'none'; object-src 'none'; "
        "form-action 'none'; base-uri 'none'")


def sandbox_html(content: str, is_svg: bool = False) -> str:
    """Wrap/patch HTML so the preview cannot reach the network or local files.
    The CSP meta tag is inserted first thing in <head>; the embedding view adds
    a URL interceptor and disables file/remote access on top of this."""
    html = content or ""
    if is_svg and "<html" not in html.lower():
        html = f"<!doctype html><html><head></head><body style='margin:0'>{html}</body></html>"
    meta = f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
    m = re.search(r"<head(?:\s[^>]*)?>", html, re.I)          # not <header>
    if m:
        return html[:m.end()] + meta + html[m.end():]
    m = re.search(r"<html(?:\s[^>]*)?>", html, re.I)
    if m:
        return html[:m.end()] + "<head>" + meta + "</head>" + html[m.end():]
    return "<!doctype html><html><head>" + meta + "</head><body>" + html + "</body></html>"


class ArtifactStore:
    """Saves artifacts under ``root`` (and optionally a project's folder)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, art: Artifact, folder: Path | None = None) -> Path:
        folder = Path(folder) if folder else self.root
        folder.mkdir(parents=True, exist_ok=True)
        name = normalise_filename(art.filename, art.language)
        for _ in range(50):
            path = unique_path(folder, name)
            try:
                # "x" = exclusive create: never overwrites, even if a file
                # appears between the check and the write.
                with open(path, "x", encoding="utf-8", newline="") as f:
                    f.write(art.content)
                art.saved_path = str(path)
                return path
            except FileExistsError:
                continue
        raise OSError("could not find a free file name")

    def list_saved(self, folders=None, limit: int = 200) -> list:
        rows = []
        for folder in [self.root] + [Path(f) for f in (folders or [])]:
            try:
                if not folder.is_dir():
                    continue
                for p in folder.iterdir():
                    if p.is_file() and p.suffix.lower() in ALLOWED_EXTS:
                        st = p.stat()
                        rows.append({"path": str(p), "name": p.name, "folder": str(folder),
                                     "mtime": st.st_mtime, "size": st.st_size})
            except Exception:
                continue
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return rows[:limit]

    @staticmethod
    def read(path, allowed_roots) -> Artifact:
        p = Path(path).resolve()
        roots = [Path(r).resolve() for r in allowed_roots]
        if not any(p.parent == r for r in roots):
            raise ValueError("not an ISHA artifact folder")
        text = p.read_text(encoding="utf-8", errors="replace")[:MAX_ARTIFACT_CHARS]
        lang = next((k for k, v in EXT_BY_LANG.items() if v == p.suffix.lower()), "")
        return Artifact(title=p.name, filename=p.name, content=text, language=lang, saved_path=str(p))
