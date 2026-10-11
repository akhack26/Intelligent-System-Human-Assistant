"""Offscreen GUI smoke test: the window, the Agent Center and STOP
must construct, update and close without exceptions."""
import pytest


def test_window_and_agent_center(mani, monkeypatch):
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    mani._init_colors(); mani._init_fonts()
    monkeypatch.setattr(mani.ISHAWindow, "_warm_up_model", lambda self: None)
    monkeypatch.setattr(mani.ISHAWindow, "_start_file_index", lambda self: None)
    win = mani.ISHAWindow()
    win.show()
    win.toggle_agent_panel(True)
    panel = win._agent_panel
    assert panel is not None and panel.isVisible()
    mani.ACTION_LOG.record("tool", tool="get_time", success=True, result="ok", risk="safe",
                           approval="auto", duration_ms=1)
    app.processEvents()
    panel.refresh()
    assert panel.activity.count() >= 1
    # The main-bar Stop button was removed on request; STOP stays reachable
    # through Esc, the Agent Center and _stop_generation().
    assert not hasattr(win, "stop_btn")
    win._set_busy(True)
    assert win._is_busy()
    win._stop_generation()
    assert not win._is_busy()
    panel.mode_cb.setCurrentIndex(2)                   # "Coding"
    assert win.cfg["model_mode"] == "coding"
    panel.mode_cb.setCurrentIndex(0)
    win.toggle_agent_panel(False)
    win.close()
    app.processEvents()
