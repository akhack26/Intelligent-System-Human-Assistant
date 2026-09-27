"""ISHA setup GUI: installer wizard, first-run wizard and uninstaller.

All long work (system probe, pip, llama.cpp, downloads, diagnostics) runs in
worker threads; the UI only receives events, so it never freezes.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QStackedWidget,
    QFrame, QCheckBox, QRadioButton, QButtonGroup, QLineEdit, QFileDialog, QTextEdit, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QScrollArea, QComboBox, QListWidget,
    QListWidgetItem, QPlainTextEdit, QSizePolicy,
)

from installer import PKG_DIR, load_installer_config
from installer import config_manager as cm
from installer import hardware_detector as hwd
from installer import model_manager as mm
from installer import python_manager as pym
from installer import runtime_manager as rt
from installer import shortcut_manager as sc
from installer import uninstaller as un
from installer.download_manager import human_bytes
from installer.installer import (Installer, InstallOptions, StepFailed, STEPS, STEP_LABELS,
                                 default_install_dir)
from isha_core import theme
from isha_core.paths import known_folder
from isha_core.file_index import DEFAULT_EXCLUDES

CFG = load_installer_config()


# ------------------------------------------------------------------ workers
class Worker(QThread):
    event = pyqtSignal(object)
    failed = pyqtSignal(object)        # StepFailed -> UI decides
    done = pyqtSignal(object)
    crashed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self._answer = None
        self._answered = threading.Event()

    def ask(self, failure) -> str:
        self._answered.clear()
        self.failed.emit(failure)
        self._answered.wait()
        return self._answer

    def answer(self, choice: str):
        self._answer = choice
        self._answered.set()

    def run(self):
        try:
            self.done.emit(self.fn(self))
        except Exception as e:  # noqa: BLE001
            self.crashed.emit(f"{type(e).__name__}: {e}")


def h1(text):
    lb = QLabel(text); lb.setObjectName("h1"); return lb


def dim(text, wrap=True):
    lb = QLabel(text); lb.setObjectName("dim"); lb.setWordWrap(wrap); return lb


def card():
    f = QFrame(); f.setObjectName("card"); return f


def badge(status: str) -> QLabel:
    color = theme.STATUS_COLORS.get(status, theme.TEXT_DIM)
    lb = QLabel(status)
    lb.setAlignment(Qt.AlignCenter)
    lb.setFixedWidth(96)
    lb.setStyleSheet(f"color:{color}; border:1px solid {color}; border-radius:9px; padding:2px 6px;"
                     f"font-weight:600; font-size:8.5pt;")
    return lb


class Page(QWidget):
    title = ""
    changed = pyqtSignal()

    def __init__(self, wiz):
        super().__init__()
        self.wiz = wiz
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(28, 22, 28, 12)
        self.lay.setSpacing(12)

    def can_next(self) -> bool:
        return True

    def enter(self):
        pass

    def leave(self) -> bool:
        return True


# ------------------------------------------------------------------ pages
class WelcomePage(Page):
    title = "Welcome"

    def __init__(self, wiz):
        super().__init__(wiz)
        self.lay.addStretch(1)
        t = QLabel("ISHA"); t.setObjectName("title"); t.setAlignment(Qt.AlignCenter)
        self.lay.addWidget(t)
        s = QLabel("Your Local AI Assistant"); s.setObjectName("h2"); s.setAlignment(Qt.AlignCenter)
        self.lay.addWidget(s)
        v = dim(f"Version {CFG['app_version']}"); v.setAlignment(Qt.AlignCenter)
        self.lay.addWidget(v)
        d = dim("ISHA runs AI models on your own computer and can open apps, find files, write and run "
                "code, install software and more - asking you first before anything risky. This setup "
                "installs Python packages, the llama.cpp AI runtime and a model that fits your PC.")
        d.setAlignment(Qt.AlignCenter)
        self.lay.addWidget(d)
        self.existing = dim(""); self.existing.setAlignment(Qt.AlignCenter)
        self.lay.addWidget(self.existing)
        self.lay.addStretch(2)

    def enter(self):
        st = cm.read_json(Path(self.wiz.opts.install_dir) / cm.STATE_FILE)
        if st.get("version"):
            self.existing.setText(f"ISHA {st['version']} is already installed in {self.wiz.opts.install_dir}. "
                                  f"Continuing will upgrade/repair it; your models, settings and memory are kept.")


def reflow(text: str) -> str:
    """Join hard-wrapped continuation lines (indented) into paragraphs."""
    out = []
    for line in text.splitlines():
        if line.startswith("   ") and out and out[-1].strip():
            out[-1] = out[-1].rstrip() + " " + line.strip()
        else:
            out.append(line)
    return "\n\n".join(p for p in out if p.strip())


class TextPage(Page):
    def __init__(self, wiz, title, file, agree_text=None):
        super().__init__(wiz)
        self.title = title
        self.lay.addWidget(h1(title))
        box = QPlainTextEdit(); box.setReadOnly(True)
        text = (PKG_DIR / file).read_text(encoding="utf-8")
        if agree_text:
            box.setPlainText(reflow(text))          # prose: proper paragraphs
        else:
            box.setPlainText(text)                  # aligned table: keep columns
            box.setFont(QFont("Consolas" if platform.system() == "Windows" else "Monospace", 9))
            box.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.lay.addWidget(box, 1)
        self.agree = None
        if agree_text:
            self.agree = QCheckBox(agree_text.replace("&", "&&"))   # '&' is a Qt mnemonic
            self.agree.toggled.connect(lambda _: self.changed.emit())
            self.lay.addWidget(self.agree)

    def can_next(self):
        return self.agree is None or self.agree.isChecked()

    def leave(self):
        if self.agree is not None:
            self.wiz.opts.accept_terms = self.agree.isChecked()
        return self.can_next()


class SystemPage(Page):
    title = "System check"

    def __init__(self, wiz):
        super().__init__(wiz)
        self.lay.addWidget(h1("System compatibility"))
        self.info = dim("Checking your computer...")
        self.lay.addWidget(self.info)
        self.grid_host = card(); self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(16, 12, 16, 12); self.grid.setVerticalSpacing(8)
        self.lay.addWidget(self.grid_host)
        row = QHBoxLayout()
        self.recheck = QPushButton("Check again"); self.recheck.clicked.connect(self.enter)
        self.pybtn = QPushButton("Install Python"); self.pybtn.hide(); self.pybtn.clicked.connect(self.install_python)
        row.addWidget(self.recheck); row.addWidget(self.pybtn); row.addStretch()
        self.lay.addLayout(row)
        self.lay.addStretch()
        self.report = None
        self._w = None

    def enter(self):
        if self._w and self._w.isRunning():
            return
        self.info.setText("Checking your computer...")
        self.recheck.setEnabled(False)
        target = Path(self.wiz.opts.install_dir)
        self._w = Worker(lambda w: (hwd.run_system_check(target, pym.find_best()),))
        self._w.done.connect(self._show)
        self._w.crashed.connect(lambda m: self.info.setText(f"System check failed: {m}"))
        self._w.start()

    def _show(self, res):
        rep = res[0]
        self.report = rep
        self.wiz.system_report = rep
        self.wiz.opts.internet = rep.internet
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, c in enumerate(rep.checks):
            name = QLabel(c.name); name.setObjectName("h2")
            det = dim(c.detail)
            self.grid.addWidget(badge(c.status), i, 0)
            self.grid.addWidget(name, i, 1)
            self.grid.addWidget(det, i, 2)
        self.grid.setColumnStretch(2, 1)
        py = rep.get("Python")
        self.pybtn.setVisible(py is not None and py.status == "MISSING")
        blocking = rep.blocking
        self.info.setText("Cannot continue: " + "; ".join(c.detail for c in blocking) if blocking else
                          "Your computer can run ISHA." + ("" if not py or py.status != "MISSING" else
                                                           " Python must be installed first."))
        self.recheck.setEnabled(True)
        self.changed.emit()

    def install_python(self):
        plan = pym.install_plan()
        if plan["method"] == "manual":
            QMessageBox.information(self, "Install Python", plan["describe"] + "\n\n" + plan.get("command", ""))
            return
        if QMessageBox.question(self, "Install Python", plan["describe"] + "\n\nContinue?") != QMessageBox.Yes:
            return
        self.info.setText("Installing Python - this can take a few minutes...")
        self.pybtn.setEnabled(False)
        self._w = Worker(lambda w: pym.install_python(plan))
        self._w.done.connect(lambda r: (QMessageBox.information(self, "Python", r["message"]),
                                        self.pybtn.setEnabled(True), self.enter()))
        self._w.start()

    def can_next(self):
        if not self.report or self.report.blocking:
            return False
        py = self.report.get("Python")
        return py is not None and py.status != "MISSING"


class LocationPage(Page):
    title = "Location"

    def __init__(self, wiz):
        super().__init__(wiz)
        self.lay.addWidget(h1("Install location"))
        self.lay.addWidget(dim("ISHA keeps its settings and models next to the program, so it installs "
                               "into a folder you own (no administrator rights needed)."))
        row = QHBoxLayout()
        self.path = QLineEdit(str(wiz.opts.install_dir))
        self.path.textChanged.connect(self._changed)
        b = QPushButton("Browse..."); b.clicked.connect(self.browse)
        row.addWidget(self.path, 1); row.addWidget(b)
        self.lay.addLayout(row)
        self.status = dim("")
        self.lay.addWidget(self.status)
        self.desktop = QCheckBox("Create a Desktop shortcut"); self.desktop.setChecked(True)
        self.lay.addWidget(self.desktop)
        self.lay.addStretch()
        self._changed()

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Install ISHA to", str(Path(self.path.text()).parent))
        if d:
            p = Path(d)
            self.path.setText(str(p if p.name.lower() == "isha" else p / "ISHA"))

    def _changed(self):
        p = Path(self.path.text().strip() or ".")
        ok, where = hwd.writable(p)
        free = shutil.disk_usage(hwd._existing_parent(p)).free / 1024 ** 3 if ok else 0
        warn = ""
        if "program files" in str(p).lower():
            warn = " Program Files needs administrator rights at every start - a per-user folder is recommended."
        self.status.setText((f"✓ Writable, {free:.1f} GB free." if ok else f"✗ Cannot write here: {where}") + warn)
        self._ok = ok
        self.changed.emit()

    def can_next(self):
        return getattr(self, "_ok", False)

    def leave(self):
        self.wiz.opts.install_dir = Path(self.path.text().strip()).expanduser()
        self.wiz.opts.desktop_shortcut = self.desktop.isChecked()
        return True


class Choices:
    """Feature / permission / search choices shared by several pages."""

    def __init__(self, existing: dict | None = None):
        ex = existing or {}
        feats = ex.get("features") or {}
        self.features = {k: feats.get(k, v.get("default", False)) for k, v in CFG["features"].items()}
        if "tts_enabled" in ex:
            self.features["voice"] = bool(ex["tts_enabled"])
        rev = {v: k for k, v in cm.AUTONOMY.items()}
        self.autonomy = rev.get(ex.get("autonomy_level"), "advanced")
        self.index_roots = ex.get("file_index_roots") or [str(known_folder(k)) for k in
                                                          ("desktop", "documents", "downloads", "pictures", "videos")]
        self.index_excludes = ex.get("file_index_excludes") or list(DEFAULT_EXCLUDES)


def voice_section(ch: Choices, on_change):
    box = card(); lay = QVBoxLayout(box)
    lay.addWidget(h1("Voice"))
    for key, text, note in (("voice", "Voice output (text-to-speech)",
                             "Neural voice uses Microsoft's online edge-tts; offline system voices are the fallback."),
                            ("stt", "Speech recognition (microphone)",
                             "Uses Google's speech service when you talk to ISHA (online).")):
        cb = QCheckBox(text); cb.setChecked(ch.features[key])
        cb.toggled.connect(lambda v, k=key: (ch.features.__setitem__(k, v), on_change()))
        lay.addWidget(cb); lay.addWidget(dim("   " + note))
    return box


def permissions_section(ch: Choices, on_change):
    box = card(); lay = QVBoxLayout(box)
    lay.addWidget(h1("How much control should ISHA have?"))
    grp = QButtonGroup(box)
    opts = (("ask", "Ask before actions", "ISHA only reads and searches on its own. Opening apps, changing "
             "files, messages and anything else asks you first."),
            ("balanced", "Balanced", "Same safeguards as 'Ask', reserved for finer policies; every change "
             "still needs your OK."),
            ("advanced", "Advanced (recommended)", "Safe actions like opening apps or setting volume run "
             "directly. Deleting, installing, sending messages and running code still ask."))
    for key, text, note in opts:
        rb = QRadioButton(text); rb.setChecked(ch.autonomy == key); grp.addButton(rb)
        rb.toggled.connect(lambda v, k=key: v and (setattr(ch, "autonomy", k), on_change()))
        lay.addWidget(rb); lay.addWidget(dim("   " + note))
    lay.addWidget(dim("Critical actions (terminal commands, running code, shutdown) ALWAYS ask, at every level."))
    box._grp = grp
    return box


def search_section(ch: Choices, on_change, parent):
    box = card(); lay = QVBoxLayout(box)
    lay.addWidget(h1("Fast file search"))
    en = QCheckBox("Enable fast file indexing (a local database of file names, sizes and dates)")
    en.setChecked(ch.features["file_index"])
    lay.addWidget(en)
    lst = QListWidget(); lst.setFixedHeight(120)

    def refresh():
        lst.clear()
        for r in ch.index_roots:
            QListWidgetItem(r, lst)
    refresh()
    lay.addWidget(dim("Folders to index (only these are scanned; file contents are never read):"))
    lay.addWidget(lst)
    row = QHBoxLayout()
    add = QPushButton("Add folder..."); rem = QPushButton("Remove selected")
    row.addWidget(add); row.addWidget(rem); row.addStretch()
    lay.addLayout(row)
    lay.addWidget(dim("Excluded folder names / paths (one per line):"))
    exc = QPlainTextEdit("\n".join(ch.index_excludes)); exc.setFixedHeight(80)
    lay.addWidget(exc)

    def toggle(v):
        ch.features["file_index"] = v
        for w in (lst, add, rem, exc):
            w.setEnabled(v)
        on_change()
    en.toggled.connect(toggle)
    add.clicked.connect(lambda: (lambda d: d and (ch.index_roots.append(d), refresh()))(
        QFileDialog.getExistingDirectory(parent, "Folder to index", str(Path.home()))))
    rem.clicked.connect(lambda: [ch.index_roots.remove(i.text()) for i in lst.selectedItems()] and refresh())
    exc.textChanged.connect(lambda: setattr(ch, "index_excludes",
                                            [x.strip() for x in exc.toPlainText().splitlines() if x.strip()]))
    toggle(ch.features["file_index"])
    return box


def tools_section(ch: Choices, on_change):
    box = card(); lay = QVBoxLayout(box)
    lay.addWidget(h1("Optional features"))
    for key in ("vision", "whatsapp", "advanced_pc", "coding"):
        cb = QCheckBox(CFG["features"][key]["label"]); cb.setChecked(ch.features[key])
        cb.toggled.connect(lambda v, k=key: (ch.features.__setitem__(k, v), on_change()))
        lay.addWidget(cb)
    lay.addWidget(dim("A feature you switch off is really off: its packages are not installed and ISHA "
                      "reports its tools as unavailable instead of pretending."))
    return box


class SectionPage(Page):
    def __init__(self, wiz, title, builders):
        super().__init__(wiz)
        self.title = title
        area = QScrollArea(); area.setWidgetResizable(True); area.setFrameShape(QFrame.NoFrame)
        host = QWidget(); v = QVBoxLayout(host); v.setSpacing(12)
        for b in builders:
            v.addWidget(b())
        v.addStretch()
        area.setWidget(host)
        self.lay.addWidget(area)


class ModelsPage(Page):
    title = "AI model"

    def __init__(self, wiz, first_run=False):
        super().__init__(wiz)
        self.first_run = first_run
        self.manifest = mm.load_manifest()
        self.lay.addWidget(h1("Choose ISHA's brain"))
        self.hwinfo = dim("")
        self.lay.addWidget(self.hwinfo)
        row = QHBoxLayout()
        self.group = QButtonGroup(self)
        presets = [("minimal", "Minimal"), ("recommended", "Recommended"), ("advanced", "Advanced"),
                   ("custom", "Custom"), ("none", "Keep current" if first_run else "Skip for now")]
        self.radios = {}
        for key, label in presets:
            rb = QRadioButton(label); self.group.addButton(rb); self.radios[key] = rb
            rb.toggled.connect(lambda v, k=key: v and self.set_preset(k))
            row.addWidget(rb)
        row.addStretch()
        tier_row = QHBoxLayout()
        tier_row.addWidget(dim("Model size:", wrap=False))
        self.tier = QComboBox()
        for t, lbl in (("small", "Small / Fast"), ("balanced", "Balanced"), ("advanced", "Advanced")):
            self.tier.addItem(lbl, t)
        self.tier.currentIndexChanged.connect(lambda _: self.refresh())
        tier_row.addWidget(self.tier); tier_row.addStretch()
        self.lay.addLayout(row)
        self.lay.addLayout(tier_row)
        self.desc = dim("")
        self.lay.addWidget(self.desc)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Model", "Used as", "Download", "RAM needed", "Recommended for"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.lay.addWidget(self.table, 1)
        self.custom_list = QListWidget(); self.custom_list.hide()
        for e in mm.entries(self.manifest):
            it = QListWidgetItem(f"{e.name}  -  {', '.join(e.roles)}  -  ~{e.size_gb:.1f} GB, {e.ram_gb:.0f} GB RAM")
            it.setData(Qt.UserRole, e.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.custom_list.addItem(it)
        self.custom_list.itemChanged.connect(lambda _: self.refresh())
        self.lay.addWidget(self.custom_list, 1)
        crow = QHBoxLayout()
        self.gguf_btn = QPushButton("Use an existing GGUF file...")
        self.gguf_btn.clicked.connect(self.pick_gguf)
        self.gguf_label = dim("", wrap=False)
        crow.addWidget(self.gguf_btn); crow.addWidget(self.gguf_label, 1)
        self.lay.addLayout(crow)
        self.storage = QLabel(""); self.storage.setWordWrap(True)
        self.lay.addWidget(self.storage)
        self._enough = True

    def enter(self):
        rep = getattr(self.wiz, "system_report", None)
        info = rep.hardware if rep else hwd.hw.probe()
        self.info = info
        tier, why = mm.recommend_tier(info.ram_total_gb, info.total_vram_gb)
        gpu = ", ".join(g.name for g in info.gpus) or "no GPU"
        ram = f"{info.ram_total_gb:.0f} GB" if info.ram_total_gb else "unknown"
        disk = f"{info.disk_free_gb:.0f} GB free" if info.disk_free_gb else ""
        self.hwinfo.setText(f"RAM: {ram}   GPU: {gpu}"
                            f"{f' ({info.total_vram_gb:.0f} GB VRAM)' if info.total_vram_gb else ''}   {disk}\n"
                            f"Recommendation: {tier.capitalize()} - {why} You can override this.")
        self.tier.setCurrentIndex(["small", "balanced", "advanced"].index(self.wiz.opts.tier or tier))
        key = self.wiz.opts.preset if self.wiz.opts.preset in self.radios else "recommended"
        if self.first_run and not self.wiz.opts.tier:
            key = "none"
        self.radios[key].setChecked(True)
        self.refresh()

    def set_preset(self, key):
        self.wiz.opts.preset = key
        custom = key == "custom"
        self.custom_list.setVisible(custom)
        self.table.setVisible(not custom)
        self.tier.setEnabled(key in ("minimal", "recommended", "advanced"))
        self.refresh()

    def pick_gguf(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose a GGUF model", str(Path.home()), "GGUF models (*.gguf)")
        if not f:
            return
        if not mm.is_gguf(Path(f)):
            QMessageBox.warning(self, "Not a GGUF file", "That file does not have a GGUF header.")
            return
        self.wiz.opts.custom_models["general"] = f
        self.gguf_label.setText(f"general → {f} (used in place, not copied)")
        self.refresh()

    def current_plan(self):
        o = self.wiz.opts
        o.tier = self.tier.currentData()
        if o.preset == "custom":
            o.model_ids = [self.custom_list.item(i).data(Qt.UserRole) for i in range(self.custom_list.count())
                           if self.custom_list.item(i).checkState() == Qt.Checked]
        inst = Installer.__new__(Installer)
        inst.o, inst.manifest, inst.report = o, self.manifest, {"hardware": getattr(self, "info", None)}
        return Installer.model_plan(inst)

    def refresh(self):
        if not hasattr(self, "info"):
            return
        plan = self.current_plan()
        o = self.wiz.opts
        pinfo = self.manifest["presets"].get(o.preset, {})
        self.desc.setText(pinfo.get("description", {"custom": "Pick any models from the catalogue.",
                                                    "none": "No model is downloaded now. You can add one later "
                                                            "by running the first-run wizard or copying a .gguf "
                                                            "into the models folder."}.get(o.preset, "")))
        self.table.setRowCount(0)
        for e in mm.unique_entries(plan):
            roles = [r for r, x in plan.items() if x is e]
            r = self.table.rowCount(); self.table.insertRow(r)
            vals = [e.name, ", ".join(roles), f"~{e.size_gb:.1f} GB", f"{e.ram_gb:.0f} GB",
                    {"small": "4-8 GB RAM", "balanced": "8-16 GB RAM", "advanced": "16 GB+ RAM / 6 GB+ VRAM"}[e.tier]]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
        runtime_ok = rt.venv_python(o.install_dir).exists()
        need = mm.storage_needed(plan, Path(o.install_dir) / "models", runtime_ok or self.first_run,
                                 CFG["runtime_space_gb"])
        warn = ""
        if getattr(self, "info", None) and self.info.ram_total_gb:
            too_big = [e.name for e in mm.unique_entries(plan) if e.ram_gb > self.info.ram_total_gb * 0.8]
            if too_big:
                warn = f"\n⚠ {', '.join(too_big)} needs more RAM than this PC comfortably has."
        self._enough = need["enough"]
        color = theme.OK if need["enough"] else theme.BAD
        self.storage.setText(f"<span style='color:{color}'>{'✓ Enough storage' if need['enough'] else '✗ Not enough disk space'}"
                             f"</span> — required {human_bytes(need['total'])} (models {human_bytes(need['models'])}, "
                             f"runtime {human_bytes(need['runtime'])}, temporary {human_bytes(need['temp'])}); "
                             f"available {human_bytes(need['available'])}.{warn}")
        self.changed.emit()

    def can_next(self):
        if self.wiz.opts.preset == "custom" and not self.wiz.opts.model_ids and not self.wiz.opts.custom_models:
            return False
        return self._enough

    def leave(self):
        self.current_plan()
        return True


class InstallPage(Page):
    title = "Install"

    def __init__(self, wiz, steps=None):
        super().__init__(wiz)
        self.steps = steps or STEPS
        self.lay.addWidget(h1("Installing ISHA"))
        self.overall = QProgressBar(); self.overall.setRange(0, len(self.steps))
        self.lay.addWidget(self.overall)
        self.current = QLabel("Ready."); self.current.setObjectName("h2")
        self.lay.addWidget(self.current)
        body = QHBoxLayout()
        self.steplist = QListWidget(); self.steplist.setFixedWidth(250)
        for s in self.steps:
            QListWidgetItem(f"○  {STEP_LABELS[s]}", self.steplist)
        body.addWidget(self.steplist)
        right = QVBoxLayout()
        self.dl = card(); dl = QGridLayout(self.dl)
        self.dl_name = QLabel(""); self.dl_bar = QProgressBar(); self.dl_info = dim("")
        self.pause = QPushButton("Pause"); self.cancel = QPushButton("Cancel")
        self.pause.clicked.connect(self.toggle_pause); self.cancel.clicked.connect(self.cancel_dl)
        dl.addWidget(self.dl_name, 0, 0, 1, 3); dl.addWidget(self.dl_bar, 1, 0, 1, 3)
        dl.addWidget(self.dl_info, 2, 0); dl.addWidget(self.pause, 2, 1); dl.addWidget(self.cancel, 2, 2)
        self.dl.hide()
        right.addWidget(self.dl)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True)
        right.addWidget(self.log, 1)
        body.addLayout(right, 1)
        self.lay.addLayout(body, 1)
        self.worker = None
        self.finished_ok = False
        self.installer = None

    def enter(self):
        if self.worker and self.worker.isRunning():
            return
        if self.finished_ok:
            return
        self.wiz.set_nav_enabled(False)
        self.log.appendPlainText(f"Installing to {self.wiz.opts.install_dir}")
        self.installer = Installer(self.wiz.opts, on_event=lambda e: self.worker.event.emit(e))
        rep = getattr(self.wiz, "system_report", None)
        if rep:
            self.installer.report.update(hardware=rep.hardware, gpu_backend=rep.gpu_backend,
                                         gpu_reason=rep.gpu_reason)
        inst = self.installer
        steps = self.steps
        self.worker = Worker(lambda w: inst.run(steps=steps, on_failure=w.ask))
        self.worker.event.connect(self.on_event)
        self.worker.failed.connect(self.on_failed)
        self.worker.done.connect(self.on_done)
        self.worker.crashed.connect(self.on_crash)
        self.worker.start()

    def _mark(self, step, sym):
        i = self.steps.index(step)
        self.steplist.item(i).setText(f"{sym}  {STEP_LABELS[step]}")

    def on_event(self, e):
        t = e.get("type")
        if t == "step":
            if e["status"] == "start":
                self._mark(e["step"], "→"); self.current.setText(e["label"] + "...")
            elif e["status"] == "done":
                self._mark(e["step"], "✓"); self.overall.setValue(self.steps.index(e["step"]) + 1)
                if e["step"] == "models":
                    self.dl.hide()
            elif e["status"] == "failed":
                self._mark(e["step"], "✗")
            elif e["status"] == "skipped":
                self._mark(e["step"], "–"); self.overall.setValue(self.steps.index(e["step"]) + 1)
        elif t == "package":
            sym = {"installing": "→", "installed": "✓", "already": "✓", "failed": "✗"}.get(e["status"], "·")
            if e["status"] != "installing":
                self.log.appendPlainText(f"{sym} {e['package']}" + (f" - {e['detail']}" if e.get("detail") else ""))
            self.current.setText(f"Python packages ({e['index']}/{e['total']}): {e['package']}")
        elif t == "download":
            self.dl.show()
            self.dl_name.setText(e["file"])
            if e["total"]:
                self.dl_bar.setRange(0, 1000); self.dl_bar.setValue(int(1000 * e["done"] / e["total"]))
            eta = f" · ETA {int(e['eta']) // 60}m {int(e['eta']) % 60}s" if e.get("eta") else ""
            self.dl_info.setText(f"{human_bytes(e['done'])} / {human_bytes(e['total'])} · "
                                 f"{human_bytes(e['speed'])}/s{eta} · {e['state']}")
        elif t == "llama":
            if e.get("total"):
                self.current.setText(f"llama.cpp: {human_bytes(e['done'])} / {human_bytes(e['total'])}")
            elif e.get("message"):
                self.log.appendPlainText(e["message"])
        elif t in ("log", "model", "progress"):
            if e.get("message"):
                self.log.appendPlainText(e["message"] if t != "progress" else f"  {e['message']}")

    def toggle_pause(self):
        d = self.installer.active_downloader if self.installer else None
        if not d:
            return
        if d.state == "paused":
            d.resume(); self.pause.setText("Pause")
        else:
            d.pause(); self.pause.setText("Resume")

    def cancel_dl(self):
        d = self.installer.active_downloader if self.installer else None
        if d and QMessageBox.question(self, "Cancel download",
                                      "Cancel this download? The partial file is kept so it can resume later.") \
                == QMessageBox.Yes:
            d.cancel()

    def on_failed(self, f: StepFailed):
        names = {"retry": "Retry", "skip": "Skip", "exit": "Exit", "change_model": "Change model",
                 "choose_location": "Choose another location"}
        box = QMessageBox(self)
        box.setWindowTitle("ISHA setup")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"{STEP_LABELS[f.step]} failed.")
        box.setInformativeText(f"Reason:\n{f.message}")
        buttons = {box.addButton(names[o], QMessageBox.ActionRole): o for o in f.options}
        box.exec_()
        choice = buttons.get(box.clickedButton(), "exit")
        if choice in ("change_model", "choose_location"):
            self.worker.answer("exit")
            target = "AI model" if choice == "change_model" else "Location"
            QTimer.singleShot(200, lambda: self.wiz.goto_title(target))
            return
        self.worker.answer(choice)

    def on_done(self, _):
        self.finished_ok = True
        self.current.setText("Installation finished.")
        for w in self.installer.warnings:
            self.log.appendPlainText("⚠ " + w)
        self.wiz.install_report = self.installer.report
        self.wiz.install_warnings = self.installer.warnings
        self.wiz.set_nav_enabled(True)
        self.changed.emit()

    def on_crash(self, msg):
        self.wiz.set_nav_enabled(True)
        if "Terms" in msg:
            QMessageBox.warning(self, "ISHA setup", msg)
            self.wiz.goto_title("Terms")
            return
        self.current.setText("Installation stopped.")
        self.log.appendPlainText(f"Stopped: {msg}\nRun setup again to resume from this step.")
        self.changed.emit()

    def can_next(self):
        return self.finished_ok


class DiagnosticsPage(Page):
    title = "Diagnostics"

    def __init__(self, wiz):
        super().__init__(wiz)
        self.lay.addWidget(h1("Checking ISHA"))
        self.host = card(); self.grid = QGridLayout(self.host)
        self.lay.addWidget(self.host)
        self.summary = QLabel(""); self.summary.setObjectName("h2")
        self.lay.addWidget(self.summary)
        self.deep = QPushButton("Test model loading (can take a minute)")
        self.deep.clicked.connect(self.run_deep)
        self.lay.addWidget(self.deep, 0, Qt.AlignLeft)
        self.lay.addStretch()

    def show_results(self, results):
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, r in enumerate(results):
            st = "WARNING" if r["status"] == "WARNING" else r["status"]
            self.grid.addWidget(badge(st), i, 0)
            n = QLabel(r["name"]); n.setObjectName("h2")
            self.grid.addWidget(n, i, 1)
            self.grid.addWidget(dim(r["detail"]), i, 2)
        self.grid.setColumnStretch(2, 1)
        from installer.diagnostics import ready
        self.summary.setText("ISHA is ready." if ready(results) else
                             "ISHA is installed, but something essential is missing (see above).")

    def enter(self):
        self.show_results((getattr(self.wiz, "install_report", {}) or {}).get("diagnostics", []))

    def run_deep(self):
        from installer import diagnostics
        self.deep.setEnabled(False); self.summary.setText("Loading the model...")
        d = Path(self.wiz.opts.install_dir)
        self._w = Worker(lambda w: diagnostics.run(d, str(rt.venv_python(d)), deep=True))
        self._w.done.connect(lambda r: (self.show_results(r), self.deep.setEnabled(True)))
        self._w.start()


class FinishPage(Page):
    title = "Finish"

    def __init__(self, wiz, first_run=False):
        super().__init__(wiz)
        self.lay.addStretch(1)
        t = QLabel("ISHA is ready"); t.setObjectName("title"); t.setAlignment(Qt.AlignCenter)
        self.lay.addWidget(t)
        self.msg = dim(""); self.msg.setAlignment(Qt.AlignCenter)
        self.lay.addWidget(self.msg)
        self.launch = QCheckBox("Launch ISHA now"); self.launch.setChecked(not first_run)
        self.lay.addWidget(self.launch, 0, Qt.AlignCenter)
        self.lay.addStretch(2)

    def enter(self):
        w = getattr(self.wiz, "install_warnings", []) or []
        self.msg.setText(f"Installed in {self.wiz.opts.install_dir}.\nStart ISHA any time from the "
                         f"{'Start Menu' if platform.system() == 'Windows' else 'application menu'}"
                         f"{' or Desktop' if self.wiz.opts.desktop_shortcut else ''}."
                         + ("\n\nNotes:\n" + "\n".join(f"• {x}" for x in w) if w else ""))


# ------------------------------------------------------------------ wizard shell
class Wizard(QWidget):
    def __init__(self, pages_factory, title="ISHA Setup", on_finish=None):
        super().__init__()
        self.setWindowTitle(title)
        self.setStyleSheet(theme.QSS)
        self.resize(1000, 680)
        self.opts = InstallOptions()
        self.system_report = None
        self.on_finish = on_finish
        root = QHBoxLayout(self); root.setContentsMargins(14, 14, 14, 14); root.setSpacing(14)
        side = QFrame(); side.setObjectName("sidebar"); side.setFixedWidth(210)
        self.side = QVBoxLayout(side); self.side.setContentsMargins(12, 18, 12, 18)
        logo = QLabel("ISHA"); logo.setObjectName("title"); logo.setStyleSheet("font-size:18pt;")
        self.side.addWidget(logo)
        self.side.addWidget(dim(f"Setup · v{CFG['app_version']}"))
        self.side.addSpacing(10)
        root.addWidget(side)
        main = QVBoxLayout()
        self.stack = QStackedWidget()
        main.addWidget(self.stack, 1)
        nav = QHBoxLayout()
        self.back = QPushButton("Back"); self.next = QPushButton("Next"); self.next.setObjectName("primary")
        self.quit = QPushButton("Cancel")
        self.back.clicked.connect(lambda: self.go(-1)); self.next.clicked.connect(lambda: self.go(1))
        self.quit.clicked.connect(self.close)
        nav.addWidget(self.quit); nav.addStretch(); nav.addWidget(self.back); nav.addWidget(self.next)
        main.addLayout(nav)
        root.addLayout(main, 1)
        self.pages = pages_factory(self)
        self.step_labels = []
        for p in self.pages:
            self.stack.addWidget(p)
            lb = QLabel(p.title); lb.setObjectName("stepOff")
            self.side.addWidget(lb); self.step_labels.append(lb)
            p.changed.connect(self.update_nav)
        self.side.addStretch()
        self._nav_enabled = True
        self.idx = 0
        self.show_page(0)

    def show_page(self, i):
        self.idx = i
        self.stack.setCurrentIndex(i)
        for j, lb in enumerate(self.step_labels):
            lb.setObjectName("stepOn" if j == i else "stepDone" if j < i else "stepOff")
            lb.style().unpolish(lb); lb.style().polish(lb)
        self.pages[i].enter()
        self.update_nav()

    def goto_title(self, title):
        for i, p in enumerate(self.pages):
            if p.title == title:
                for later in self.pages[i:]:
                    if isinstance(later, InstallPage):
                        later.finished_ok = False
                self.show_page(i)
                return

    def go(self, d):
        page = self.pages[self.idx]
        if d > 0:
            if not page.can_next() or not page.leave():
                return
            if self.idx == len(self.pages) - 1:
                self.finish()
                return
        self.show_page(max(0, min(len(self.pages) - 1, self.idx + d)))

    def set_nav_enabled(self, on):
        self._nav_enabled = on
        self.update_nav()

    def update_nav(self):
        last = self.idx == len(self.pages) - 1
        self.next.setText("Finish" if last else "Get Started" if self.idx == 0 else "Next")
        self.next.setEnabled(self._nav_enabled and self.pages[self.idx].can_next())
        self.back.setEnabled(self._nav_enabled and self.idx > 0 and
                             not isinstance(self.pages[self.idx], (DiagnosticsPage, FinishPage)))
        self.quit.setEnabled(self._nav_enabled)

    def finish(self):
        if self.on_finish:
            self.on_finish(self)
        fin = self.pages[-1]
        if isinstance(fin, FinishPage) and fin.launch.isChecked():
            launch_isha(Path(self.opts.install_dir))
        self.close()


def launch_isha(install_dir: Path):
    cmd = sc.launch_command(install_dir)
    kw = {"cwd": str(install_dir)}
    if platform.system() == "Windows":
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | 0x200
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kw)
    except Exception as e:  # noqa: BLE001
        QMessageBox.warning(None, "ISHA", f"Could not start ISHA: {e}")


def setup_pages(wiz):
    ch = Choices()
    wiz.choices = ch

    def sync():
        wiz.opts.features = dict(ch.features)
        wiz.opts.autonomy = ch.autonomy
        wiz.opts.index_roots = list(ch.index_roots)
        wiz.opts.index_excludes = list(ch.index_excludes)
    sync()
    on = sync
    return [WelcomePage(wiz),
            TextPage(wiz, "Terms", "TERMS.txt", "I agree to the ISHA Terms & Conditions"),
            TextPage(wiz, "Privacy", "PRIVACY.txt"),
            SystemPage(wiz), LocationPage(wiz),
            SectionPage(wiz, "Features", [lambda: tools_section(ch, on), lambda: voice_section(ch, on),
                                          lambda: permissions_section(ch, on),
                                          lambda: search_section(ch, on, wiz)]),
            ModelsPage(wiz), InstallPage(wiz), DiagnosticsPage(wiz), FinishPage(wiz)]


def run_setup(install_dir=None, offline_dir=None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    wiz = Wizard(setup_pages)
    if install_dir:
        wiz.opts.install_dir = Path(install_dir)
    if offline_dir:
        wiz.opts.offline_dir = Path(offline_dir)
    wiz.show()
    return app.exec_()


# ------------------------------------------------------------------ first-run wizard
def first_run_pages(wiz):
    d = Path(wiz.opts.install_dir)
    existing = cm.read_json(d / cm.CONFIG_FILE)
    ch = Choices(existing)
    wiz.choices = ch
    wiz.opts.accept_terms = True
    wiz.opts.preset = "none"

    def sync():
        wiz.opts.features = dict(ch.features)
        wiz.opts.autonomy = ch.autonomy
        wiz.opts.index_roots, wiz.opts.index_excludes = list(ch.index_roots), list(ch.index_excludes)
    sync()
    on = sync
    intro = WelcomePage(wiz)
    intro.title = "Welcome"
    return [intro, ModelsPage(wiz, first_run=True),
            SectionPage(wiz, "Voice", [lambda: voice_section(ch, on)]),
            SectionPage(wiz, "Permissions", [lambda: permissions_section(ch, on)]),
            SectionPage(wiz, "Search", [lambda: search_section(ch, on, wiz)]),
            SectionPage(wiz, "Tools", [lambda: tools_section(ch, on)]),
            InstallPage(wiz, steps=["models", "configure"]), FinishPage(wiz, first_run=True)]


def run_first_run(install_dir: Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    def factory(w):
        w.opts.install_dir = Path(install_dir)
        w.opts.python_exe = sys.executable
        return first_run_pages(w)

    def done(w):
        st = cm.State(Path(install_dir))
        st.data["first_run_complete"] = True
        st.data.setdefault("version", CFG["app_version"])
        st.save()
    wiz = Wizard(factory, "Welcome to ISHA", on_finish=done)
    wiz.pages[0].existing.setText("Let's configure your local AI assistant: model, voice, permissions, "
                                  "search and tools.")
    wiz.show()
    app.exec_()
    return 0


# ------------------------------------------------------------------ uninstall dialog
class UninstallDialog(QWidget):
    def __init__(self, install_dir: Path):
        super().__init__()
        self.dir = Path(install_dir)
        self.setWindowTitle("Uninstall ISHA")
        self.setStyleSheet(theme.QSS)
        self.resize(560, 470)
        lay = QVBoxLayout(self); lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(h1("Uninstall ISHA"))
        lay.addWidget(dim(f"From: {self.dir}"))
        self.boxes = {}
        labels = {"app": "Remove application", "runtime": "Remove runtime (Python packages, llama.cpp)",
                  "models": "Remove downloaded models", "cache": "Remove cache (file index, logs, backups)",
                  "config": "Remove configuration", "memory": "Remove memory and chat history"}
        for k, text in labels.items():
            cb = QCheckBox(text); cb.setChecked(un.DEFAULTS[k])
            if k in ("app", "runtime"):
                cb.setEnabled(False)
            lay.addWidget(cb); self.boxes[k] = cb
        lay.addWidget(dim("Your models, settings and memory are kept unless you tick them."))
        self.result = dim("")
        lay.addWidget(self.result)
        lay.addStretch()
        row = QHBoxLayout()
        c = QPushButton("Cancel"); c.clicked.connect(self.close)
        self.go = QPushButton("Uninstall"); self.go.setObjectName("danger"); self.go.clicked.connect(self.run)
        row.addStretch(); row.addWidget(c); row.addWidget(self.go)
        lay.addLayout(row)

    def run(self):
        choices = {k: cb.isChecked() for k, cb in self.boxes.items()}
        if QMessageBox.question(self, "Uninstall ISHA", "Remove ISHA now?") != QMessageBox.Yes:
            return
        try:
            res = un.uninstall(self.dir, choices)
        except Exception as e:  # noqa: BLE001
            self.result.setText(f"Uninstall failed: {e}")
            return
        self.result.setText(f"Removed {len(res.removed)} item(s)." +
                            (f" Kept: {len(res.kept)} (see ISHA_USER_DATA_KEPT.txt)." if res.kept else "") +
                            (f" Errors: {'; '.join(res.errors)}" if res.errors else ""))
        self.go.setEnabled(False)


def run_uninstall_gui(install_dir: Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    d = UninstallDialog(install_dir)
    d.show()
    return app.exec_()
