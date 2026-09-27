"""Shared ISHA visual identity (Qt stylesheet strings, no PyQt import).

Used by the installer / first-run wizard and the Agent Center so the setup
and the app look like one product: dark background, subtle neon accents,
rounded panels, clean typography.
"""
BG = "#070a14"
PANEL = "#0d1424"
PANEL_2 = "#111b30"
BORDER = "#16324a"
NEON = "#00F0FF"
NEON_DIM = "#0aa5b8"
PINK = "#ff2d6f"
TEXT = "#c8e6ff"
TEXT_DIM = "#6f93b3"
OK = "#35e39b"
WARN = "#ffc24b"
BAD = "#ff5c7a"

STATUS_COLORS = {"READY": OK, "WARNING": WARN, "DEGRADED": WARN, "MISSING": BAD, "UNAVAILABLE": BAD}

QSS = f"""
* {{ font-family: 'Segoe UI', 'Ubuntu', 'Noto Sans', sans-serif; }}
QWidget {{ background: {BG}; color: {TEXT}; font-size: 10pt; }}
QLabel#title {{ color: {NEON}; font-size: 22pt; font-weight: 600; letter-spacing: 2px; }}
QLabel#h1 {{ color: {NEON}; font-size: 15pt; font-weight: 600; }}
QLabel#h2 {{ color: {TEXT}; font-size: 11pt; font-weight: 600; }}
QLabel#dim {{ color: {TEXT_DIM}; }}
QLabel#stepOn {{ color: {NEON}; font-weight: 600; padding: 6px 10px; border-left: 3px solid {NEON}; }}
QLabel#stepDone {{ color: {TEXT_DIM}; padding: 6px 10px; border-left: 3px solid {BORDER}; }}
QLabel#stepOff {{ color: #3f5a73; padding: 6px 10px; border-left: 3px solid transparent; }}
QFrame#card, QFrame#sidebar {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#cardSel {{ background: {PANEL_2}; border: 1px solid {NEON_DIM}; border-radius: 12px; }}
QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 18px; }}
QPushButton:hover {{ border-color: {NEON_DIM}; }}
QPushButton:disabled {{ color: #3f5a73; border-color: #13243a; }}
QPushButton#primary {{ background: {NEON_DIM}; color: {BG}; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: {NEON}; }}
QPushButton#primary:disabled {{ background: #123744; color: #3f6070; }}
QPushButton#danger {{ background: #3a0d1b; border: 1px solid {PINK}; color: #ffd0dc; }}
QLineEdit, QTextEdit, QPlainTextEdit, QListWidget, QTableWidget, QComboBox {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px;
    selection-background-color: {NEON_DIM}; selection-color: {BG}; }}
QHeaderView::section {{ background: {PANEL_2}; color: {TEXT_DIM}; border: none; padding: 6px; }}
QProgressBar {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 7px; height: 14px;
    text-align: center; color: {TEXT}; }}
QProgressBar::chunk {{ border-radius: 6px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #007c99, stop:1 {NEON}); }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""
