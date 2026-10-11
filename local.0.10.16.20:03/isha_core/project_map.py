"""Project structure mapping for the coding agent.

Inspired by agent-scripts' project-structure skill (dense symbol maps for TS/Swift),
reimplemented in pure Python for ISHA:

* Walk a project tree with sensible exclusions.
* Summarise languages, entry points, top-level layout, and key files.
* Optionally produce a compact "map" suitable for a local model's context window.
* Never modifies files; read-only.

Used by the ``inspect_project`` tool path and by coding workflows.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Directories we never descend into
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".isha", "__pycache__", "node_modules", ".venv", "venv",
    "env", ".env", "dist", "build", "target", ".next", ".nuxt", "coverage",
    ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode", ".vs",
    "eggs", "*.egg-info", ".eggs", "htmlcov", "vendor", "bower_components",
}

# File extensions → language label
EXT_LANG = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".md": "markdown", ".rst": "rst", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".xml": "xml", ".sql": "sql",
    ".r": "r", ".R": "r", ".lua": "lua", ".dart": "dart",
}

ENTRY_CANDIDATES = (
    "main.py", "app.py", "run.py", "manage.py", "__main__.py", "cli.py",
    "index.js", "index.ts", "main.js", "main.ts", "server.js", "server.ts",
    "package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod",
    "Makefile", "CMakeLists.txt", "pom.xml", "build.gradle", "README.md",
    "README", "readme.md",
)

MAX_FILES_LISTED = 200
MAX_DEPTH = 6


@dataclass
class ProjectMap:
    root: str
    exists: bool = True
    languages: dict = field(default_factory=dict)   # lang → file count
    top_level: list = field(default_factory=list)   # names at root
    entry_points: list = field(default_factory=list)
    key_files: list = field(default_factory=list)   # relative paths of interesting files
    file_count: int = 0
    dir_count: int = 0
    errors: list = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "exists": self.exists,
            "languages": self.languages,
            "top_level": self.top_level,
            "entry_points": self.entry_points,
            "key_files": self.key_files[:80],
            "file_count": self.file_count,
            "dir_count": self.dir_count,
            "errors": self.errors,
            "summary": self.summary,
        }

    def prompt_text(self, max_chars: int = 3500) -> str:
        """Compact text for injection into a coding-model prompt."""
        if not self.exists:
            return f"Project path does not exist: {self.root}"
        lines = [
            f"Project root: {self.root}",
            f"Files: {self.file_count}, dirs: {self.dir_count}",
            "Languages: " + (", ".join(f"{k}({v})" for k, v in sorted(
                self.languages.items(), key=lambda x: -x[1])) or "unknown"),
            "Top-level: " + ", ".join(self.top_level[:30]),
        ]
        if self.entry_points:
            lines.append("Likely entry / config: " + ", ".join(self.entry_points[:15]))
        if self.key_files:
            lines.append("Sample paths:")
            for p in self.key_files[:40]:
                lines.append(f"  {p}")
        if self.errors:
            lines.append("Notes: " + "; ".join(self.errors[:5]))
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 20] + "\n… (truncated)"


def _should_skip_dir(name: str) -> bool:
    if name in SKIP_DIRS:
        return True
    if name.startswith(".") and name not in (".github",):
        return True
    if name.endswith(".egg-info"):
        return True
    return False


def map_project(root: str | Path, max_files: int = MAX_FILES_LISTED,
                max_depth: int = MAX_DEPTH) -> ProjectMap:
    """Build a read-only structure map of a project directory."""
    root = Path(root).expanduser().resolve()
    result = ProjectMap(root=str(root))
    if not root.exists():
        result.exists = False
        result.errors.append("path does not exist")
        result.summary = f"Missing: {root}"
        return result
    if not root.is_dir():
        result.exists = False
        result.errors.append("not a directory")
        result.summary = f"Not a directory: {root}"
        return result

    try:
        result.top_level = sorted(
            p.name for p in root.iterdir() if not p.name.startswith(".")
        )[:50]
    except OSError as e:
        result.errors.append(f"cannot list root: {e}")
        return result

    languages: dict[str, int] = {}
    key_files: list[str] = []
    entry_points: list[str] = []
    file_count = 0
    dir_count = 0

    def walk(current: Path, depth: int, rel_base: str = ""):
        nonlocal file_count, dir_count
        if depth > max_depth or file_count >= max_files:
            return
        try:
            entries = list(current.iterdir())
        except OSError as e:
            result.errors.append(f"cannot list {current}: {e}")
            return
        for p in sorted(entries, key=lambda x: x.name.lower()):
            if file_count >= max_files:
                return
            name = p.name
            if p.is_dir():
                if _should_skip_dir(name):
                    continue
                dir_count += 1
                walk(p, depth + 1, f"{rel_base}{name}/" if rel_base else f"{name}/")
            elif p.is_file():
                file_count += 1
                rel = f"{rel_base}{name}"
                ext = p.suffix.lower()
                lang = EXT_LANG.get(ext)
                if lang:
                    languages[lang] = languages.get(lang, 0) + 1
                if name in ENTRY_CANDIDATES or name.lower() in {c.lower() for c in ENTRY_CANDIDATES}:
                    entry_points.append(rel)
                # Keep a sample of source / config files
                if lang in ("python", "javascript", "typescript", "go", "rust", "java",
                            "shell", "markdown", "json", "toml", "yaml") or name in ENTRY_CANDIDATES:
                    if len(key_files) < 80:
                        key_files.append(rel)

    walk(root, 0)
    result.languages = languages
    result.entry_points = entry_points
    result.key_files = key_files
    result.file_count = file_count
    result.dir_count = dir_count

    primary = max(languages, key=languages.get) if languages else "unknown"
    result.summary = (
        f"{root.name}: {file_count} files, primary={primary}, "
        f"entries={', '.join(entry_points[:5]) or 'none'}"
    )
    return result


def find_likely_errors(root: str | Path, max_log_bytes: int = 40_000) -> list[dict]:
    """Look for recent log / traceback files that may explain a crash (read-only)."""
    root = Path(root).expanduser()
    findings: list[dict] = []
    if not root.is_dir():
        return findings
    candidates = []
    for pattern in ("*.log", "traceback.txt", "error.log", "npm-debug.log", "pip.log"):
        try:
            candidates.extend(root.rglob(pattern))
        except OSError:
            break
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:8]:
        try:
            if path.stat().st_size > max_log_bytes:
                text = path.read_text(encoding="utf-8", errors="replace")[-max_log_bytes:]
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
            # Keep only if it looks like an error
            if re.search(r"(Error|Exception|Traceback|FAILED|fatal:)", text, re.I):
                findings.append({
                    "path": str(path),
                    "size": path.stat().st_size,
                    "excerpt": text[-2000:],
                })
        except OSError:
            continue
    return findings
