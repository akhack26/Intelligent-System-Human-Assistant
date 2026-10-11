"""Coding agent core: scaffold -> write -> check -> run -> read error -> fix -> rerun.

Kept free of Qt and of the model backend so it can be tested headlessly: the
"brain" is injected as ``fixer(code, error, language, path) -> new_code`` and
``generator(prompt, language) -> code``.

Safety model for generated code
    * static scan before EVERY run (not just the first draft): catastrophic
      patterns are refused outright; a fix that introduces a *new* risky
      capability (subprocess, deleting files, network) that the approved
      version did not have stops the loop and asks the user again;
    * runs with a timeout, stdin closed or fed a known test input, the
      project folder as cwd, a scrubbed environment (no API keys/tokens), and
      on Linux/macOS CPU-time + file-size rlimits;
    * bounded attempts (default 3, hard cap 5), and STOP is honoured.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .control import STOP, Cancelled

_IS_WIN = platform.system() == "Windows"
META_FILE = ".isha_project.json"
HARD_MAX_ATTEMPTS = 5

LANG_BY_EXT = {".py": "python", ".js": "javascript", ".sh": "bash", ".html": "html", ".htm": "html",
               ".css": "css", ".ps1": "powershell", ".bat": "batch"}

BLOCKED_CODE = [
    (r"rm\s+-rf\s+/(?:\s|$|\*)", "rm -rf /"),
    (r"\bmkfs\b", "mkfs"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bdd\s+if=.*of=/dev/", "raw disk write"),
    (r"\bformat\s+[c-z]:", "drive format"),
    (r"shutil\.rmtree\(\s*(['\"])(/|[A-Za-z]:\\\\?|~)\1", "rmtree on a root/home path"),
    (r"shutil\.rmtree\(\s*(os\.path\.expanduser\(['\"]~['\"]\)|Path\.home\(\))\s*\)", "rmtree on home"),
    (r"os\.system\(\s*['\"](rm\s+-rf|del\s+/[sq]|format)\b", "destructive os.system"),
    (r"\bdiskpart\b|\bbcdedit\b|\bvssadmin\s+delete", "system disk/boot tampering"),
    (r"set-mppreference.*-disable|netsh\s+advfirewall\s+set.*off", "disables OS security"),
    (r"\\CurrentVersion\\Run\b|\bschtasks\s+/create\b|\bcrontab\s+-", "persistence mechanism"),
    (r"(ctypes\.windll\.ntdll\.RtlAdjustPrivilege|NtRaiseHardError)", "BSOD trick"),
]

RISKY_CAPABILITIES = {
    "runs shell commands": r"\bsubprocess\.|\bos\.system\(|\bos\.popen\(|child_process|\bexecSync\(|\bspawn\(",
    "deletes files": r"\bos\.remove\(|\bos\.unlink\(|\bshutil\.rmtree\(|\.unlink\(|\brmdir\(|fs\.rm|fs\.unlink|\brm\s+-",
    "uses the network": r"\brequests\.|\burllib\.request|\bsocket\.|\bhttp\.client|\bfetch\(|\baxios\b|\bcurl\b|\bwget\b",
    "dynamic code execution": r"\beval\(|\bexec\(|\bcompile\(|new\s+Function\(",
    "writes outside project": r"open\(\s*['\"](/|[A-Za-z]:\\\\)|Path\(\s*['\"](/|[A-Za-z]:\\\\)",
}

_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_KEY|PRIVATE|CREDENTIAL|AUTH)", re.I)


def detect_language(path) -> str:
    return LANG_BY_EXT.get(Path(path).suffix.lower(), "unknown")


def scan_code(code: str) -> dict:
    blocked = [why for pat, why in BLOCKED_CODE if re.search(pat, code or "", re.I)]
    caps = sorted(name for name, pat in RISKY_CAPABILITIES.items() if re.search(pat, code or "", re.I))
    return {"blocked": blocked, "capabilities": caps}


def new_capabilities(old_code: str, new_code: str) -> list:
    return sorted(set(scan_code(new_code)["capabilities"]) - set(scan_code(old_code)["capabilities"]))


def strip_fences(text: str) -> str:
    t = (text or "").strip()
    m = re.search(r"```[\w+\-]*\s*\n(.*?)```", t, re.S)
    if m:
        return m.group(1).strip("\n")
    if t.startswith("```"):
        lines = t.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip("\n")


def safe_name(name: str) -> str:
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip()).strip(" .")
    n = re.sub(r"\s+", "_", n)
    if not n or n in (".", "..") or n.upper() in ("CON", "PRN", "AUX", "NUL"):
        raise ValueError(f"'{name}' is not a valid project name")
    return n[:80]


# ---------------------------------------------------------------------------
# templates (used when no model is loaded, or the model's draft is unusable)
# ---------------------------------------------------------------------------
PY_CALCULATOR = '''"""{name} - a safe command-line calculator (created by ISHA).

Type an expression like 12 * (3 + 4) / 2 and press Enter. Type q to quit.
Only numbers and + - * / // % ** ( ) are accepted - nothing else is ever run.
"""
import ast
import operator
import sys

OPS = {{
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}}


def evaluate(expression: str) -> float:
    """Evaluate an arithmetic expression without eval()."""
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](walk(node.operand))
        raise ValueError("only numbers and + - * / // % ** ( ) are allowed")
    return walk(ast.parse(expression.strip(), mode="eval"))


def main() -> int:
    print("{name} calculator - type an expression, or q to quit.")
    for line in sys.stdin:
        expr = line.strip()
        if not expr:
            continue
        if expr.lower() in ("q", "quit", "exit"):
            print("Bye!")
            return 0
        try:
            result = evaluate(expr)
            if isinstance(result, float) and result.is_integer():
                result = int(result)
            print(f"= {{result}}")
        except ZeroDivisionError:
            print("Error: division by zero")
        except (ValueError, SyntaxError) as exc:
            print(f"Error: {{exc}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

PY_GENERIC = '''"""{name} - created by ISHA.

{description}
"""


def main() -> None:
    print("Hello from {name}!")


if __name__ == "__main__":
    main()
'''

WEB_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{name}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main class="card">
    <h1>{name}</h1>
    <p class="sub">{description}</p>
    <button id="action">Click me</button>
    <p id="output" aria-live="polite"></p>
  </main>
  <script src="script.js"></script>
</body>
</html>
'''

WEB_CSS = '''* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: grid; place-items: center;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: linear-gradient(135deg, #0f172a, #1e3a8a); color: #e2e8f0;
}
.card {
  background: rgba(15, 23, 42, 0.85); padding: 2.5rem; border-radius: 16px;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.4); text-align: center; max-width: 480px; width: 90%;
}
h1 { margin-top: 0; color: #38bdf8; }
.sub { color: #94a3b8; }
button {
  font-size: 1rem; padding: 0.7rem 1.6rem; border: none; border-radius: 999px;
  background: #38bdf8; color: #0f172a; cursor: pointer; font-weight: 600;
}
button:hover { background: #7dd3fc; }
#output { min-height: 1.5em; font-size: 1.1rem; }
'''

WEB_JS = '''// {name} - created by ISHA
let clicks = 0;
document.getElementById("action").addEventListener("click", () => {
  clicks += 1;
  document.getElementById("output").textContent = `Button clicked ${clicks} time(s).`;
});
'''

WEB_CALC_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{name}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main class="calc">
    <h1>{name}</h1>
    <input id="display" readonly value="0" aria-label="display">
    <div class="keys">
      <button data-k="C" class="op">C</button><button data-k="(" class="op">(</button>
      <button data-k=")" class="op">)</button><button data-k="/" class="op">&divide;</button>
      <button data-k="7">7</button><button data-k="8">8</button><button data-k="9">9</button>
      <button data-k="*" class="op">&times;</button>
      <button data-k="4">4</button><button data-k="5">5</button><button data-k="6">6</button>
      <button data-k="-" class="op">&minus;</button>
      <button data-k="1">1</button><button data-k="2">2</button><button data-k="3">3</button>
      <button data-k="+" class="op">+</button>
      <button data-k="0" class="wide">0</button><button data-k=".">.</button>
      <button data-k="=" class="eq">=</button>
    </div>
  </main>
  <script src="script.js"></script>
</body>
</html>
'''

WEB_CALC_CSS = WEB_CSS + '''
.calc { background: rgba(15,23,42,.9); padding: 1.5rem; border-radius: 18px; width: 320px; }
#display { width: 100%; font-size: 2rem; text-align: right; padding: .6rem; border: none;
  border-radius: 10px; background: #020617; color: #f8fafc; margin-bottom: 1rem; }
.keys { display: grid; grid-template-columns: repeat(4, 1fr); gap: .5rem; }
.keys button { border-radius: 12px; padding: 1rem 0; background: #1e293b; color: #e2e8f0; }
.keys button.op { background: #334155; }
.keys button.eq { background: #38bdf8; color: #0f172a; }
.keys button.wide { grid-column: span 2; }
'''

WEB_CALC_JS = '''// {name} - safe calculator (no eval): shunting-yard parser
const display = document.getElementById("display");
let expr = "";

function tokenize(s) {
  const out = []; let num = "";
  for (const ch of s) {
    if ("0123456789.".includes(ch)) { num += ch; continue; }
    if (num) { out.push(parseFloat(num)); num = ""; }
    if ("+-*/()".includes(ch)) out.push(ch);
  }
  if (num) out.push(parseFloat(num));
  return out;
}

function evaluate(s) {
  const prec = { "+": 1, "-": 1, "*": 2, "/": 2 };
  const out = [], ops = [];
  for (const t of tokenize(s)) {
    if (typeof t === "number") out.push(t);
    else if (t === "(") ops.push(t);
    else if (t === ")") { while (ops.length && ops[ops.length - 1] !== "(") out.push(ops.pop()); ops.pop(); }
    else { while (ops.length && prec[ops[ops.length - 1]] >= prec[t]) out.push(ops.pop()); ops.push(t); }
  }
  while (ops.length) out.push(ops.pop());
  const st = [];
  for (const t of out) {
    if (typeof t === "number") { st.push(t); continue; }
    const b = st.pop(), a = st.pop();
    if (a === undefined || b === undefined) throw new Error("bad expression");
    st.push(t === "+" ? a + b : t === "-" ? a - b : t === "*" ? a * b : a / b);
  }
  if (st.length !== 1 || !isFinite(st[0])) throw new Error("bad expression");
  return st[0];
}

document.querySelectorAll(".keys button").forEach(btn => btn.addEventListener("click", () => {
  const k = btn.dataset.k;
  if (k === "C") expr = "";
  else if (k === "=") { try { expr = String(evaluate(expr)); } catch { expr = ""; display.value = "Error"; return; } }
  else expr += k;
  display.value = expr || "0";
}));
'''


@dataclass
class ProjectSpec:
    name: str
    kind: str                 # python | web
    files: dict               # relative path -> content
    entry: str
    test_input: str = ""
    source: str = "template"  # template | model
    description: str = ""


def choose_template(kind: str, name: str, description: str = "") -> ProjectSpec:
    d = (description or "").lower() + " " + name.lower()
    is_calc = bool(re.search(r"calc|calculator|hisaab|hisab", d))
    desc = description.strip() or f"{name} project"
    if kind == "web":
        if is_calc:
            files = {"index.html": WEB_CALC_HTML.format(name=name), "style.css": WEB_CALC_CSS,
                     "script.js": WEB_CALC_JS.replace("{name}", name)}
        else:
            files = {"index.html": WEB_HTML.format(name=name, description=desc), "style.css": WEB_CSS,
                     "script.js": WEB_JS.replace("{name}", name)}
        return ProjectSpec(name, "web", files, "index.html", description=desc)
    if is_calc:
        return ProjectSpec(name, "python", {"main.py": PY_CALCULATOR.format(name=name),
                                            "README.md": f"# {name}\n\nRun: `python main.py`\n"},
                           "main.py", test_input="2+3*4\n10/4\n(1+2)**3\nq\n", description=desc)
    return ProjectSpec(name, "python", {"main.py": PY_GENERIC.format(name=name, description=desc),
                                        "README.md": f"# {name}\n\n{desc}\n\nRun: `python main.py`\n"},
                       "main.py", description=desc)


def write_project(base_dir: Path, spec: ProjectSpec, overwrite: bool = False) -> dict:
    base_dir = Path(base_dir)
    if not base_dir.is_dir():
        return {"success": False, "message": f"Location nahi mila: {base_dir}", "error": "missing base dir"}
    root = base_dir / safe_name(spec.name)
    existing = [f for f in spec.files if (root / f).exists()]
    if existing and not overwrite:
        return {"success": False, "path": str(root), "error": "exists",
                "message": f"'{root}' me yeh files pehle se hain: {', '.join(existing)}. Overwrite ke liye confirm karo."}
    written = []
    for rel, content in spec.files.items():
        rel_p = Path(rel)
        if rel_p.is_absolute() or ".." in rel_p.parts:
            return {"success": False, "error": "unsafe path", "message": f"Unsafe file path refused: {rel}"}
        target = root / rel_p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(str(target))
    meta = {"name": spec.name, "kind": spec.kind, "entry": spec.entry, "test_input": spec.test_input,
            "source": spec.source, "created": datetime.now().isoformat(timespec="seconds"),
            "created_by": "ISHA"}
    (root / META_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    # verify on disk, never just assume
    missing = [w for w in written if not Path(w).is_file()]
    ok = not missing
    return {"success": ok, "path": str(root), "files": written, "entry": str(root / spec.entry),
            "source": spec.source,
            "message": (f"Project '{spec.name}' ban gaya: {root} ({len(written)} files: "
                        f"{', '.join(Path(w).name for w in written)})") if ok else
                       f"Kuch files nahi bani: {missing}", "error": None if ok else "missing files"}


def load_meta(project_dir: Path) -> dict:
    try:
        return json.loads((Path(project_dir) / META_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_entry(project_dir: Path) -> Path | None:
    project_dir = Path(project_dir)
    meta = load_meta(project_dir)
    if meta.get("entry") and (project_dir / meta["entry"]).is_file():
        return project_dir / meta["entry"]
    for cand in ("main.py", "app.py", "run.py", "__main__.py", "index.js", "main.js", "app.js",
                 "server.js", "index.html", "main.sh", "run.sh"):
        if (project_dir / cand).is_file():
            return project_dir / cand
    pys = sorted(project_dir.glob("*.py"))
    if len(pys) == 1:
        return pys[0]
    htmls = sorted(project_dir.glob("*.html"))
    return htmls[0] if htmls else (pys[0] if pys else None)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def _scrubbed_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _limits(cpu_seconds: int):
    if _IS_WIN:
        return None

    def apply():
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))
            resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024 * 1024, 200 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except Exception:
            pass
    return apply


def command_for(path: Path) -> list | None:
    ext = path.suffix.lower()
    if ext == ".py":
        return [sys.executable, "-X", "utf8", str(path)]
    if ext == ".js":
        node = shutil.which("node")
        return [node, str(path)] if node else None
    if ext == ".sh":
        bash = shutil.which("bash")
        return [bash, str(path)] if bash else None
    if ext == ".ps1" and _IS_WIN:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
    return None


def syntax_check(path: Path) -> str:
    """'' when fine, otherwise the error text. Never executes the code."""
    if path.suffix.lower() == ".py":
        try:
            compile(path.read_text(encoding="utf-8", errors="replace"), str(path), "exec")
            return ""
        except SyntaxError as e:
            return (f'  File "{path}", line {e.lineno}\n    {(e.text or "").rstrip()}\n'
                    f"SyntaxError: {e.msg}")
        except Exception as e:  # noqa: BLE001
            return str(e)
    if path.suffix.lower() == ".js" and shutil.which("node"):
        r = subprocess.run([shutil.which("node"), "--check", str(path)], capture_output=True, text=True, timeout=20)
        return "" if r.returncode == 0 else (r.stderr or r.stdout)[-2000:]
    return ""


def is_gui_or_server(code: str) -> bool:
    return bool(re.search(r"\b(tkinter|PyQt5|PyQt6|PySide\d?|pygame|kivy|wx\b|flask|fastapi|uvicorn|"
                          r"http\.server|app\.run\(|mainloop\(\)|createServer|express\(\))", code or ""))


def run_capture(path: Path, timeout: int = 20, stdin_text: str | None = None) -> dict:
    cmd = command_for(path)
    if cmd is None:
        return {"exit_code": None, "stdout": "", "stderr": f"No runner for {path.suffix}", "timed_out": False,
                "ran": False}
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=str(path.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", env=_scrubbed_env(),
                                preexec_fn=_limits(timeout + 5),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return {"exit_code": None, "stdout": "", "stderr": str(e), "timed_out": False, "ran": False}
    STOP.track(proc)
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        timed_out = True
    finally:
        STOP.untrack(proc)
    return {"exit_code": proc.returncode, "stdout": (out or "")[-6000:], "stderr": (err or "")[-6000:],
            "timed_out": timed_out, "ran": True, "seconds": round(time.time() - t0, 2),
            "stopped": STOP.stopped}


def launch_detached(path: Path) -> dict:
    """Start a program for the user to interact with (GUI / interactive CLI)."""
    cmd = command_for(path)
    if cmd is None:
        return {"success": False, "message": f"No runner for {path.suffix}"}
    kw = {"cwd": str(path.parent)}
    if _IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
    else:
        term = next((t for t in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm")
                     if shutil.which(t)), None)
        if term and path.suffix.lower() == ".py" and not is_gui_or_server(path.read_text(errors="replace")):
            cmd = ([term, "--", *cmd] if term == "gnome-terminal" else [term, "-e", " ".join(
                f'"{c}"' if " " in c else c for c in cmd)])
        kw["start_new_session"] = True
    try:
        log = open(path.parent / ".isha_run.log", "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, **kw)
    except Exception as e:
        return {"success": False, "message": f"Launch failed: {e}"}
    time.sleep(2.0)
    alive = proc.poll() is None
    code = proc.returncode
    tail = ""
    try:
        tail = (path.parent / ".isha_run.log").read_text(encoding="utf-8", errors="replace")[-1500:]
    except Exception:
        pass
    ok = alive or code == 0
    return {"success": ok, "pid": proc.pid, "alive": alive, "exit_code": code, "output": tail,
            "message": (f"'{path.name}' chal raha hai (PID {proc.pid})." if alive else
                        f"'{path.name}' chala aur exit code {code} ke saath band hua.")}


def run_failed(result: dict, code: str) -> bool:
    if not result.get("ran"):
        return True
    if result.get("timed_out"):
        return False           # long-running is not an error by itself
    err = result.get("stderr", "")
    if result.get("exit_code") not in (0, None):
        # interactive program that simply had no input is not a bug
        if "EOFError" in err and re.search(r"\binput\s*\(", code or ""):
            return False
        return True
    return bool(re.search(r"Traceback \(most recent call last\)|SyntaxError|ReferenceError|TypeError:", err))


@dataclass
class FixResult:
    success: bool
    attempts: int
    path: str
    log: list = field(default_factory=list)
    output: str = ""
    error: str = ""
    needs_approval: list = field(default_factory=list)
    stopped: bool = False
    status: str = ""          # ok | failed | blocked | needs_approval | stopped | long_running | no_runner

    def message(self) -> str:
        name = Path(self.path).name
        if self.status == "ok":
            fixed = " (auto-fix ke baad)" if self.attempts > 1 else ""
            return f"'{name}' successfully chala{fixed} - attempt {self.attempts}.\nOutput:\n{self.output[:800] or '(no output)'}"
        if self.status == "long_running":
            return (f"'{name}' start hua aur {self.attempts}. attempt me time-limit tak bina error ke chalta raha "
                    f"(GUI/server/long-running). Output:\n{self.output[:500]}")
        if self.status == "blocked":
            return f"'{name}' NAHI chalaya - code me blocked high-risk pattern mila: {self.error}"
        if self.status == "needs_approval":
            return (f"Auto-fix ne naye risky kaam jode ({', '.join(self.needs_approval)}). "
                    f"Maine rok diya hai - pehle aap code dekh kar approve karo: {self.path}")
        if self.status == "stopped":
            return f"Rok diya gaya (attempt {self.attempts})."
        if self.status == "no_runner":
            return self.error
        return (f"{self.attempts} attempt(s) ke baad bhi '{name}' theek nahi hua. Last error:\n"
                f"{self.error[-800:]}\nFile: {self.path}")


def fix_loop(path: Path, fixer=None, max_attempts: int = 3, timeout: int = 20,
             stdin_text: str | None = None, on_step=None) -> FixResult:
    """Run `path`; on failure feed the error to `fixer` and retry, bounded."""
    path = Path(path)
    max_attempts = max(1, min(HARD_MAX_ATTEMPTS, int(max_attempts or 3)))
    approved_code = path.read_text(encoding="utf-8", errors="replace")
    code = approved_code
    lang = detect_language(path)
    res = FixResult(False, 0, str(path))
    say = on_step or (lambda s: None)
    if command_for(path) is None:
        res.status, res.error = "no_runner", f"'{path.suffix}' files ke liye runner nahi mila (node/bash install hai?)."
        return res
    for attempt in range(1, max_attempts + 1):
        try:
            STOP.check()
        except Cancelled:
            res.status, res.stopped = "stopped", True
            return res
        res.attempts = attempt
        scan = scan_code(code)
        if scan["blocked"]:
            res.status, res.error = "blocked", ", ".join(scan["blocked"])
            res.log.append(f"attempt {attempt}: blocked ({res.error})")
            return res
        added = new_capabilities(approved_code, code)
        if added:
            res.status, res.needs_approval = "needs_approval", added
            res.log.append(f"attempt {attempt}: fix added {added} - waiting for user")
            return res
        say(f"attempt {attempt}/{max_attempts}: checking + running {path.name}")
        err = syntax_check(path)
        if err:
            run = {"ran": True, "exit_code": 1, "stdout": "", "stderr": err, "timed_out": False}
        else:
            run = run_capture(path, timeout=timeout, stdin_text=stdin_text)
        if run.get("stopped") or STOP.stopped:
            res.status, res.stopped = "stopped", True
            return res
        if not run_failed(run, code):
            res.success = True
            res.output = (run.get("stdout") or "") + (("\n" + run["stderr"]) if run.get("stderr") else "")
            res.status = "long_running" if run.get("timed_out") else "ok"
            res.log.append(f"attempt {attempt}: {res.status}")
            return res
        res.error = (run.get("stderr") or run.get("stdout") or f"exit code {run.get('exit_code')}")[-3000:]
        res.log.append(f"attempt {attempt}: FAILED - {res.error.strip().splitlines()[-1][:160] if res.error.strip() else ''}")
        if attempt >= max_attempts or fixer is None:
            break
        say(f"error mila, fix kar rahi hoon (attempt {attempt})")
        try:
            new_code = strip_fences(fixer(code, res.error, lang, str(path)) or "")
        except Cancelled:
            res.status, res.stopped = "stopped", True
            return res
        except Exception as e:  # noqa: BLE001
            res.log.append(f"fixer failed: {e}")
            break
        if not new_code.strip() or new_code.strip() == code.strip():
            res.log.append("fixer returned no change")
            break
        code = new_code
        path.write_text(code, encoding="utf-8", newline="\n")
    res.status = "failed"
    return res
