import pytest

from isha_core import coding as K
from isha_core.control import STOP


def test_python_calculator_template_runs(tmp_path):
    spec = K.choose_template("python", "TestApp", "calculator")
    res = K.write_project(tmp_path, spec)
    assert res["success"]
    entry = tmp_path / "TestApp" / "main.py"
    assert entry.is_file() and (tmp_path / "TestApp" / K.META_FILE).is_file()
    fr = K.fix_loop(entry, stdin_text=spec.test_input)
    assert fr.success and fr.status == "ok" and "= 14" in fr.output and "= 27" in fr.output


def test_web_project_files(tmp_path):
    res = K.write_project(tmp_path, K.choose_template("web", "MyWebsite", "portfolio site"))
    root = tmp_path / "MyWebsite"
    assert res["success"] and all((root / f).is_file() for f in ("index.html", "style.css", "script.js"))
    assert "style.css" in (root / "index.html").read_text()


def test_never_overwrites(tmp_path):
    K.write_project(tmp_path, K.choose_template("python", "App", ""))
    res = K.write_project(tmp_path, K.choose_template("python", "App", ""))
    assert not res["success"] and res["error"] == "exists"


def test_bad_names_rejected():
    for bad in ("", "..", "CON", "   "):
        with pytest.raises(ValueError):
            K.safe_name(bad)
    assert K.safe_name("my app") == "my_app"


def test_intentional_error_is_detected_fixed_and_rerun(tmp_path):
    f = tmp_path / "buggy.py"
    f.write_text("def add(a, b):\n    return a + b\n\nprint(ad(2, 3))\n")
    calls = []

    def fixer(code, error, lang, path):
        calls.append(error)
        assert "NameError" in error          # the model is given the real error
        return code.replace("ad(2, 3)", "add(2, 3)")

    fr = K.fix_loop(f, fixer=fixer, max_attempts=3)
    assert fr.success and fr.attempts == 2 and "5" in fr.output and len(calls) == 1
    assert "add(2, 3)" in f.read_text()


def test_syntax_error_caught_before_running(tmp_path):
    f = tmp_path / "s.py"
    f.write_text("print('hi'\n")
    fr = K.fix_loop(f, fixer=lambda c, e, l, p: "print('hi')\n", max_attempts=2)
    assert fr.success and fr.attempts == 2


def test_bounded_retries(tmp_path):
    f = tmp_path / "never.py"
    f.write_text("raise SystemExit(3)\n")
    n = []
    fr = K.fix_loop(f, fixer=lambda c, e, l, p: (n.append(1), c + "\n# try\n")[1], max_attempts=3)
    assert not fr.success and fr.status == "failed" and fr.attempts == 3 and len(n) == 2


def test_hard_cap(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("raise SystemExit(1)\n")
    fr = K.fix_loop(f, fixer=lambda c, e, l, p: c + "#\n", max_attempts=99)
    assert fr.attempts == K.HARD_MAX_ATTEMPTS


def test_blocked_code_is_not_executed(tmp_path):
    marker = tmp_path / "ran.txt"
    f = tmp_path / "evil.py"
    f.write_text(f"open(r'{marker}','w').write('x')\nimport os\nos.system('rm -rf /')\n")
    fr = K.fix_loop(f)
    assert fr.status == "blocked" and not marker.exists()


def test_fix_that_adds_risky_capability_needs_approval(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("print(undefined_name)\n")
    fr = K.fix_loop(f, fixer=lambda c, e, l, p: "import subprocess\nsubprocess.run(['echo','hi'])\n",
                    max_attempts=3)
    assert fr.status == "needs_approval" and "runs shell commands" in fr.needs_approval


def test_interactive_program_without_input_is_not_a_bug(tmp_path):
    f = tmp_path / "ask.py"
    f.write_text("name = input('name? ')\nprint('hi', name)\n")
    fr = K.fix_loop(f)
    assert fr.success


def test_stop_cancels(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("print(1)\n")
    STOP.trigger()
    assert K.fix_loop(f).status == "stopped"


def test_secrets_not_passed_to_generated_code(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    f = tmp_path / "env.py"
    f.write_text("import os\nprint(os.environ.get('OPENAI_API_KEY', 'absent'))\n")
    assert "absent" in K.run_capture(f)["stdout"]
