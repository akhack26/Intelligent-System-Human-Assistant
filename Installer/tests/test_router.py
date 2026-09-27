from pathlib import Path

import pytest

from isha_core import router as R
from isha_core.hardware import HardwareInfo


@pytest.mark.parametrize("text,task", [
    ("Python mein calculator bana do", R.TaskType.CODING),
    ("Is Python code ka bug fix karo", R.TaskType.DEBUGGING),
    ("Mujhe relativity samjhao", R.TaskType.STUDY),
    ("Ek reasoning problem solve karo", R.TaskType.REASONING),
    ("Downloads folder mein meri PDF dhundo", R.TaskType.FILE_SEARCH),
    ("Chrome kholo", R.TaskType.QUICK_COMMAND),
    ("Kitni RAM use ho rahi hai?", R.TaskType.SYSTEM_CONTROL),
    ("Rahul ko WhatsApp par message bhejo", R.TaskType.MESSAGING),
    ("What is on my screen?", R.TaskType.VISION),
    ("Isha, kya haal hai?", R.TaskType.GENERAL_CHAT),
    ("Ek kavita likho baarish par", R.TaskType.CREATIVE),
    ("Kal ka plan banao", R.TaskType.PLANNING),
    ("Python ka program bana do", R.TaskType.CODING),
    ("Calculator app bana do", R.TaskType.CODING),
    ("VS Code download karo", R.TaskType.SYSTEM_CONTROL),
])
def test_classify(text, task):
    assert R.classify_task(text).task == task


def _models(tmp_path, roles, size=1024):
    d = tmp_path / "models"
    cfg = {"models": {}}
    for role in roles:
        (d / role).mkdir(parents=True, exist_ok=True)
        p = d / role / f"{role}-model.gguf"
        p.write_bytes(b"\0" * size)
        cfg["models"][role] = str(p)
    return cfg, d


def test_route_to_role_models(tmp_path):
    cfg, d = _models(tmp_path, ["general", "coding", "reasoning", "fast"])
    reg = R.ModelRegistry(cfg, d, tmp_path)
    router = R.ModelRouter(reg)
    assert router.route("Python mein calculator bana do").role == "coding"
    assert router.route("Chrome kholo").role == "fast"
    assert router.route("Ek reasoning problem solve karo").role == "reasoning"
    assert router.route("Isha kaise ho").role == "general"
    # study falls back to reasoning when no study model exists
    dec = router.route("Mujhe relativity samjhao")
    assert dec.requested_role == "study" and dec.role == "reasoning"


def test_manual_override(tmp_path):
    cfg, d = _models(tmp_path, ["general", "coding"])
    router = R.ModelRouter(R.ModelRegistry(cfg, d, tmp_path))
    dec = router.route("Isha kaise ho", mode="coding")
    assert dec.manual and dec.role == "coding"


def test_vision_never_falls_back_to_text_model(tmp_path):
    cfg, d = _models(tmp_path, ["general"])
    dec = R.ModelRouter(R.ModelRegistry(cfg, d, tmp_path)).route("what is on my screen")
    assert dec.task == R.TaskType.VISION and dec.path is None and not dec.available


def test_missing_file_is_not_claimed(tmp_path):
    cfg = {"models": {"coding": str(tmp_path / "nope.gguf")}}
    reg = R.ModelRegistry(cfg, tmp_path, tmp_path)
    assert reg.get("coding") is None
    assert any(r["role"] == "coding" and r["status"] == "MISSING FILE" for r in reg.describe())


def test_legacy_keys_still_work(tmp_path):
    g = tmp_path / "g.gguf"; g.write_bytes(b"x")
    c = tmp_path / "c.gguf"; c.write_bytes(b"x")
    reg = R.ModelRegistry({"gguf_model_path": str(g), "code_model_path": str(c)}, tmp_path, tmp_path)
    router = R.ModelRouter(reg)
    assert router.route("python script likho").path == c
    assert router.route("hello").path == g


def test_low_memory_falls_back_to_smaller_model(tmp_path):
    cfg, d = _models(tmp_path, ["general", "fast"])
    big = Path(cfg["models"]["general"])
    big.write_bytes(b"\0" * (3 * 1024 * 1024))          # 3 MB "big" model
    hw = HardwareInfo(ram_total_gb=8, ram_available_gb=0.001 + 1.5 + (1.5 / 1024))   # ~1.5 MB usable
    router = R.ModelRouter(R.ModelRegistry(cfg, d, tmp_path), hw)
    dec = router.route("Isha kaise ho")
    assert dec.role == "fast"
    assert any("does not fit" in n for n in dec.notes)
