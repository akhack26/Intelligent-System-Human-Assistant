import pytest

from isha_core import permissions as P


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_read_only_always_auto(level):
    assert P.decide("check_memory_usage", "safe", {"autonomy_level": level}).action == P.ALLOW


def test_safe_change_depends_on_autonomy():
    assert P.decide("open_app", "safe", {"autonomy_level": "high"}).action == P.ALLOW
    assert P.decide("open_app", "safe", {"autonomy_level": "medium"}).action == P.ASK
    assert P.decide("open_app", "safe", {"autonomy_level": "low"}).action == P.ASK


def test_confirm_asks_by_default():
    assert P.decide("delete_file_safely", "confirm", {}).action == P.ASK
    assert P.decide("empty_recycle_bin", "confirm", {"autonomy_level": "high"}).action == P.ASK


def test_critical_always_asks_even_when_trusted():
    cfg = {"autonomy_level": "high", "confirm_risky_tools": False, "trusted_tools": ["run_terminal_command"]}
    assert P.decide("run_terminal_command", "critical", cfg).action == P.ASK


def test_trusted_messaging():
    assert P.decide("send_whatsapp_message", "confirm", {}).action == P.ASK
    assert P.decide("send_whatsapp_message", "confirm", {"trusted_messaging": True}).action == P.ALLOW


def test_blocked_and_unknown():
    assert P.decide("open_app", "safe", {"blocked_tools": ["open_app"]}).action == P.DENY
    assert P.decide("brand_new_tool", "weird", {}).risk == "confirm"


def test_legacy_confirm_switch():
    assert P.decide("write_file_content", "confirm", {"confirm_risky_tools": False}).action == P.ALLOW
