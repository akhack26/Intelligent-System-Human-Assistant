"""Shared fixtures. The whole suite runs headless:

* HOME points at a throw-away folder with Desktop/Downloads/... so file tools
  never touch the real user profile,
* ISHA_SKIP_BACKEND=1 stops mani.py from pip-installing/downloading llama.cpp,
* QT_QPA_PLATFORM=offscreen lets PyQt load without a display,
* ISHA_DATA_DIR keeps the index / audit log / memory out of the project.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSION = Path(tempfile.mkdtemp(prefix="isha_test_"))
HOME = SESSION / "home"
for d in ("Desktop", "Downloads", "Documents", "Pictures", "Music", "Videos"):
    (HOME / d).mkdir(parents=True, exist_ok=True)
(SESSION / "data").mkdir(exist_ok=True)

os.environ["HOME"] = str(HOME)
os.environ["USERPROFILE"] = str(HOME)
os.environ["XDG_CONFIG_HOME"] = str(HOME / ".config")     # no user-dirs.dirs -> default names
os.environ["XDG_DATA_HOME"] = str(HOME / ".local" / "share")
os.environ["ISHA_SKIP_BACKEND"] = "1"
os.environ["ISHA_DATA_DIR"] = str(SESSION / "data")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def home():
    return HOME


@pytest.fixture(scope="session")
def mani():
    """Import the real application module once (heavy: ~4 s)."""
    import mani as m
    data = SESSION / "data"
    m.CONFIG_FILE = str(data / "isha_config.json")
    m.HISTORY_FILE = str(data / "isha_chat_history.json")
    m._WA_CONTACTS_FILE = data / "whatsapp_contacts.json"
    return m


@pytest.fixture(autouse=True)
def _reset_stop():
    from isha_core.control import STOP
    STOP.reset()
    yield
    STOP.reset()
