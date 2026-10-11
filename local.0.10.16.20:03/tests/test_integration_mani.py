"""End-to-end tests against the real mani.py module (headless).

These drive the same functions the GUI calls: plan_request() for deterministic
routing, run_tool_gated() for permission + execution, pick_cfg_for() for model
routing and AgentWorker._loop() with a scripted fake model for the agent loop.
"""
import json
import time
from pathlib import Path

import pytest


def tool_of(mani, text):
    plan = mani.plan_request(text)
    assert len(plan) == 1, plan
    return plan[0]["tool"], plan[0]["args"]


def run(mani, text, ask=None, cfg=None):
    name, args = tool_of(mani, text)
    assert name, f"no deterministic tool for {text!r}"
    return mani.run_tool_gated(name, args, ask=ask, cfg=cfg)


# ---- acceptance test 2: file search returns actual files -------------------
def test_downloads_pdf_search_returns_real_files(mani, home):
    (home / "Downloads" / "bank_statement_2025.pdf").write_bytes(b"%PDF-1.4")
    (home / "Downloads" / "photo.jpg").write_bytes(b"x")
    ok, out, info = run(mani, "Downloads mein PDF dhundo")
    assert ok and info["approval"] == "auto"
    assert "bank_statement_2025.pdf" in out and "photo.jpg" not in out
    assert str(home / "Downloads") in out
    # the path went into working memory for a follow-up
    assert mani.WORKING.resolve_file().endswith("bank_statement_2025.pdf")


def test_follow_up_move_uses_working_memory(mani, home):
    (home / "Downloads" / "move_me.pdf").write_bytes(b"%PDF")
    assert run(mani, "Downloads mein move_me pdf dhundo")[0]
    name, args = tool_of(mani, "Is file ko Documents mein move karo")
    assert name == "move_or_rename_file" and args["dest_path"].endswith("Documents/move_me.pdf")
    ok, out, info = mani.run_tool_gated(name, args, ask=lambda *a: True)
    assert ok and info["approval"] == "user"
    assert (home / "Documents" / "move_me.pdf").exists()


# ---- acceptance tests 3, 4, 5: project create / run / auto-fix -------------
def test_create_python_project_on_desktop(mani, home):
    ok, out, _ = run(mani, "Desktop par TestApp naam ka Python project banao.")
    assert ok, out
    assert (home / "Desktop" / "TestApp" / "main.py").is_file()


def test_create_web_project(mani, home):
    ok, out, _ = run(mani, "Desktop par MyWebsite naam ka HTML CSS JS project bana do.")
    assert ok, out
    for f in ("index.html", "style.css", "script.js"):
        assert (home / "Desktop" / "MyWebsite" / f).is_file()


def test_run_project_needs_approval_and_really_runs(mani, home):
    run(mani, "Desktop par Calc2 naam ka calculator project banao")
    name, args = tool_of(mani, "Calc2 run karo")
    assert name == "run_project"
    ok, out, _ = mani.run_tool_gated(name, args)                 # no UI -> critical is refused
    assert not ok and out.startswith("DENIED")
    args["open_window"] = False
    ok, out, info = mani.run_tool_gated(name, args, ask=lambda *a: True)
    assert ok and info["approval"] == "user", out
    assert "successfully chala" in out and "= 14" in out


def test_intentional_error_detect_fix_retry(mani, home, monkeypatch):
    proj = home / "Desktop" / "BuggyApp"
    proj.mkdir()
    (proj / "main.py").write_text("total = sum([1, 2, 3])\nprint('total =', totl)\n")
    seen = []

    def fake_fixer(code, error, lang, path):
        seen.append(error)
        return code.replace("totl", "total")

    monkeypatch.setattr(mani, "_LLAMA_CPP_OK", True)
    monkeypatch.setattr(mani, "_code_fixer", fake_fixer)
    ok, out, _ = mani.run_tool_gated("run_project", {"project": "BuggyApp", "open_window": False},
                                     ask=lambda *a: True)
    assert ok, out
    assert "auto-fix" in out and "total = 6" in out
    assert "NameError" in seen[0]
    assert "print('total =', total)" in (proj / "main.py").read_text()


def test_fix_attempts_are_bounded_and_failure_reported(mani, home, monkeypatch):
    proj = home / "Desktop" / "Hopeless"
    proj.mkdir()
    (proj / "main.py").write_text("raise RuntimeError('always')\n")
    monkeypatch.setattr(mani, "_LLAMA_CPP_OK", True)
    monkeypatch.setattr(mani, "_code_fixer", lambda c, e, l, p: c + "\n# attempt\n")
    ok, out, _ = mani.run_tool_gated("run_project", {"project": "Hopeless", "max_attempts": 2,
                                                     "open_window": False}, ask=lambda *a: True)
    assert not ok and "2 attempt" in out and "RuntimeError" in out


# ---- acceptance test 7: recycle bin asks, executes, verifies ---------------
def test_recycle_bin_confirm_flow(mani, home):
    trash = home / ".local" / "share" / "Trash"
    (trash / "files").mkdir(parents=True, exist_ok=True)
    (trash / "info").mkdir(parents=True, exist_ok=True)
    (trash / "files" / "old.txt").write_text("bye")
    asked = []
    ok, out, _ = run(mani, "Recycle Bin clean karo.", ask=lambda t, a, r: (asked.append((t, r)), False)[1])
    assert not ok and out.startswith("DENIED") and asked == [("empty_recycle_bin", "confirm")]
    assert (trash / "files" / "old.txt").exists()                 # nothing happened
    ok, out, _ = run(mani, "Recycle Bin clean karo.", ask=lambda *a: True)
    assert ok and "verified" in out and not any((trash / "files").iterdir())


# ---- acceptance test 8: real system info ------------------------------------
def test_ram_usage_is_real(mani):
    ok, out, _ = run(mani, "RAM kitni use ho rahi hai?")
    assert ok and "MB" in out and "%" in out


def test_system_information(mani):
    ok, out, _ = run(mani, "System information batao.")
    assert ok and "CPU" in out and "RAM" in out and "Mode:" in out


# ---- acceptance tests 9, 10: model routing ------------------------------------
def test_router_picks_reasoning_and_coding_models(mani, tmp_path):
    paths = {}
    for role in ("general", "coding", "reasoning", "fast"):
        p = tmp_path / f"{role}.gguf"
        p.write_bytes(b"\0" * 64)
        paths[role] = str(p)
    cfg = mani.load_config()
    cfg["models"] = dict(paths)
    cfg["model_mode"] = "auto"
    assert mani.pick_cfg_for("Ek reasoning problem solve karo", cfg)["gguf_model_path"] == paths["reasoning"]
    assert mani.pick_cfg_for("Python ka program bana do", cfg)["gguf_model_path"] == paths["coding"]
    assert mani.pick_cfg_for("Chrome kholo", cfg)["gguf_model_path"] == paths["fast"]
    assert mani.ACTIVE_MODEL["name"] == "fast.gguf"
    cfg["model_mode"] = "coding"                                  # manual override from the GUI
    assert mani.pick_cfg_for("Isha kaise ho", cfg)["gguf_model_path"] == paths["coding"]


# ---- WhatsApp: preview + confirmation, trusted switch ------------------------
@pytest.mark.parametrize("text,contact,msg", [
    ("Rahul ko WhatsApp karo ki main 10 minute late aaunga", "Rahul", "main 10 minute late aaunga"),
    ("rahul ko whatsapp par message bhejo ki kal milte hain", "rahul", "kal milte hain"),
    ("Mummy ko WhatsApp par 'khana bana lena' bhej do", "Mummy", "khana bana lena"),
    ("send a whatsapp message to Priya saying I am on my way", "Priya", "I am on my way"),
    ("whatsapp par papa ko bolo ki main pahunch gaya", "papa", "main pahunch gaya"),
])
def test_whatsapp_parser_variants(mani, text, contact, msg):
    name, args = mani.parse_whatsapp_command(text)
    assert name == "send_whatsapp_message"
    assert args.get("contact", "").lower() == contact.lower()
    assert args.get("message", "").lower() == msg.lower()


def test_whatsapp_confirmation_preview_and_no_send_when_denied(mani, monkeypatch):
    sent = []
    monkeypatch.setattr(mani, "_wa_send_via_web", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(mani, "_wa_send_via_desktop", lambda *a, **k: sent.append(a))
    preview = mani.describe_tool_call("send_whatsapp_message",
                                      {"contact": "Rahul", "message": "Main 10 minute late aaunga."})
    assert preview.startswith("Rahul ko WhatsApp par ye message bhejna hai") and "Send karun?" in preview
    ok, out, _ = mani.run_tool_gated("send_whatsapp_message", {"contact": "Rahul", "message": "hi"},
                                     ask=lambda *a: False)
    assert not ok and not sent
    cfg = mani.load_config()
    cfg["trusted_messaging"] = True
    assert mani.isha_perm.decide("send_whatsapp_message", "confirm", cfg).action == "allow"


# ---- permission & audit --------------------------------------------------------
def test_audit_log_records_tool_runs(mani):
    mani.run_tool_gated("get_time", {})
    last = mani.ACTION_LOG.recent(1, kind="tool")[0]
    assert last["tool"] == "get_time" and last["success"] and last["approval"] == "auto"
    assert "duration_ms" in last and last["risk"] == "safe"


def test_terminal_command_blocked_even_when_approved(mani):
    ok, out, _ = mani.run_tool_gated("run_terminal_command", {"command": "rm -rf /"}, ask=lambda *a: True)
    assert not ok and "blocked" in out


def test_user_approval_is_the_confirmation(mani, home):
    f = home / "Desktop" / "delete_me.txt"
    f.write_text("x")
    ok, out, _ = mani.run_tool_gated("delete_file_safely", {"file_path": str(f)}, ask=lambda *a: True)
    assert ok and not f.exists(), out


# ---- memory ------------------------------------------------------------------------
def test_remember_forget_show(mani):
    assert run(mani, "yaad rakho ki mera favourite editor VS Code hai")[0]
    ok, out, _ = run(mani, "memory dikhao")
    assert ok and "VS Code" in out
    assert run(mani, "bhool jao editor")[0]
    assert "VS Code" not in run(mani, "memory dikhao")[1]


# ---- agent loop with a scripted fake model -----------------------------------------
class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, messages, options=None, cfg=None, tools=None, json_mode=False):
        self.seen.append(messages)
        r = self.replies.pop(0)
        return {"message": r}


def call(name, **args):
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": json.dumps(args)}}]}


def test_agent_plan_act_observe_complete(mani, home, monkeypatch):
    (home / "Desktop" / "agent_target.py").write_text("print(1)")
    fake = ScriptedModel([call("find_files", query="agent_target.py desktop par"),
                          {"content": "Mil gayi: Desktop par agent_target.py hai.", "tool_calls": []}])
    monkeypatch.setattr(mani.GGUF, "complete", fake)
    monkeypatch.setattr(mani, "_LLAMA_CPP_OK", True)
    w = mani.AgentWorker(messages=[{"role": "user", "content": "meri agent_target file dhundo"}],
                         options={}, cfg=mani.load_config(), max_steps=5)
    final = w._loop()
    assert "agent_target.py" in final
    assert w.trace[0]["tool"] == "find_files" and "agent_target.py" in w.trace[0]["result"]
    # the observation was fed back to the model before its final answer
    assert any("agent_target.py" in str(m.get("content")) for m in fake.seen[1] if m.get("role") == "tool")


def test_agent_failure_then_retry(mani, monkeypatch):
    fake = ScriptedModel([call("read_file_content", file_path="/definitely/missing.txt"),
                          call("get_date"),
                          {"content": "File nahi mili, par aaj ki date bata di.", "tool_calls": []}])
    monkeypatch.setattr(mani.GGUF, "complete", fake)
    w = mani.AgentWorker(messages=[{"role": "user", "content": "x"}], options={}, cfg=mani.load_config())
    final = w._loop()
    assert w.trace[0]["result"].startswith("ERROR") and w.trace[1]["tool"] == "get_date"
    assert "date" in final


def test_agent_step_budget_is_bounded(mani, monkeypatch):
    fake = ScriptedModel([call("get_time")] * 3 + [{"content": "2 kaam kiye.", "tool_calls": []}])
    monkeypatch.setattr(mani.GGUF, "complete", fake)
    w = mani.AgentWorker(messages=[{"role": "user", "content": "x"}], options={}, cfg=mani.load_config(),
                         max_steps=3)
    w._loop()
    assert len(w.trace) == 3


def test_agent_stop(mani, monkeypatch):
    fake = ScriptedModel([call("get_time")] * 5)
    monkeypatch.setattr(mani.GGUF, "complete", fake)
    w = mani.AgentWorker(messages=[{"role": "user", "content": "x"}], options={}, cfg=mani.load_config())
    mani.ISHA_STOP.trigger()
    assert w._loop() == "Rok diya." and not w.trace


def test_agent_denied_tool_is_not_run(mani, home, monkeypatch):
    f = home / "Desktop" / "keep.txt"
    f.write_text("keep")
    fake = ScriptedModel([call("delete_file_safely", file_path=str(f)),
                          {"content": "Theek hai, cancel kar diya.", "tool_calls": []}])
    monkeypatch.setattr(mani.GGUF, "complete", fake)
    w = mani.AgentWorker(messages=[{"role": "user", "content": "delete"}], options={}, cfg=mani.load_config())
    w.permission_needed.connect(lambda req: req.answer(False))       # user clicks "No"
    w._loop()
    assert f.exists() and w.trace[0]["result"].startswith("DENIED")


def test_plan_executor_compound_command(mani, monkeypatch):
    steps = mani.plan_request("time batao aur date batao")
    assert [s["tool"] for s in steps] == ["get_time", "get_date"]
    w = mani.PlanExecutorWorker(steps=steps, options={}, cfg=mani.load_config())
    monkeypatch.setattr(w, "STEP_GAP_SEC", 0)
    report = w._execute_plan()
    assert report.count("✅") == 2


# ---- regression: OSTaskWorker must deliver string results -----------------------------
def test_os_task_worker_delivers_str(mani):
    got = []
    w = mani.OSTaskWorker(lambda: "Notepad khol diya.")
    w.task_finished.connect(lambda r: got.append(r))
    w.task_error.connect(lambda e: got.append(("ERR", e)))
    w.run()
    assert got == ["Notepad khol diya."]


# ---- diagnostics ---------------------------------------------------------------
def test_self_diagnostics_statuses(mani):
    rep = mani.run_self_diagnostics(mani.load_config())
    st = rep["status"]
    for comp in ("Python", "OS", "CPU", "RAM", "GPU", "VRAM", "Model files", "Model backend", "TTS", "STT",
                 "Microphone", "Camera", "File search", "Memory", "Tool registry", "WhatsApp",
                 "Browser automation", "System control", "Vision"):
        assert comp in st and st[comp][0] in ("READY", "DEGRADED", "UNAVAILABLE")
    assert st["Model backend"][0] == "UNAVAILABLE"          # honest: backend skipped in tests
    assert "Components:" in mani.format_self_diagnostics(rep)


def test_capabilities_categories(mani):
    out = mani.execute_tool("list_capabilities", {})
    for cat in ("files", "applications", "system", "network", "media", "messaging", "coding",
                "vision", "memory", "automation"):
        assert f"{cat}:" in out
    assert "find_files" in mani.execute_tool("list_capabilities", {"category": "FILES".lower()})
