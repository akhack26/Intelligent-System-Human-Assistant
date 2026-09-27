class _AuditBridge(QObject):
    """ActionLog listeners fire on worker threads; this hops them onto the
    UI thread through a queued Qt signal."""
    entry = pyqtSignal(object)


class AgentPanelWindow(QWidget):
    """ISHA Agent Center: model/mode/autonomy controls, live status,
    STOP, file-index controls and the activity log."""

    closed = pyqtSignal()
    _MODES = [("Auto", "auto"), ("General", "general"), ("Coding", "coding"), ("Reasoning", "reasoning"),
              ("Study", "study"), ("Fast", "fast"), ("Vision", "vision")]
    _AUTONOMY = [("Low - ask before most actions", "low"), ("Medium - read-only automatic", "medium"),
                 ("High - only risky actions ask", "high")]

    def __init__(self, main_window, parent=None):
        super().__init__(parent, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.main_window = main_window
        self.setWindowTitle("ISHA — Agent Center")
        self.resize(640, 620)
        self.setStyleSheet("""
            QWidget { background: #070a14; color: #c8e6ff; font-family: 'Segoe UI'; font-size: 10pt; }
            QLabel#h { color: #00F0FF; font-size: 13pt; font-weight: 600; }
            QLabel#k { color: #5f8fb0; }
            QComboBox, QListWidget, QTextEdit { background: #0d1424; border: 1px solid #12405a;
                border-radius: 6px; padding: 4px; }
            QPushButton { background: #0f2a3d; border: 1px solid #1b6a8a; border-radius: 6px; padding: 6px 12px; }
            QPushButton:hover { background: #154060; }
            QPushButton#stop { background: #5a0d1e; border-color: #ff2d6f; color: #ffd0dc; font-weight: 700; }
            QPushButton#stop:hover { background: #7a1028; }
        """)
        self._bridge = _AuditBridge()
        self._bridge.entry.connect(self._on_entry)
        self._listener = lambda e: self._bridge.entry.emit(e)
        ACTION_LOG.subscribe(self._listener)
        self._build()
        for e in ACTION_LOG.recent(80):
            self._on_entry(e)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1500)
        self.refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        top = QHBoxLayout()
        h = QLabel("ISHA Agent Center"); h.setObjectName("h")
        top.addWidget(h); top.addStretch()
        self.stop_btn = QPushButton("■  STOP"); self.stop_btn.setObjectName("stop")
        self.stop_btn.setToolTip("Stop the agent loop, generation, queued tools and running code (Esc)")
        self.stop_btn.clicked.connect(self._stop)
        top.addWidget(self.stop_btn)
        root.addLayout(top)

        grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(5)
        self.mode_cb = QComboBox()
        for label, val in self._MODES:
            self.mode_cb.addItem(label, val)
        self.auto_cb = QComboBox()
        for label, val in self._AUTONOMY:
            self.auto_cb.addItem(label, val)
        cfg = self.main_window.cfg
        self.mode_cb.setCurrentIndex(max(0, [v for _, v in self._MODES].index(cfg.get("model_mode", "auto"))
                                         if cfg.get("model_mode", "auto") in [v for _, v in self._MODES] else 0))
        au = isha_perm.normalise_autonomy(cfg.get("autonomy_level"))
        self.auto_cb.setCurrentIndex([v for _, v in self._AUTONOMY].index(au))
        self.mode_cb.currentIndexChanged.connect(self._mode_changed)
        self.auto_cb.currentIndexChanged.connect(self._autonomy_changed)
        self.labels = {}
        rows = [("Model mode", self.mode_cb), ("Autonomy", self.auto_cb)]
        for key in ("Backend", "Active model", "Loaded", "Current task", "Current tool", "Agent step",
                    "CPU / RAM", "GPU", "Voice / Mic", "Tools", "File index"):
            lbl = QLabel("-"); lbl.setWordWrap(True)
            self.labels[key] = lbl
            rows.append((key, lbl))
        for i, (k, w) in enumerate(rows):
            kl = QLabel(k); kl.setObjectName("k")
            grid.addWidget(kl, i, 0, Qt.AlignTop)
            grid.addWidget(w, i, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        btns = QHBoxLayout()
        for text, fn in (("Rebuild index", self._rebuild), ("Pause/Resume index", self._pause),
                         ("Self-check", self._selfcheck), ("Models", self._models)):
            b = QPushButton(text); b.clicked.connect(fn); btns.addWidget(b)
        root.addLayout(btns)

        al = QLabel("Activity"); al.setObjectName("k")
        root.addWidget(al)
        self.activity = QListWidget()
        root.addWidget(self.activity, 1)
        self.detail = QTextEdit(); self.detail.setReadOnly(True); self.detail.setFixedHeight(110)
        root.addWidget(self.detail)

    # ---- controls ----
    def _stop(self):
        self.main_window._stop_generation()

    def _mode_changed(self, _i):
        val = self.mode_cb.currentData()
        self.main_window.cfg["model_mode"] = val
        save_config(self.main_window.cfg)
        ACTION_LOG.record("model_mode", mode=val, source="gui")

    def _autonomy_changed(self, _i):
        val = self.auto_cb.currentData()
        self.main_window.cfg["autonomy_level"] = val
        save_config(self.main_window.cfg)
        ACTION_LOG.record("autonomy", level=val, source="gui")

    def _bg(self, fn):
        def run():
            try:
                out = fn()
            except Exception as e:  # noqa: BLE001
                out = f"ERROR: {e}"
            self._bridge.entry.emit({"kind": "_detail", "text": str(out)})
        threading.Thread(target=run, daemon=True).start()

    def _rebuild(self):
        self._bg(lambda: _rebuild_file_index({}))

    def _pause(self):
        idx = get_file_index()
        if idx is None:
            self.detail.setPlainText("File index disabled.")
            return
        self._bg(lambda: _pause_file_index({"pause": not idx.status()["paused"]}))

    def _selfcheck(self):
        self._bg(lambda: format_self_diagnostics(run_self_diagnostics(self.main_window.cfg)))

    def _models(self):
        self._bg(lambda: _get_model_status({}))

    # ---- updates ----
    def _on_entry(self, e: dict):
        if e.get("kind") == "_detail":
            self.detail.setPlainText(e.get("text", ""))
            return
        if e.get("kind") == "status":
            return
        t = str(e.get("time", ""))[-8:]
        k = e.get("kind")
        if k == "tool":
            mark = "✔" if e.get("success") else "✖"
            text = (f"{t}  {mark} {e.get('tool')}  [{e.get('risk')}/{e.get('approval')}]  "
                    f"{e.get('duration_ms', 0)} ms  {str(e.get('result', ''))[:70]}")
        elif k == "route":
            text = f"{t}  ⇢ route {e.get('task')} → {e.get('model')}"
        elif k == "approval":
            text = f"{t}  ? approval {e.get('tool')}: {e.get('approval')}"
        elif k == "request":
            text = f"{t}  ▶ {str(e.get('text', ''))[:80]}"
        else:
            text = f"{t}  {k}: " + ", ".join(f"{a}={b}" for a, b in e.items()
                                             if a not in ("time", "ts", "kind"))[:90]
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, e)
        self.activity.addItem(item)
        while self.activity.count() > 300:
            self.activity.takeItem(0)
        self.activity.scrollToBottom()
        try:
            self.activity.itemClicked.disconnect()
        except Exception:
            pass
        self.activity.itemClicked.connect(
            lambda it: self.detail.setPlainText(json.dumps(it.data(Qt.UserRole), ensure_ascii=False,
                                                           indent=2, default=str)))

    def refresh(self):
        cur = ACTION_LOG.current
        s = getattr(self.main_window, "_latest_stats", {}) or {}
        L = self.labels
        L["Backend"].setText(f"llama.cpp ({_LLAMA_MODE})" + ("" if _LLAMA_CPP_OK else " — UNAVAILABLE"))
        L["Active model"].setText(f"{ACTIVE_MODEL.get('name') or '-'}  ({ACTIVE_MODEL.get('role') or '-'})"
                                  + (f" — {ACTIVE_MODEL['notes']}" if ACTIVE_MODEL.get("notes") else ""))
        L["Loaded"].setText(", ".join(GGUF.loaded_names()) or "none")
        L["Current task"].setText(cur.get("task") or "-")
        L["Current tool"].setText(cur.get("tool") or "-")
        L["Agent step"].setText(cur.get("step") or "-")
        cpu, ram = s.get("cpu_percent"), s.get("ram_percent")
        L["CPU / RAM"].setText(f"{cpu:.0f}% / {ram:.0f}%" if cpu is not None and ram is not None else "-")
        hw = _HW_CACHE.get("info")
        L["GPU"].setText(", ".join(g.name for g in hw.gpus) if hw and hw.gpus else "none detected")
        L["Voice / Mic"].setText(f"TTS {'on' if _TTS_OK else 'off'} / mic {'on' if _STT_OK else 'off'}")
        L["Tools"].setText(f"{len(TOOL_REGISTRY)} registered")
        idx = get_file_index(create=False)
        if idx is None:
            L["File index"].setText("not started" if self.main_window.cfg.get("file_index_enabled", True) else "disabled")
        else:
            st = idx.status()
            L["File index"].setText(f"{st['state']}{' (paused)' if st['paused'] else ''}, {st['entries']} entries, "
                                    f"last: {st['last_complete'] or 'never'}")
        self.stop_btn.setEnabled(bool(self.main_window._is_busy()))

    def closeEvent(self, event):
        self.closed.emit()
        event.ignore()
        self.hide()

    def shutdown(self):
        ACTION_LOG.unsubscribe(self._listener)
        self._timer.stop()


