"""Install / uninstall / update software through OFFICIAL package managers.

Flow:  identify software -> pick an available package manager -> build an
argv (never a shell string) -> user approves -> run -> VERIFY -> report.

Sources: winget / choco / scoop on Windows, apt / dnf / pacman / zypper /
flatpak / snap on Linux, brew on macOS. ISHA never downloads an .exe from a
random website. A name that is not in the catalogue is looked up with the
package manager's own search and the user is shown candidates instead of
ISHA guessing a package id.

Privilege: Windows installers raise their own UAC prompt; on Linux, system
package managers run through ``pkexec`` (graphical polkit prompt) - ISHA
never sees or types a password. Without pkexec, the exact command is returned
for the user to run.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass, field

from . import commands

_OS = platform.system()

# name aliases -> canonical key
ALIASES = {
    "firefox": "firefox", "mozilla firefox": "firefox", "mozilla": "firefox",
    "vs code": "vscode", "vscode": "vscode", "visual studio code": "vscode", "code editor": "vscode",
    "chrome": "chrome", "google chrome": "chrome", "vlc": "vlc", "vlc player": "vlc",
    "git": "git", "python": "python", "node": "nodejs", "nodejs": "nodejs", "node js": "nodejs",
    "7zip": "7zip", "7-zip": "7zip", "notepad++": "notepadpp", "notepad plus plus": "notepadpp",
    "gimp": "gimp", "obs": "obs", "obs studio": "obs", "spotify": "spotify", "discord": "discord",
    "brave": "brave", "brave browser": "brave", "telegram": "telegram", "zoom": "zoom",
    "libreoffice": "libreoffice", "libre office": "libreoffice", "audacity": "audacity",
    "blender": "blender", "steam": "steam", "thunderbird": "thunderbird", "inkscape": "inkscape",
    "krita": "krita", "whatsapp": "whatsapp", "whatsapp desktop": "whatsapp", "curl": "curl",
    "ffmpeg": "ffmpeg", "htop": "htop", "java": "java", "openjdk": "java",
}

# canonical key -> {manager: package id}, plus the binary to verify with
CATALOG = {
    "firefox": {"winget": "Mozilla.Firefox", "choco": "firefox", "apt": "firefox", "dnf": "firefox",
                "pacman": "firefox", "zypper": "MozillaFirefox", "flatpak": "org.mozilla.firefox",
                "snap": "firefox", "brew": "--cask firefox", "_bin": ["firefox"]},
    "vscode": {"winget": "Microsoft.VisualStudioCode", "choco": "vscode", "flatpak": "com.visualstudio.code",
               "snap": "code --classic", "brew": "--cask visual-studio-code", "pacman": "code",
               "_bin": ["code"]},
    "chrome": {"winget": "Google.Chrome", "choco": "googlechrome", "flatpak": "com.google.Chrome",
               "brew": "--cask google-chrome", "_bin": ["google-chrome", "google-chrome-stable", "chrome"]},
    "vlc": {"winget": "VideoLAN.VLC", "choco": "vlc", "apt": "vlc", "dnf": "vlc", "pacman": "vlc",
            "zypper": "vlc", "flatpak": "org.videolan.VLC", "snap": "vlc", "brew": "--cask vlc", "_bin": ["vlc"]},
    "git": {"winget": "Git.Git", "choco": "git", "apt": "git", "dnf": "git", "pacman": "git", "zypper": "git",
            "brew": "git", "_bin": ["git"]},
    "python": {"winget": "Python.Python.3.12", "choco": "python", "apt": "python3", "dnf": "python3",
               "pacman": "python", "zypper": "python3", "brew": "python", "_bin": ["python3", "python"]},
    "nodejs": {"winget": "OpenJS.NodeJS.LTS", "choco": "nodejs-lts", "apt": "nodejs", "dnf": "nodejs",
               "pacman": "nodejs", "zypper": "nodejs", "brew": "node", "snap": "node --classic", "_bin": ["node"]},
    "7zip": {"winget": "7zip.7zip", "choco": "7zip", "apt": "p7zip-full", "dnf": "p7zip", "pacman": "p7zip",
             "_bin": ["7z", "7za"]},
    "notepadpp": {"winget": "Notepad++.Notepad++", "choco": "notepadplusplus", "_bin": ["notepad++"]},
    "gimp": {"winget": "GIMP.GIMP", "choco": "gimp", "apt": "gimp", "dnf": "gimp", "pacman": "gimp",
             "flatpak": "org.gimp.GIMP", "snap": "gimp", "_bin": ["gimp"]},
    "obs": {"winget": "OBSProject.OBSStudio", "choco": "obs-studio", "apt": "obs-studio", "dnf": "obs-studio",
            "pacman": "obs-studio", "flatpak": "com.obsproject.Studio", "_bin": ["obs"]},
    "spotify": {"winget": "Spotify.Spotify", "flatpak": "com.spotify.Client", "snap": "spotify", "_bin": ["spotify"]},
    "discord": {"winget": "Discord.Discord", "flatpak": "com.discordapp.Discord", "snap": "discord", "_bin": ["discord"]},
    "brave": {"winget": "Brave.Brave", "flatpak": "com.brave.Browser", "snap": "brave", "_bin": ["brave-browser", "brave"]},
    "telegram": {"winget": "Telegram.TelegramDesktop", "flatpak": "org.telegram.desktop",
                 "snap": "telegram-desktop", "_bin": ["telegram-desktop"]},
    "zoom": {"winget": "Zoom.Zoom", "flatpak": "us.zoom.Zoom", "_bin": ["zoom"]},
    "libreoffice": {"winget": "TheDocumentFoundation.LibreOffice", "apt": "libreoffice", "dnf": "libreoffice",
                    "pacman": "libreoffice-fresh", "flatpak": "org.libreoffice.LibreOffice", "_bin": ["libreoffice", "soffice"]},
    "audacity": {"winget": "Audacity.Audacity", "apt": "audacity", "dnf": "audacity", "pacman": "audacity",
                 "flatpak": "org.audacityteam.Audacity", "_bin": ["audacity"]},
    "blender": {"winget": "BlenderFoundation.Blender", "apt": "blender", "dnf": "blender", "pacman": "blender",
                "flatpak": "org.blender.Blender", "snap": "blender --classic", "_bin": ["blender"]},
    "steam": {"winget": "Valve.Steam", "apt": "steam", "pacman": "steam", "flatpak": "com.valvesoftware.Steam", "_bin": ["steam"]},
    "thunderbird": {"winget": "Mozilla.Thunderbird", "apt": "thunderbird", "dnf": "thunderbird",
                    "pacman": "thunderbird", "flatpak": "org.mozilla.Thunderbird", "_bin": ["thunderbird"]},
    "inkscape": {"winget": "Inkscape.Inkscape", "apt": "inkscape", "dnf": "inkscape", "pacman": "inkscape",
                 "flatpak": "org.inkscape.Inkscape", "_bin": ["inkscape"]},
    "krita": {"winget": "KDE.Krita", "apt": "krita", "flatpak": "org.kde.krita", "_bin": ["krita"]},
    "whatsapp": {"winget": "9NKSQGP7F2NH", "_bin": []},
    "curl": {"winget": "cURL.cURL", "apt": "curl", "dnf": "curl", "pacman": "curl", "_bin": ["curl"]},
    "ffmpeg": {"winget": "Gyan.FFmpeg", "choco": "ffmpeg", "apt": "ffmpeg", "dnf": "ffmpeg", "pacman": "ffmpeg", "_bin": ["ffmpeg"]},
    "htop": {"apt": "htop", "dnf": "htop", "pacman": "htop", "brew": "htop", "_bin": ["htop"]},
    "java": {"winget": "EclipseAdoptium.Temurin.21.JDK", "apt": "default-jdk", "dnf": "java-latest-openjdk",
             "pacman": "jdk-openjdk", "_bin": ["java"]},
}

WINDOWS_MANAGERS = ("winget", "choco", "scoop")
LINUX_NATIVE = ("apt", "dnf", "pacman", "zypper")
LINUX_MANAGERS = LINUX_NATIVE + ("flatpak", "snap")
SYSTEM_MANAGERS = {"apt", "dnf", "pacman", "zypper", "snap"}


def _which(name, which=shutil.which):
    if name == "apt":
        return which("apt-get")
    return which(name)


def detect_managers(which=shutil.which, os_name: str = _OS) -> list:
    pool = WINDOWS_MANAGERS if os_name == "Windows" else ("brew",) if os_name == "Darwin" else LINUX_MANAGERS
    return [m for m in pool if _which(m, which)]


def canonical(name: str) -> tuple:
    """(catalog_key or None, cleaned name)"""
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    n = re.sub(r"\b(app|application|software|program|browser\s+app|latest|version|ko|ka|ki)\b", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" .")
    if n in ALIASES:
        return ALIASES[n], n
    if n in CATALOG:
        return n, n
    for alias, key in ALIASES.items():
        if re.fullmatch(rf"{re.escape(alias)}(\s+\w+)?", n):
            return key, n
    return None, n


@dataclass
class SoftwarePlan:
    action: str                       # install | uninstall | update
    name: str
    key: str | None
    manager: str | None = None
    package: str | None = None
    argv: list = field(default_factory=list)
    verify_argv: list = field(default_factory=list)
    binaries: list = field(default_factory=list)
    elevated_with: str = ""           # "pkexec" | "uac" | ""
    manual_command: str = ""          # when ISHA cannot elevate itself
    error: str = ""
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.argv)

    def describe(self) -> str:
        if self.error:
            return self.error
        head = f"{self.action} '{self.name}' via {self.manager} (package {self.package})"
        if self.elevated_with == "pkexec":
            head += " - the system will ask for your password"
        elif self.elevated_with == "uac":
            head += " - Windows may show a UAC prompt"
        return head + ("\n" + "; ".join(self.notes) if self.notes else "")


def _pkg_parts(pkg: str) -> list:
    return pkg.split()


def _argv_for(manager: str, action: str, pkg: str) -> list:
    parts = _pkg_parts(pkg)
    first = parts[0] if not parts[0].startswith("--") else parts[-1]
    if manager == "winget":
        verb = {"install": "install", "uninstall": "uninstall", "update": "upgrade"}[action]
        argv = ["winget", verb, "--id", pkg, "-e"]
        if action != "uninstall":
            argv += ["--accept-package-agreements", "--accept-source-agreements"]
        return argv
    if manager == "choco":
        verb = {"install": "install", "uninstall": "uninstall", "update": "upgrade"}[action]
        return ["choco", verb, pkg, "-y"]
    if manager == "scoop":
        verb = {"install": "install", "uninstall": "uninstall", "update": "update"}[action]
        return ["scoop", verb, pkg]
    if manager == "apt":
        if action == "install":
            return ["apt-get", "install", "-y", pkg]
        if action == "uninstall":
            return ["apt-get", "remove", "-y", pkg]
        return ["apt-get", "install", "--only-upgrade", "-y", pkg]
    if manager == "dnf":
        verb = {"install": "install", "uninstall": "remove", "update": "upgrade"}[action]
        return ["dnf", verb, "-y", pkg]
    if manager == "zypper":
        verb = {"install": "install", "uninstall": "remove", "update": "update"}[action]
        return ["zypper", "--non-interactive", verb, pkg]
    if manager == "pacman":
        verb = {"install": ["-S", "--needed"], "uninstall": ["-R"], "update": ["-S"]}[action]
        return ["pacman"] + verb + ["--noconfirm", pkg]
    if manager == "flatpak":
        if action == "install":
            return ["flatpak", "install", "--user", "-y", "flathub", pkg]
        if action == "uninstall":
            return ["flatpak", "uninstall", "-y", pkg]
        return ["flatpak", "update", "-y", pkg]
    if manager == "snap":
        verb = {"install": "install", "uninstall": "remove", "update": "refresh"}[action]
        return ["snap", verb] + ([first] if action != "install" else parts)
    if manager == "brew":
        verb = {"install": "install", "uninstall": "uninstall", "update": "upgrade"}[action]
        return ["brew", verb] + parts
    raise ValueError(f"unknown manager {manager}")


def _verify_argv(manager: str, pkg: str) -> list:
    first = [p for p in pkg.split() if not p.startswith("--")][0]
    return {
        "winget": ["winget", "list", "--id", pkg, "-e"],
        "choco": ["choco", "list", "--local-only", "--exact", pkg],
        "scoop": ["scoop", "list", pkg],
        "apt": ["dpkg", "-s", pkg],
        "dnf": ["rpm", "-q", pkg],
        "zypper": ["rpm", "-q", pkg],
        "pacman": ["pacman", "-Q", pkg],
        "flatpak": ["flatpak", "info", pkg],
        "snap": ["snap", "list", first],
        "brew": ["brew", "list", first],
    }.get(manager, [])


def plan(action: str, name: str, managers: list | None = None, preferred: str = "",
         which=shutil.which, os_name: str = _OS, is_root: bool | None = None) -> SoftwarePlan:
    action = {"download": "install", "remove": "uninstall", "upgrade": "update"}.get(action, action)
    key, clean = canonical(name)
    sp = SoftwarePlan(action, clean or name, key)
    if managers is None:
        managers = detect_managers(which, os_name)
    if not managers:
        sp.error = ("Is system par koi supported package manager nahi mila "
                    + ("(winget / choco / scoop)." if os_name == "Windows" else "(apt / dnf / pacman / flatpak / snap)."))
        return sp
    if key is None:
        sp.error = (f"'{clean}' mere software catalogue me nahi hai. Main package manager me search "
                    f"kar sakti hoon (search_software) aur sahi package chunne ke liye options dikha sakti hoon.")
        return sp
    entry = CATALOG[key]
    sp.binaries = list(entry.get("_bin", []))
    order = list(managers)
    if preferred and preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    chosen = next((m for m in order if m in entry), None)
    if chosen is None:
        sp.error = (f"'{clean}' ke liye available package managers ({', '.join(managers)}) me koi "
                    f"official package entry nahi mili.")
        return sp
    pkg = entry[chosen]
    sp.manager, sp.package = chosen, pkg
    argv = _argv_for(chosen, action, pkg)
    if os_name == "Windows":
        sp.elevated_with = "uac" if chosen in ("winget", "choco") else ""
    elif chosen in SYSTEM_MANAGERS:
        root = (os.geteuid() == 0) if (is_root is None and hasattr(os, "geteuid")) else bool(is_root)
        if not root:
            if which("pkexec"):
                argv = ["pkexec"] + argv
                sp.elevated_with = "pkexec"
            else:
                sp.manual_command = "sudo " + " ".join(argv)
                sp.notes.append("pkexec nahi mila - yeh command khud terminal me chalao")
    if chosen == "winget" and action != "uninstall":
        sp.notes.append("winget publisher ka license agreement accept karega")
    if chosen == "apt" and key == "vscode":
        sp.notes.append("VS Code apt me Microsoft repo ke bina nahi milta")
    sp.argv = argv
    sp.verify_argv = _verify_argv(chosen, pkg)
    return sp


def is_installed(sp: SoftwarePlan, runner=None, which=shutil.which) -> bool | None:
    """True / False / None (could not tell)."""
    runner = runner or (lambda argv: commands.run_command(commands.CommandPlan(raw=" ".join(argv), argv=argv,
                                                                                  program=argv[0]), timeout=60))
    if sp.verify_argv and which(sp.verify_argv[0]):
        res = runner(sp.verify_argv)
        out = (res.get("output") or "").lower()
        if sp.manager == "winget":
            return bool(res.get("success")) and (sp.package or "").lower() in out
        if sp.manager == "choco":
            return bool(res.get("success")) and (sp.package or "").lower() in out and "0 packages" not in out
        return bool(res.get("success"))
    for b in sp.binaries:
        if which(b):
            return True
    return None


def execute(sp: SoftwarePlan, runner=None, which=shutil.which, timeout: int = 1200) -> dict:
    if not sp.ok:
        return {"success": False, "message": sp.error or "No executable plan.", "verified": None}
    if sp.manual_command:
        return {"success": False, "verified": None, "manual": True,
                "message": f"Admin rights chahiye. Terminal me yeh chalao:\n{sp.manual_command}"}
    runner = runner or (lambda argv: commands.run_command(
        commands.CommandPlan(raw=" ".join(argv), argv=argv, program=argv[0]), timeout=timeout))
    before = is_installed(sp, runner=runner, which=which) if sp.action != "install" else None
    res = runner(sp.argv)
    after = is_installed(sp, runner=runner, which=which)
    if sp.action == "uninstall":
        verified = (after is False) if after is not None else None
    else:
        verified = after if after is not None else None
    ok = bool(res.get("success")) and verified is not False
    tail = (res.get("output") or res.get("error") or "").strip()[-600:]
    if ok and verified:
        msg = f"'{sp.name}' {sp.action} ho gaya aur verify bhi kar liya ({sp.manager})."
    elif ok:
        msg = f"'{sp.name}' ka {sp.action} command successful raha ({sp.manager}), par main verify nahi kar paayi."
    else:
        msg = f"'{sp.name}' {sp.action} fail hua ({sp.manager}, exit {res.get('exit_code')}).\n{tail}"
    return {"success": ok, "verified": verified, "message": msg, "was_installed": before,
            "exit_code": res.get("exit_code")}


def search(name: str, managers: list | None = None, runner=None, which=shutil.which,
           os_name: str = _OS, limit: int = 8) -> dict:
    managers = managers if managers is not None else detect_managers(which, os_name)
    runner = runner or (lambda argv: commands.run_command(
        commands.CommandPlan(raw=" ".join(argv), argv=argv, program=argv[0]), timeout=90))
    q = (name or "").strip()
    if not q:
        return {"success": False, "message": "Search ke liye naam do."}
    for m in managers:
        argv = {"winget": ["winget", "search", q, "--accept-source-agreements"],
                "choco": ["choco", "search", q, "--limit-output"],
                "scoop": ["scoop", "search", q],
                "apt": ["apt-cache", "search", "--names-only", q],
                "dnf": ["dnf", "search", "-q", q],
                "pacman": ["pacman", "-Ss", q],
                "zypper": ["zypper", "--non-interactive", "search", q],
                "flatpak": ["flatpak", "search", q],
                "snap": ["snap", "find", q],
                "brew": ["brew", "search", q]}.get(m)
        if not argv or not which(argv[0]):
            continue
        res = runner(argv)
        lines = [ln for ln in (res.get("output") or "").splitlines() if ln.strip() and "[STDERR]" not in ln]
        if lines:
            return {"success": True, "manager": m, "results": lines[:limit],
                    "message": f"{m} results for '{q}':\n" + "\n".join(lines[:limit])}
    return {"success": False, "message": f"'{q}' ke liye koi package nahi mila."}
