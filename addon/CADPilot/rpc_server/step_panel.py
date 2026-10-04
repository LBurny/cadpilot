"""The CADPilot steps panel.

A QDockWidget owned by FreeCAD's main window: it lists the journal's steps as
a status-dotted list, shows the selected step's parameters in an inline JSON
editor, and drives the whole review loop — release (next/all/to-here), accept,
reject, roll back, replay.

It talks to ``step_engine`` directly (same process, GUI thread) rather than
through the RPC server, so the panel keeps working with the RPC server
stopped — which is the normal state while the user studies a model by hand.

The look is MATLAB-inspired: flat surfaces, hairline separators, section
captions, one restrained accent. FreeCAD themes are application-level
stylesheets — the QPalette stays light even in dark mode — so decorative
colors come from two hand-tuned palettes (dark/light), chosen by sampling a
rendered pixel of the main window. Actions are text-only: the platform's
standard icons (floppy, trash, media arrows) clash with FreeCAD's chrome.
"""

from __future__ import annotations

import html
import json
import time

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtGui, QtWidgets

from rpc_server import step_engine
from rpc_server import step_journal as sj

PANEL_NAME = "CADPilotStepPanel"
PANEL_TITLE = "CADPilot Steps"
REFRESH_MS = 1000

_STATE_LABEL = {sj.STATE_PLANNED: "planned", sj.STATE_DONE: "done", sj.STATE_FAILED: "failed"}
_GREY = QtGui.QColor(150, 150, 150)
_RED = QtGui.QColor(200, 40, 40)
_STATE_COLORS = {
    sj.STATE_PLANNED: _GREY,
    sj.STATE_DONE: QtGui.QColor(58, 157, 78),
    sj.STATE_FAILED: _RED,
}
_ICON_CACHE: dict = {}

NO_DOCUMENT = "No document open."

# Two hand-tuned decorative palettes. Everything that must read on the theme's
# surface (captions, hairlines, error/status text, accents) is a solid color;
# everything painted OVER it (hover, zebra, selection) is an alpha tint so it
# composites onto whatever background the FreeCAD theme provides.
_THEME_DARK = {
    "dim": "#9aa3ab",
    "faint": "#6d757d",
    "hair": "#4b5258",
    "hover": "rgba(255, 255, 255, 0.06)",
    "press": "rgba(255, 255, 255, 0.12)",
    "track": "rgba(255, 255, 255, 0.08)",
    "alt": "rgba(255, 255, 255, 0.035)",
    "sel": "rgba(92, 148, 184, 0.45)",
    "seltext": "#f2f5f7",
    "red": "#e06c60",
    # The primary-button accent is one neutral light gray in BOTH palettes:
    # it must read on the theme's surface whichever mode FreeCAD is in.
    "accent": "#9aa3ab",
    "accentbg": "rgba(154, 163, 171, 0.16)",
    "accenthover": "rgba(154, 163, 171, 0.26)",
}
_THEME_LIGHT = {
    "dim": "#66707a",
    "faint": "#9aa1a8",
    "hair": "#d3d7db",
    "hover": "rgba(0, 0, 0, 0.05)",
    "press": "rgba(0, 0, 0, 0.10)",
    "track": "rgba(0, 0, 0, 0.08)",
    "alt": "rgba(0, 0, 0, 0.025)",
    "sel": "rgba(70, 130, 170, 0.30)",
    "seltext": "#1c2126",
    "red": "#c94f45",
    "accent": "#9aa3ab",
    "accentbg": "rgba(154, 163, 171, 0.16)",
    "accenthover": "rgba(154, 163, 171, 0.26)",
}


def _dot(
    color: QtGui.QColor, hollow: bool = False, ring: bool = False, size: int = 13
) -> QtGui.QIcon:
    """A round status marker, painted at runtime so it follows no icon theme."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pm)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    if ring:
        # Bullseye: an accepted (reviewed) done step.
        painter.setPen(QtGui.QPen(color, 1.4))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawEllipse(QtCore.QRectF(1.2, 1.2, size - 2.4, size - 2.4))
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QtCore.QRectF(3.8, 3.8, size - 7.6, size - 7.6))
    elif hollow:
        painter.setPen(QtGui.QPen(color, 1.5))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawEllipse(QtCore.QRectF(1.6, 1.6, size - 3.2, size - 3.2))
    else:
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QtCore.QRectF(1.8, 1.8, size - 3.6, size - 3.6))
    painter.end()
    return QtGui.QIcon(pm)


def _state_icon(rec) -> QtGui.QIcon:
    key = (rec.state, rec.accepted)
    if key not in _ICON_CACHE:
        if rec.accepted and rec.state == sj.STATE_DONE:
            # Bullseye in the same done green: reviewed, still part of the run.
            _ICON_CACHE[key] = _dot(_STATE_COLORS[sj.STATE_DONE], ring=True)
        else:
            color = _STATE_COLORS.get(rec.state, _GREY)
            _ICON_CACHE[key] = _dot(color, hollow=rec.state == sj.STATE_PLANNED)
    return _ICON_CACHE[key]


def _editor_text(rec) -> str:
    """What the detail editor shows.

    For an execute_code step that is the snippet itself — the panel used to show
    a raw params dict, which for those steps was an opaque ``{}``. Editing the
    snippet and hitting Re-run is how a user iterates on code the model wrote.
    Every other op shows its params JSON.
    """
    if rec.operation == "execute_code":
        return str((rec.params or {}).get("code") or "")
    return json.dumps(rec.params, ensure_ascii=False, indent=2) or "{}"


def _editor_placeholder(rec) -> str:
    if rec.operation == "execute_code":
        # Old journals recorded execute_code before the code was kept.
        return "# No code recorded for this step (it predates v0.5.2)."
    return "{}"


def _op_label(spec: dict) -> str:
    """ "rollback_to step 4" — a short human label for a journal op spec."""
    label = str(spec.get("operation") or "?").replace("_", " ")
    index = spec.get("index")
    if isinstance(index, int) and spec.get("operation") != "replay":
        label += f" step {index}"
    return label


def _row_text(rec) -> str:
    """Delegate: the display rules live in ``step_journal`` (pure, tested)."""
    return sj.row_text(rec)


def _tooltip_text(rec) -> str:
    return sj.tooltip_text(rec)


class StepPanel(QtWidgets.QDockWidget):
    def __init__(self, parent=None):
        super().__init__(PANEL_TITLE, parent)
        self.setObjectName(PANEL_NAME)
        self._digest: str | None = None
        self._note: str | None = None
        self._records: list = []
        self._details_for: int = 0
        self._in_apply: bool = False
        self._drifted: bool = False
        self._entries: list[dict] = []
        self._spotlight: dict | None = None
        self._meta_raw: str = ""
        self._derive_colors()
        self._splitter_timer = QtCore.QTimer(self)
        self._splitter_timer.setSingleShot(True)
        self._splitter_timer.setInterval(600)
        self._splitter_timer.timeout.connect(self._save_splitter)
        self._columns_timer = QtCore.QTimer(self)
        self._columns_timer.setSingleShot(True)
        self._columns_timer.setInterval(600)
        self._columns_timer.timeout.connect(self._save_step_width)
        self._build()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # --- construction ----------------------------------------------------

    def _derive_colors(self) -> None:
        """Pick the dark or light decorative palette.

        FreeCAD themes live in an application stylesheet and leave the
        QPalette light, so the only truthful source is a rendered pixel of the
        main window's chrome. Themes apply at startup, when this panel is
        built, so detecting once here is enough.
        """
        self._colors = dict(_THEME_DARK if self._detect_dark() else _THEME_LIGHT)

    @staticmethod
    def _detect_dark() -> bool:
        try:
            mw = FreeCADGui.getMainWindow()
            if mw is None:
                return False
            img = mw.grab(QtCore.QRect(4, 4, 16, 16)).toImage()
            if img.isNull():
                return False
            lum, n = 0.0, 0
            for x in range(0, img.width(), 4):
                for y in range(0, img.height(), 4):
                    c = img.pixelColor(x, y)
                    lum += 0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()
                    n += 1
            return n > 0 and (lum / n) < 128
        except Exception:
            return False

    def _action(self, text, tip, slot) -> QtGui.QAction:
        # Text-only: the platform's standard icons (floppy, trash, media
        # arrows) are colorful glyphs that clash with FreeCAD's chrome.
        action = QtGui.QAction(text, self)
        action.setToolTip(tip)
        action.triggered.connect(slot)
        return action

    def _build(self) -> None:
        box = QtWidgets.QWidget(self)
        box.setObjectName("StepPanelRoot")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.header = QtWidgets.QLabel("", box)
        self.header.setObjectName("PanelHeader")
        self.header.setWordWrap(True)
        self.header.setTextFormat(QtCore.Qt.RichText)
        layout.addWidget(self.header)

        self.progress = QtWidgets.QProgressBar(box)
        self.progress.setObjectName("PlanProgress")
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        self.toolbar = QtWidgets.QToolBar(box)
        self.toolbar.setObjectName("StepToolBar")
        self.toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.act_next = self._action("Next", "Run the next planned step", self._run_next)
        self.act_all = self._action(
            "Run all",
            "Run until a step fails or the plan ends",
            self._run_all,
        )
        self.act_rollback = self._action(
            "Roll back",
            "Undo back to just before the selected step",
            self._rollback,
        )
        self.act_accept = self._action(
            "Accept",
            "Mark the selected done step as reviewed (soft-lock; unaccept if it already is)",
            self._accept,
        )
        self.act_reject = self._action(
            "Reject",
            "Undo the selected step and forget everything from it onward",
            self._reject,
        )
        self.act_replay = self._action(
            "Replay",
            "Roll back to the start and re-run the whole journal",
            self._replay,
        )
        self.act_snapshot = self._action(
            "Snapshot",
            "Accept the current state (incl. manual edits outside the journal) "
            "as the reviewed baseline",
            self._snapshot,
        )
        self.act_copy = self._action(
            "Copy params",
            "Copy the selected step's parameter JSON to the clipboard",
            self._copy_params,
        )
        for action in (
            self.act_next,
            self.act_all,
            self.act_rollback,
            self.act_accept,
            self.act_reject,
            self.act_replay,
            self.act_snapshot,
        ):
            self.toolbar.addAction(action)
        layout.addWidget(self.toolbar)

        self.tree = QtWidgets.QTreeWidget(box)
        self.tree.setObjectName("StepTree")
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["#", "Step", "Op"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemSelectionChanged.connect(self._on_selection)
        # The description follows the SELECTION (one click), so double-click
        # keeps its own affordance: jump into the parameter editor.
        self.tree.itemDoubleClicked.connect(self._focus_editor)
        header = self.tree.header()
        # The Step column carries long labels ("execute_code: +1 object(s)…")
        # and was Stretch: elided, with no way to widen it. Now everything is
        # user-resizable, Op takes the slack, and the Step width persists
        # (double-clicking a section separator still auto-fits, per Qt).
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Interactive)
        header.setStretchLastSection(True)
        header.resizeSection(1, self._load_step_width())
        header.sectionResized.connect(self._columns_changed)
        self.tree.setMinimumHeight(120)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical, box)
        splitter.setObjectName("StepSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.tree)
        splitter.addWidget(self._build_details(splitter))
        splitter.addWidget(self._build_log(splitter))
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes(self._load_splitter())
        splitter.splitterMoved.connect(lambda *_: self._splitter_timer.start())
        self._splitter = splitter
        layout.addWidget(splitter, stretch=1)

        box.setStyleSheet(self._stylesheet())
        self.setWidget(box)

    def _build_details(self, parent) -> QtWidgets.QWidget:
        details = QtWidgets.QWidget(parent)
        lay = QtWidgets.QVBoxLayout(details)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        card = QtWidgets.QFrame(details)
        card.setObjectName("DetailCard")
        card_lay = QtWidgets.QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        self.detail_meta = QtWidgets.QLabel("", card)
        self.detail_meta.setObjectName("DetailMeta")
        # One elided line, never a wrapped two-liner; the tooltip holds the
        # full text. Eliding is recomputed on resize (eventFilter below).
        #
        # Ignored horizontal policy is load-bearing: a non-wrapping QLabel
        # reports minimumSizeHint == the full text width, and the layout takes
        # that as a width FLOOR. Since every refresh wrote a differently
        # elided text, the dock's width tracked the text and visibly jittered
        # (and the floor exceeded the visible width, clipping the ellipsis).
        # Ignored makes the layout hand the label the available width instead.
        self.detail_meta.setWordWrap(False)
        self.detail_meta.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred
        )
        self.detail_meta.installEventFilter(self)
        card_lay.addWidget(self.detail_meta)
        card_lay.addWidget(self._hairline(card))

        self.editor = QtWidgets.QPlainTextEdit(card)
        self.editor.setObjectName("ParamEditor")
        self.editor.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.editor.setPlaceholderText("Step parameters (JSON)")
        # Code must not soft-wrap: a wrapped line is unreadable. The editor
        # scrolls horizontally instead (the log console keeps WidgetWidth).
        self.editor.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.editor.setMinimumHeight(80)
        card_lay.addWidget(self.editor, stretch=1)

        card_lay.addWidget(self._hairline(card))
        footer = QtWidgets.QWidget(card)
        edit_row = QtWidgets.QHBoxLayout(footer)
        edit_row.setContentsMargins(8, 6, 8, 6)
        edit_row.setSpacing(8)
        self.btn_save = QtWidgets.QPushButton("Save", footer)
        self.btn_save.setToolTip("Save the edited parameters to the planned/failed step")
        self.btn_save.clicked.connect(self._save)
        edit_row.addWidget(self.btn_save)
        self.btn_rerun = QtWidgets.QPushButton("Save && re-run", footer)
        self.btn_rerun.setProperty("role", "primary")
        self.btn_rerun.setToolTip(
            "Save the parameters and re-run the step (rolls back to just before it first)"
        )
        self.btn_rerun.clicked.connect(self._save_rerun)
        edit_row.addWidget(self.btn_rerun)
        edit_row.addStretch(1)
        card_lay.addWidget(footer)
        lay.addWidget(card, stretch=1)

        details.setMinimumHeight(180)
        return details

    def _build_log(self, parent) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget(parent)
        lay = QtWidgets.QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        card = QtWidgets.QFrame(box)
        card.setObjectName("LogCard")
        card_lay = QtWidgets.QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        self.status = QtWidgets.QLabel("", card)
        self.status.setObjectName("PanelStatus")
        self.status.setWordWrap(True)
        card_lay.addWidget(self.status)
        card_lay.addWidget(self._hairline(card))

        self.log = QtWidgets.QTextEdit(card)
        self.log.setObjectName("LogConsole")
        self.log.setReadOnly(True)
        self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.log.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
        self.log.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        card_lay.addWidget(self.log, stretch=1)
        lay.addWidget(card, stretch=1)
        box.setMinimumHeight(90)
        return box

    def _hairline(self, parent) -> QtWidgets.QFrame:
        line = QtWidgets.QFrame(parent)
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setProperty("role", "hairline")
        line.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        return line

    def _stylesheet(self) -> str:
        c = self._colors
        green = _STATE_COLORS[sj.STATE_DONE].name()
        # Backgrounds and base text are deliberately NOT set on the tree,
        # editor and buttons: the FreeCAD theme (an app-level stylesheet)
        # paints those, and this sheet only refines borders, spacing and the
        # overlay tints that make the panel read as one calm surface.
        return f"""
QLabel {{ background: transparent; }}
QLabel#PanelHeader {{ padding: 2px 2px 0 2px; }}
QFrame[role="hairline"] {{ border: none; background: {c["hair"]}; max-height: 1px; }}
QProgressBar#PlanProgress {{ border: none; border-radius: 3px; background: {c["track"]}; }}
QProgressBar#PlanProgress::chunk {{ border-radius: 3px; background: {green}; }}
QTreeWidget#StepTree {{
    border: 1px solid {c["hair"]}; border-radius: 4px; outline: none;
    alternate-background-color: {c["alt"]};
}}
QTreeWidget#StepTree::item {{ padding: 4px 6px; border: none; }}
QTreeWidget#StepTree::item:hover:!selected {{ background: {c["hover"]}; }}
QTreeWidget#StepTree::item:selected {{ background: {c["sel"]}; color: {c["seltext"]}; }}
QHeaderView::section {{
    background: transparent; border: none; border-bottom: 1px solid {c["hair"]};
    color: {c["dim"]}; font-weight: bold; padding: 2px 6px 5px 6px;
}}
QToolBar#StepToolBar {{ background: transparent; border: none; spacing: 2px; }}
QToolBar#StepToolBar QToolButton {{
    background: transparent; border: 1px solid transparent;
    border-radius: 4px; padding: 4px 8px;
}}
QToolBar#StepToolBar QToolButton:hover {{
    background: {c["hover"]}; border-color: {c["hair"]};
}}
QToolBar#StepToolBar QToolButton:pressed {{ background: {c["press"]}; }}
QToolBar#StepToolBar QToolButton:disabled {{ color: {c["faint"]}; }}
QPlainTextEdit#ParamEditor, QTextEdit#LogConsole {{ border: none; padding: 4px; }}
QFrame#DetailCard, QFrame#LogCard {{
    border: 1px solid {c["hair"]}; border-radius: 4px;
}}
QPushButton {{ border-radius: 4px; padding: 5px 14px; }}
QPushButton:hover {{ border-color: {c["dim"]}; }}
QPushButton:disabled {{ color: {c["faint"]}; }}
QPushButton[role="primary"] {{
    background: {c["accentbg"]}; border: 1px solid {c["accent"]};
}}
QPushButton[role="primary"]:hover {{ background: {c["accenthover"]}; }}
QLabel#DetailMeta {{ color: {c["dim"]}; padding: 5px 8px 4px 8px; }}
QLabel#PanelStatus {{ color: {c["dim"]}; padding: 5px 8px 4px 8px; }}
"""

    # --- refresh ---------------------------------------------------------

    def refresh(self) -> None:
        """Redraw when the journal changed; report drift on its own channel.

        The tree and the status line are pure functions of the journal text,
        so the digest gates them. Drift detection cannot share that gate — a
        manual Ctrl+Z changes the undo stack WITHOUT touching the journal —
        so it is checked on every tick and logged once per episode.
        """
        doc = FreeCAD.ActiveDocument
        if doc is None:
            if self._note != NO_DOCUMENT:
                self._digest = None
                self._note = NO_DOCUMENT
                self._records = []
                self._render([], "", NO_DOCUMENT)
            return
        try:
            text = getattr(doc, sj.JOURNAL_PROP, "") or ""
        except Exception:
            text = ""
        records = sj.from_json(text)
        self._report_drift(doc, records)
        note = self._summary(records)
        if text == self._digest and note == self._note:
            return
        old_states = {r.index: r.state for r in self._records}
        self._digest = text
        self._note = note
        self._records = records
        self._render(records, self._header_text(doc, records, sj.meta_from_json(text)), note)
        # Mirror failures caused OUTSIDE the panel (MCP step_control, another
        # client) into the log: the panel's own _apply already narrated its
        # failures, and the first render after a rebuild stays quiet.
        if old_states and not self._in_apply:
            for rec in records:
                if rec.state == sj.STATE_FAILED and old_states.get(rec.index) not in (
                    None,
                    sj.STATE_FAILED,
                ):
                    self._log(f"step {rec.index} ({rec.operation}) failed: {rec.error}", "error")
        # Manual-edit sync notices queued by step_engine's observer: the user
        # corrected a tracked object in the GUI and the journal followed.
        for ev in step_engine.pop_sync_events(doc.Name):
            self._log(
                f"step {ev['index']}: {ev['prop']} {ev['old']} -> {ev['new']} (manual edit)",
                "info",
            )

    def _header_text(self, doc, records, meta) -> str:
        name = html.escape(doc.Label or doc.Name)
        dim = self._colors["dim"]
        text = (
            f"<b>{name}</b>"
            f"<span style='color:{dim}'>&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"{sj.done_count(records)}/{len(records)} done</span>"
        )
        description = str(meta.get("description") or "").strip()
        if description:
            text += f"<br><span style='color:{dim}'>Plan: {html.escape(description)}</span>"
        return text

    def _summary(self, records) -> str:
        return (
            f"{len(records)} step(s), "
            f"{sj.done_count(records)} done, {sj.pending_count(records)} planned, "
            f"{sum(1 for r in records if r.accepted)} accepted"
        )

    def _report_drift(self, doc, records) -> None:
        """Log once when the journal and FreeCAD's undo stack start disagreeing.

        A manual Ctrl+Z/Ctrl+Y pops journal transactions without touching the
        journal itself, so from then on rollback's undo math is unreliable —
        it may rebuild instead of undoing cleanly. The notice is
        edge-triggered (clean -> drifted logs one line, drifted -> clean
        re-arms it): pinned in the status line it stayed on screen forever,
        because the stack never realigns on its own.
        """
        try:
            names = list(getattr(doc, "UndoNames", []) or [])
        except Exception:
            names = []
        # Same anchor as step_engine._status: the last ATOMIC step. A trailing
        # execute_code record has no transaction, so anchoring on the last
        # completed record outright would hide a manual undo here too.
        last = sj.last_atomic_done(records)
        drifted = bool(last and last.transaction not in names)
        if drifted and not self._drifted:
            self._log(
                "out of sync with FreeCAD's undo stack (undone manually?) — "
                "rollback may rebuild instead of undoing cleanly",
                "error",
            )
        self._drifted = drifted

    def _render(self, records, header: str, note: str) -> None:
        self.header.setText(header)
        self.header.setVisible(bool(header))
        total = len(records)
        self.progress.setVisible(total > 0)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(sj.done_count(records))

        selected = self._selected_index()
        self.tree.blockSignals(True)
        self.tree.clear()
        for rec in records:
            item = QtWidgets.QTreeWidgetItem([str(rec.index), _row_text(rec), rec.operation])
            item.setIcon(0, _state_icon(rec))
            item.setToolTip(1, _tooltip_text(rec))
            if rec.accepted:
                font = item.font(1)
                font.setBold(True)
                item.setFont(1, font)
            if rec.state == sj.STATE_PLANNED:
                for col in range(3):
                    item.setForeground(col, QtGui.QBrush(_GREY))
            elif rec.state == sj.STATE_FAILED:
                item.setForeground(1, QtGui.QBrush(_RED))
            self.tree.addTopLevelItem(item)
            if rec.index == selected:
                self.tree.setCurrentItem(item)
        self.tree.blockSignals(False)
        self.status.setText(note)
        self._update_details()
        # The rebuild above ran with signals blocked, so _on_selection never
        # fired — the spotlight must be refreshed here, or an edited snippet's
        # comment would not show until the selection moved.
        self._update_spotlight()
        self._update_actions()

    # --- selection / details ---------------------------------------------

    def _selected_index(self) -> int:
        items = self.tree.selectedItems()
        if not items:
            return 0
        try:
            return int(items[0].text(0))
        except (TypeError, ValueError):
            return 0

    def _selected_record(self):
        index = self._selected_index()
        return next((r for r in self._records if r.index == index), None)

    def _on_selection(self) -> None:
        self._update_details()
        self._update_spotlight()
        self._update_actions()

    def _focus_editor(self, *_) -> None:
        """Double-click: jump into the parameter editor (editing IS the point
        of double-clicking; the description already shows on selection)."""
        self.editor.setFocus()

    def _update_spotlight(self) -> None:
        """Show the SELECTED step's description as the log's spotlight.

        Runs on every selection change, so one click is enough — the same way
        the middle pane shows the step's code the moment it is selected. The
        spotlight is for the human, not the debug history: it renders as one
        bright line BELOW the log entries but never becomes one (the log is
        for debugging). No selection = no spotlight.
        """
        rec = self._selected_record()
        if rec is None:
            self._spotlight = None
        else:
            desc = sj.step_description(rec)
            if desc:
                text = f"step {rec.index} ({rec.operation}): {desc}"
            elif rec.operation == "execute_code":
                effect = rec.label.removeprefix("execute_code: ") or rec.operation
                text = (
                    f"step {rec.index} ({rec.operation}): {effect} "
                    "(no description — start the snippet with a # comment line)"
                )
            else:
                text = f"step {rec.index} ({rec.operation}): {rec.label or '?'}"
            self._spotlight = {"stamp": time.strftime("%H:%M:%S"), "text": text}
        self._render_log()

    def _update_details(self) -> None:
        rec = self._selected_record()
        if rec is None:
            self._details_for = 0
            self._set_meta("No step selected")
            if not self.editor.document().isModified():
                self.editor.setPlainText("")
            return
        parts = [f"Step {rec.index}", rec.operation, _STATE_LABEL.get(rec.state, rec.state)]
        if rec.accepted:
            parts.append("accepted")
        if rec.operation == "execute_code":
            # Whether the snippet touched the model is the difference between a
            # rollback-able step and a harmless inspection — worth naming.
            parts.append("mutating" if rec.mutated else "read-only")
        if rec.timestamp:
            parts.append(rec.timestamp)
        if rec.duration_ms:
            parts.append(f"{rec.duration_ms} ms")
        gained = len(rec.objects_after) - len(rec.objects_before)
        if rec.state == sj.STATE_DONE and gained:
            parts.append(f"{gained:+d} object(s)")
        self._set_meta(" · ".join(parts))
        # Do not clobber a half-edited value on every 1s refresh: only refill
        # when the selection moved or the editor is untouched.
        if self._details_for != rec.index or not self.editor.document().isModified():
            self.editor.setPlaceholderText(_editor_placeholder(rec))
            self.editor.setPlainText(_editor_text(rec))
            self.editor.document().setModified(False)
        self._details_for = rec.index

    def _set_meta(self, text: str) -> None:
        """The detail meta line: one line, elided to fit — never wrapped.

        The full text stays available as the tooltip; the elision is
        recomputed from the raw text on every set and on every resize (the
        event filter below), so widening the dock reveals more of it.
        """
        self._meta_raw = text
        self.detail_meta.setToolTip(text)
        # contentsRect, not width(): the stylesheet's 8px side padding is part
        # of the widget but not of the text area — eliding against width()
        # put the ellipsis 12px too far right, painted into the padding and
        # clipped away (the "no ellipsis, just cut off" bug).
        width = max(self.detail_meta.contentsRect().width(), 60)
        elided = self.detail_meta.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, width)
        self.detail_meta.setText(elided)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.detail_meta and event.type() == QtCore.QEvent.Resize:
            self._set_meta(self._meta_raw)
        return super().eventFilter(obj, event)

    def _update_actions(self) -> None:
        rec = self._selected_record()
        has_doc = FreeCAD.ActiveDocument is not None
        has_planned = any(r.state == sj.STATE_PLANNED for r in self._records)
        any_done = any(r.state == sj.STATE_DONE for r in self._records)
        self.act_next.setEnabled(has_doc and has_planned)
        self.act_all.setEnabled(has_doc and has_planned)
        self.act_replay.setEnabled(has_doc and any_done)
        self.act_snapshot.setEnabled(has_doc)
        done = rec is not None and rec.state == sj.STATE_DONE
        self.act_rollback.setEnabled(done)
        self.act_accept.setEnabled(done)
        self.act_accept.setText("Unaccept" if (rec is not None and rec.accepted) else "Accept")
        self.act_reject.setEnabled(rec is not None)
        self.btn_save.setEnabled(
            rec is not None and rec.state in (sj.STATE_PLANNED, sj.STATE_FAILED)
        )
        self.btn_rerun.setEnabled(
            rec is not None and rec.executable and rec.state in (sj.STATE_DONE, sj.STATE_FAILED)
        )

    # --- actions ---------------------------------------------------------

    def _apply(self, spec: dict, warn: bool = True):
        doc = FreeCAD.ActiveDocument
        if doc is None:
            self._warn(NO_DOCUMENT)
            return None
        label = _op_label(spec)
        # _in_apply silences the refresh-time failure mirror: this method
        # narrates its own outcome below, with the op label attached.
        self._in_apply = True
        try:
            res = step_engine.apply_op(doc, spec)
            self._digest = None
            self._note = None
            self.refresh()
        except Exception as e:
            self._warn(f"{label}: {type(e).__name__}: {e}")
            return None
        finally:
            self._in_apply = False
        if res.get("success"):
            self._log(label, "ok")
            # A rollback that rebuilt or only partially restored the model
            # "succeeded" — the panel user clicked the button and got their
            # answer, so without this the degradation notes stay invisible.
            for text in res.get("warnings") or []:
                self._log(text, "error")
        elif warn:
            self._warn(f"{label}: {res.get('error') or 'The operation failed.'}")
        return res

    def _apply_maybe_force(self, spec: dict):
        """Run a spec; when the engine refuses without force, offer to force."""
        res = self._apply(spec, warn=False)
        if res is None or res.get("success"):
            return
        error = str(res.get("error") or "The operation failed.")
        if "force=true" in error and self._confirm(f"{error}\n\nForce it?"):
            self._apply({**spec, "force": True})
        else:
            self._warn(f"{_op_label(spec)}: {error}")

    def _run_next(self, _checked=False):
        self._apply({"operation": "run_next"})

    def _run_all(self, _checked=False):
        self._apply({"operation": "run_all"})

    def _clear_plan(self):
        self._apply({"operation": "clear_plan"})

    def _rollback(self, _checked=False):
        index = self._selected_index()
        doc = FreeCAD.ActiveDocument
        if not index or doc is None:
            return
        plan = sj.plan_rollback(step_engine.read_journal(doc), index - 1)
        if plan["undo_count"] == 0:
            self._warn(f"Nothing has been executed before step {index}.")
            return
        text = (
            f"Roll back to just before step {index}? "
            f"This undoes {plan['undo_count']} transaction(s)."
        )
        if plan["accepted"]:
            text += f"\nStep(s) {plan['accepted']} are accepted (reviewed)."
        if not self._confirm(text):
            return
        self._apply_maybe_force({"operation": "rollback_to", "index": index - 1})

    def _accept(self, _checked=False):
        rec = self._selected_record()
        if rec is None:
            return
        self._apply({"operation": "accept", "index": rec.index, "params": {"on": not rec.accepted}})

    def _reject(self, _checked=False):
        rec = self._selected_record()
        doc = FreeCAD.ActiveDocument
        if rec is None or doc is None:
            return
        plan = sj.plan_reject(step_engine.read_journal(doc), rec.index)
        if plan is None:
            return
        text = (
            f"Reject step {rec.index} and everything after it? "
            f"{plan['undo_count']} transaction(s) will be undone and "
            f"{len(plan['drop'])} step(s) forgotten."
        )
        if plan["accepted"]:
            text += f"\nThat includes accepted step(s) {plan['accepted']}."
        force = False
        if plan["blocking"]:
            text += (
                f"\nStep(s) {sj.blocking_text(self._records, plan['blocking'])} "
                "carry no transaction, so undo may revert the wrong change."
            )
            force = True
        if not self._confirm(text):
            return
        self._apply({"operation": "reject", "index": rec.index, "force": force})

    def _replay(self, _checked=False):
        doc = FreeCAD.ActiveDocument
        if doc is None:
            return
        plan = sj.plan_rollback(step_engine.read_journal(doc), 0)
        if plan["undo_count"] == 0:
            self._warn("Nothing has been executed yet.")
            return
        if not self._confirm(
            f"Replay the journal from scratch? "
            f"This undoes {plan['undo_count']} transaction(s) and re-runs every step."
        ):
            return
        self._apply_maybe_force({"operation": "replay", "index": 0})

    def _snapshot(self, _checked=False):
        text, ok = QtWidgets.QInputDialog.getText(
            self,
            "Snapshot",
            "Note for the baseline (what was done outside the journal):",
        )
        if not ok:
            return
        res = self._apply({"operation": "snapshot", "params": {"note": text.strip()}})
        if res and res.get("success") and res.get("added"):
            self._log(f"new since the last step: {', '.join(res['added'])}", "info")

    def _copy_params(self, _checked=False):
        rec = self._selected_record()
        if rec is None:
            return
        QtGui.QGuiApplication.clipboard().setText(_editor_text(rec))

    def _edited_params(self) -> dict | None:
        rec = self._selected_record()
        # An execute_code step's detail IS its snippet: the editor holds raw
        # code, so editing it and hitting Re-run is how a user iterates on a
        # snippet the model wrote. Wrap it back into the params shape.
        if rec is not None and rec.operation == "execute_code":
            return {"code": self.editor.toPlainText()}
        try:
            params = json.loads(self.editor.toPlainText() or "{}")
        except json.JSONDecodeError as e:
            self._warn(f"Invalid JSON: {e}")
            return None
        if not isinstance(params, dict):
            self._warn("Parameters must be a JSON object.")
            return None
        return params

    def _save(self, _checked=False):
        rec = self._selected_record()
        if rec is None:
            return
        params = self._edited_params()
        if params is None:
            return
        res = self._apply({"operation": "update", "index": rec.index, "params": params})
        if res is not None and res.get("success"):
            self.editor.document().setModified(False)

    def _save_rerun(self, _checked=False):
        rec = self._selected_record()
        if rec is None:
            return
        params = self._edited_params()
        if params is None:
            return
        if not rec.executable:
            self._warn(f"'{rec.operation}' cannot be re-run — only modeling steps can.")
            return
        self._apply_maybe_force({"operation": "reexecute", "index": rec.index, "params": params})
        self.editor.document().setModified(False)

    def _menu(self, pos) -> None:
        menu = QtWidgets.QMenu(self)
        for action in (
            self.act_next,
            self.act_all,
            self.act_rollback,
            self.act_accept,
            self.act_reject,
            self.act_replay,
        ):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.act_copy)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _load_splitter() -> list[int]:
        """The user's region sizes from the previous session, if any."""
        try:
            from rpc_server.settings import load_settings

            sizes = load_settings().get("step_panel_splitter")
            if (
                isinstance(sizes, list)
                and len(sizes) == 3
                and all(isinstance(s, int) and s > 0 for s in sizes)
            ):
                return sizes
        except Exception:
            pass
        return [170, 240, 110]

    def _save_splitter(self) -> None:
        """Persist region sizes (debounced from splitterMoved)."""
        try:
            from rpc_server.settings import load_settings, save_settings

            sizes = self._splitter.sizes()
            if len(sizes) == 3 and all(s > 0 for s in sizes):
                settings = load_settings()
                settings["step_panel_splitter"] = sizes
                save_settings(settings)
        except Exception:
            pass

    @staticmethod
    def _load_step_width() -> int:
        """The Step column's width from the previous session, else a default."""
        try:
            from rpc_server.settings import load_settings

            width = load_settings().get("step_panel_step_width")
            if isinstance(width, int) and 60 <= width <= 2000:
                return width
        except Exception:
            pass
        return 220

    def _columns_changed(self, *_) -> None:
        self._columns_timer.start()

    def _save_step_width(self) -> None:
        """Persist the Step column's width (debounced from sectionResized)."""
        try:
            from rpc_server.settings import load_settings, save_settings

            width = self.tree.header().sectionSize(1)
            if width > 0:
                settings = load_settings()
                settings["step_panel_step_width"] = width
                save_settings(settings)
        except Exception:
            pass

    def _log(self, text: str, kind: str = "info") -> None:
        """Append one timestamped entry to the persistent console.

        "ok" entries keep the theme's text color, "info" is dimmed, "error"
        is red — the MATLAB Command-Window idiom: output is quiet, failures
        are loud. The full history (200 entries) is the single place every
        outcome lands, so nothing is duplicated in the status footer. The
        entries list is the model and the QTextEdit its re-rendered view, so
        the human-facing spotlight line (double-click) can render below the
        history without becoming part of it.
        """
        self._entries.append({"stamp": time.strftime("%H:%M:%S"), "text": text, "kind": kind})
        del self._entries[:-200]
        self._render_log()

    def _entry_html(self, entry: dict) -> str:
        message = html.escape(entry["text"]).replace("\n", "<br>")
        color = {
            "info": self._colors["dim"],
            "error": self._colors["red"],
        }.get(entry["kind"])
        if color:
            message = f"<span style='color:{color}'>{message}</span>"
        return f"<span style='color:{self._colors['dim']}'>[{entry['stamp']}]</span> {message}"

    def _spotlight_html(self, entry: dict) -> str:
        """The double-clicked step's description: the WHOLE line — timestamp
        included — bright and bold, so it reads as the answer, not as log
        noise."""
        message = html.escape(entry["text"]).replace("\n", "<br>")
        line = f"[{entry['stamp']}] {message}"
        return f"<b><span style='color:{self._colors['seltext']}'>{line}</span></b>"

    def _render_log(self) -> None:
        lines = [self._entry_html(e) for e in self._entries]
        if self._spotlight is not None:
            lines.append(self._spotlight_html(self._spotlight))
        self.log.setHtml("<br>".join(lines))
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _warn(self, text: str) -> None:
        self._log(text, "error")

    def _confirm(self, text: str) -> bool:
        answer = QtWidgets.QMessageBox.question(
            self,
            PANEL_TITLE,
            text,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        return answer == QtWidgets.QMessageBox.Yes


# --- lifecycle ---------------------------------------------------------------
#
# Look the dock up by objectName, not by Python class: ``findChild(StepPanel, …)``
# matches on the class object, so after a hot reload (importlib.reload) every
# surviving dock belongs to the OLD class, is therefore invisible to the new
# module, and a fresh dock gets built on top of it. The result was a stack of
# identically-named docks where findChild returned a hidden stale one — so the
# toolbar toggle set visibility on the wrong widget and appeared to do nothing.


def _find_panels(parent) -> list:
    """Every CADPilot dock parented to the main window, regardless of class."""
    return list(parent.findChildren(QtWidgets.QDockWidget, PANEL_NAME))


def _canonical_panel(parent):
    """The live dock built by the CURRENT StepPanel class, or None."""
    for panel in _find_panels(parent):
        if isinstance(panel, StepPanel):
            return panel
    return None


def _drop_panel(parent, panel) -> None:
    panel.setVisible(False)
    parent.removeDockWidget(panel)
    panel.deleteLater()


def panel_visible() -> bool:
    try:
        parent = FreeCADGui.getMainWindow()
    except Exception:
        return False
    if parent is None:
        return False
    return any(p.isVisible() for p in _find_panels(parent))


def ensure_panel() -> StepPanel | None:
    """Return the single live dock, rebuilding it once if needed. Idempotent.

    Surviving docks from an older class (hot reload) are torn down and replaced
    by one current-class dock that inherits what was on screen, so exactly one
    ``CADPilotStepPanel`` exists and the toolbar toggle always drives it.
    """
    try:
        parent = FreeCADGui.getMainWindow()
    except Exception:
        return None
    if parent is None:
        return None

    panels = _find_panels(parent)
    panel = _canonical_panel(parent)
    if panel is None:
        want_visible = any(p.isVisible() for p in panels) if panels else _load_visible()
        for stale in panels:
            _drop_panel(parent, stale)
        panel = StepPanel(parent)
        parent.addDockWidget(QtCore.Qt.RightDockWidgetArea, panel)
        panel.setVisible(want_visible)
    for duplicate in _find_panels(parent):
        if duplicate is not panel:
            _drop_panel(parent, duplicate)
    return panel


def show_step_panel(visible: bool) -> None:
    panel = ensure_panel()
    if panel is None:
        return
    panel.setVisible(bool(visible))
    _save_visible(bool(visible))


def _load_visible() -> bool:
    try:
        from rpc_server.settings import load_settings

        return bool(load_settings().get("step_panel_visible", False))
    except Exception:
        return False


def _save_visible(value: bool) -> None:
    try:
        from rpc_server.settings import load_settings, save_settings

        settings = load_settings()
        settings["step_panel_visible"] = bool(value)
        save_settings(settings)
    except Exception:
        pass
