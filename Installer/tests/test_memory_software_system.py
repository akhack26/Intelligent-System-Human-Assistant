import os
from pathlib import Path

import pytest

from isha_core import memory as M
from isha_core import software as S
from isha_core import system as SY
from isha_core import audit as A


def test_long_term_memory(tmp_path):
    lt = M.LongTermMemory(tmp_path / "m.json")
    assert lt.remember("mera favourite editor VS Code hai")["success"]
    assert lt.items()[0]["category"] == "preference"
    assert not lt.remember("mera password hunter2 hai")["success"]
    assert not lt.remember("card 4111 1111 1111 1111")["success"]
    assert "VS Code" in lt.format()
    lt2 = M.LongTermMemory(tmp_path / "m.json")            # persisted
    assert len(lt2.items()) == 1
    assert lt2.forget("editor")["removed"] == 1 and not lt2.items()


@pytest.mark.parametrize("text,tool", [
    ("yaad rakho ki main Python seekh raha hoon", "remember_fact"),
    ("remember that my project is called Jarvis", "remember_fact"),
    ("bhool jao editor wali baat", "forget_memory"),
    ("memory dikhao", "show_memory"),
    ("tumhe kya yaad hai", "show_memory"),
])
def test_parse_memory_commands(text, tool):
    assert M.parse_memory_command(text)[0] == tool


def test_memory_parser_does_not_steal_ram_question():
    assert M.parse_memory_command("kitni memory use ho rahi hai") is None


def test_working_memory_pronouns():
    w = M.WorkingMemory()
    w.note_tool("find_files", {}, "1 result(s):\n- a.pdf (x)\n  /home/u/Downloads/a.pdf")
    assert w.resolve_file() == "/home/u/Downloads/a.pdf"
    assert w.refers_back("is file ko Documents mein move karo")
    w.note_tool("open_app", {"app_name": "chrome"}, "ok")
    assert w.last_browser == "chrome"


def fake_which(avail):
    return lambda name: f"/usr/bin/{name}" if name in avail else None


def test_software_plan_windows_winget():
    sp = S.plan("install", "VS Code", which=fake_which({"winget"}), os_name="Windows")
    assert sp.ok and sp.manager == "winget" and sp.argv[:4] == ["winget", "install", "--id", "Microsoft.VisualStudioCode"]


def test_software_plan_linux_uses_pkexec_never_password():
    sp = S.plan("uninstall", "firefox", which=fake_which({"apt-get", "pkexec", "dpkg"}), os_name="Linux", is_root=False)
    assert sp.argv[:4] == ["pkexec", "apt-get", "remove", "-y"] and sp.elevated_with == "pkexec"
    sp = S.plan("install", "firefox", which=fake_which({"apt-get"}), os_name="Linux", is_root=False)
    assert sp.manual_command.startswith("sudo apt-get install")


def test_software_unknown_and_no_manager():
    assert "catalogue" in S.plan("install", "totally-unknown-app", which=fake_which({"winget"}), os_name="Windows").error
    assert S.plan("install", "vlc", managers=[]).error


def test_software_execute_verifies():
    sp = S.plan("install", "vlc", which=fake_which({"flatpak"}), os_name="Linux")
    state = {"installed": False}

    def runner(argv):
        if argv[1] == "install":
            state["installed"] = True
            return {"success": True, "exit_code": 0, "output": "done"}
        return {"success": state["installed"], "exit_code": 0 if state["installed"] else 1, "output": ""}

    res = S.execute(sp, runner=runner, which=fake_which({"flatpak"}))
    assert res["success"] and res["verified"] is True


def test_software_install_reported_failure_when_not_verified():
    sp = S.plan("install", "vlc", which=fake_which({"flatpak"}), os_name="Linux")
    res = S.execute(sp, runner=lambda argv: {"success": argv[1] == "install", "exit_code": 0, "output": ""},
                    which=fake_which({"flatpak"}))
    assert not res["success"] and res["verified"] is False


def test_linux_trash_empty_is_verified(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    ctl = SY.LinuxSystemController(home=tmp_path)
    trash = tmp_path / ".local/share/Trash"
    (trash / "files" / "dir").mkdir(parents=True)
    (trash / "info").mkdir(parents=True)
    (trash / "files" / "a.txt").write_text("x" * 100)
    (trash / "files" / "dir" / "b.txt").write_text("y")
    (trash / "info" / "a.txt.trashinfo").write_text("[Trash Info]")
    assert ctl.trash_status()["items"] == 2
    res = ctl.empty_trash()
    assert res["success"] and res["verified"] and res["remaining"] == 0
    assert ctl.empty_trash()["message"].startswith("Trash pehle se")


def test_audit_redacts_secrets(tmp_path):
    log = A.ActionLog(tmp_path / "a.jsonl")
    e = log.record("tool", tool="x", args={"password": "hunter2", "note": "key sk-abcdefghijklmnopqrstu"})
    assert e["args"]["password"] == "***redacted***" and "sk-" not in e["args"]["note"]
    assert "hunter2" not in (tmp_path / "a.jsonl").read_text()
