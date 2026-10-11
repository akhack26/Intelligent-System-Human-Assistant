"""Structured command requests: parse -> validate -> classify -> (approve) -> run.

The model never gets a raw ``shell=True`` pipe by default:

* the command string is tokenised with ``shlex``; if it has no shell
  operators it runs as an argv list with ``shell=False``;
* commands with pipes/redirects/chaining are flagged ``needs_shell`` and
  shown to the user as such in the approval prompt;
* a blocklist refuses destructive, security-disabling, persistence and
  download-and-execute patterns outright - no approval can unlock them;
* every run is bounded by a timeout and registered with the global STOP.
"""
from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field

from .control import STOP

_IS_WIN = platform.system() == "Windows"

BLOCKED_PATTERNS = [
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)[a-z]*\s+(/|~|\*|/\*|\$home)(\s|$)", "recursive delete of root/home"),
    (r"\bmkfs(\.\w+)?\b", "formats a filesystem"),
    (r"\bdd\s+[^|]*\bof=/dev/", "raw write to a disk device"),
    (r">\s*/dev/(sd|nvme|hd)[a-z]", "raw write to a disk device"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bformat\s+[a-z]:", "formats a drive"),
    (r"\bchmod\s+-R\s+0?777\s+/(\s|$)", "opens permissions on the whole system"),
    (r"\bchown\s+-R\s+\S+\s+/(\s|$)", "changes ownership of the whole system"),
    (r"\b(del|erase)\s+(/[a-z]\s+)*[a-z]:\\\*?(\s|$)", "deletes a whole drive"),
    (r"\brd\s+/s\s+/q\s+[a-z]:\\(\s|$)", "deletes a whole drive"),
    (r"\bdiskpart\b", "disk partitioning"),
    (r"\bbcdedit\b", "boot configuration"),
    (r"\bvssadmin\s+delete\b", "deletes shadow copies"),
    (r"\bcipher\s+/w\b", "wipes free space"),
    (r"\breg\s+delete\s+hk(lm|ey_local_machine)", "deletes machine registry keys"),
    (r"set-mppreference\s+.*-disable", "disables Windows Defender"),
    (r"\b(sc|net)\s+stop\s+(windefend|wscsvc|mpssvc)", "stops a security service"),
    (r"netsh\s+advfirewall\s+set\s+\w+\s+state\s+off", "disables the firewall"),
    (r"\bsetenforce\s+0\b", "disables SELinux"),
    (r"\bufw\s+disable\b", "disables the firewall"),
    (r"(curl|wget|iwr|invoke-webrequest)[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", "download-and-execute"),
    (r"\b(iex|invoke-expression)\b.*\b(iwr|invoke-webrequest|downloadstring|net\.webclient)", "download-and-execute"),
    (r"\bschtasks\s+/create\b", "creates a hidden scheduled task (persistence)"),
    (r"\\currentversion\\run\b", "adds an autorun entry (persistence)"),
    (r"\bcrontab\s+-r\b", "wipes the user's cron table"),
    (r"\bhistory\s+-c\b|\bclear-history\b", "erases the audit trail"),
    (r"\b(passwd|chpasswd)\b", "changes account passwords"),
    (r"\buseradd\b|\bnet\s+user\s+\S+\s+\S+\s+/add\b", "creates user accounts"),
]

READ_ONLY_PROGRAMS = {
    "ls", "dir", "pwd", "echo", "cat", "type", "head", "tail", "whoami", "hostname", "date",
    "uname", "df", "du", "free", "uptime", "ps", "tasklist", "ipconfig", "ifconfig", "ip",
    "ping", "nslookup", "where", "which", "systeminfo", "ver", "lsblk", "lscpu", "lsusb", "wc",
    "find", "grep", "findstr", "tree", "stat", "file", "env", "printenv", "nproc", "id",
}
READ_ONLY_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "branch", "show", "remote", "rev-parse"},
    "pip": {"list", "show", "freeze", "--version", "-V"},
    "pip3": {"list", "show", "freeze", "--version", "-V"},
    "python": {"--version", "-V"}, "python3": {"--version", "-V"}, "py": {"--version", "-V"},
    "node": {"--version", "-v"}, "npm": {"list", "ls", "--version", "-v"},
    "winget": {"list", "search", "show", "--version"}, "apt": {"list", "search", "show", "policy"},
    "dpkg": {"-l", "-s", "-L"}, "flatpak": {"list", "search", "info"}, "snap": {"list", "find", "info"},
}
_WIN_BUILTINS = {"dir", "type", "echo", "del", "erase", "copy", "move", "ren", "rename", "set",
                 "ver", "cls", "mkdir", "md", "rmdir", "rd", "cd", "start", "where", "vol"}
_SHELL_OPS = re.compile(r"[|&;<>`]|\$\(|\$\{|&&|\|\|")


@dataclass
class CommandPlan:
    raw: str
    argv: list = field(default_factory=list)
    program: str = ""
    needs_shell: bool = False
    blocked: bool = False
    block_reason: str = ""
    read_only: bool = False
    risk: str = "critical"

    def describe(self) -> str:
        if self.blocked:
            return f"BLOCKED ({self.block_reason}): {self.raw}"
        mode = "shell" if self.needs_shell else "direct"
        ro = "read-only" if self.read_only else "may change the system"
        return f"[{mode}, {ro}] {self.raw}"


def analyze_command(command: str) -> CommandPlan:
    raw = (command or "").strip()
    plan = CommandPlan(raw=raw)
    if not raw:
        plan.blocked, plan.block_reason = True, "empty command"
        return plan
    low = raw.lower()
    for pat, why in BLOCKED_PATTERNS:
        if re.search(pat, low, re.I):
            plan.blocked, plan.block_reason = True, why
            return plan
    plan.needs_shell = bool(_SHELL_OPS.search(raw))
    try:
        plan.argv = shlex.split(raw, posix=not _IS_WIN)
    except ValueError as e:
        plan.blocked, plan.block_reason = True, f"could not parse command: {e}"
        return plan
    if not plan.argv:
        plan.blocked, plan.block_reason = True, "empty command"
        return plan
    prog = os.path.basename(plan.argv[0]).lower()
    if prog.endswith(".exe"):
        prog = prog[:-4]
    plan.program = prog
    if prog in ("sudo", "doas", "runas", "su") or "-verb runas" in low:
        plan.risk = "critical"
        plan.read_only = False
        return plan
    if not plan.needs_shell:
        if prog in READ_ONLY_PROGRAMS:
            plan.read_only = True
        elif prog in READ_ONLY_SUBCOMMANDS and len(plan.argv) > 1 and \
                plan.argv[1].lower() in READ_ONLY_SUBCOMMANDS[prog]:
            plan.read_only = True
    return plan


def run_command(plan: CommandPlan, timeout: int = 60, cwd: str | None = None) -> dict:
    """Execute an analysed, already-approved plan. Never runs a blocked plan."""
    if plan.blocked:
        return {"success": False, "exit_code": None, "output": "",
                "error": f"blocked: {plan.block_reason}"}
    t0 = time.time()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if plan.needs_shell:
            proc = subprocess.Popen(plan.raw, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, creationflags=flags)
        else:
            argv = list(plan.argv)
            if _IS_WIN and plan.program in _WIN_BUILTINS:
                argv = ["cmd", "/c"] + argv
            proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, creationflags=flags)
    except FileNotFoundError:
        return {"success": False, "exit_code": None, "output": "",
                "error": f"program not found: {plan.argv[0] if plan.argv else plan.raw}"}
    except Exception as e:
        return {"success": False, "exit_code": None, "output": "", "error": str(e)}
    STOP.track(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        timed_out = True
    finally:
        STOP.untrack(proc)
    output = (out or "")
    if err:
        output += ("\n[STDERR]\n" + err)
    return {"success": (proc.returncode == 0 and not timed_out), "exit_code": proc.returncode,
            "output": output[-6000:], "timed_out": timed_out,
            "error": ("timed out" if timed_out else ""), "seconds": round(time.time() - t0, 2)}
