"""The CADPilot steps panel.

A QDockWidget owned by FreeCAD's main window: it lists the journal's steps as
a status-dotted list, shows the selected step's parameters in an inline JSON
editor, and drives the whole review loop — release (next/all/to-here), accept,
reject, roll back, replay.

It talks to ``step_engine`` directly (same process, GUI thread) rather than
through the RPC server, so the panel keeps working with the RPC server
stopped — which is the normal state while the user studies a model by hand.
"""

from __future__ import annotations

import json

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
_ACCEPTED_COLOR = QtGui.QColor(20, 110, 60)
_ICON_CACHE: dict = {}

NO_DOCUMENT = "No document open."


def _dot(color: QtGui.QColor, hollow: bool = False, size: int = 12) -> QtGui.QIcon:
    """A round status marker, painted at runtime so it follows no icon theme."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pm)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    if hollow:
        painter.setPen(QtGui.QPen(color, 1.6))
        painter.setBrush(QtCore.Qt.NoBrush)
    else:
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(color)
    painter.drawEllipse(QtCore.QRectF(1.5, 1.5, size - 3, size - 3))
    painter.end()
    return QtGui.QIcon(pm)


def _state_icon(rec) -> QtGui.QIcon:
    key = (rec.state, rec.accepted)
    if key not in _ICON_CACHE:
        accepted_done = rec.accepted and rec.state == sj.STATE_DONE
        color = _ACCEPTED_COLOR if accepted_done else _STATE_COLORS.get(rec.state, _GREY)
        _ICON_CACHE[key] = _dot(color, hollow=rec.state == sj.STATE_PLANNED)
    return _ICON_CACHE[key]


class StepPanel(QtWidgets.QDockWidget):
    def __init__(self, parent=None):
        super().__init__(PANEL_TITLE, parent)
        self.setObjectName(PANEL_NAME)
        self._digest: str | None = None
        self._note: str | None = None
        self._records: list = []
        self._details_for: int = 0
        self._build()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # --- construction ----------------------------------------------------

    def _std_icon(self, name: str) -> QtGui.QIcon:
        return self.style().standardIcon(getattr(QtWidgets.QStyle, name))

    def _action(self, text, tip, slot, icon=None) -> QtGui.QAction:
        # icon=None keeps the action text-only: FreeCAD's own chrome uses no
        # tick/cross glyphs, and neither does this panel.
        action = QtGui.QAction(self._std_icon(icon) if icon else QtGui.QIcon(), text, self)
        action.setToolTip(tip)
        action.triggered.connect(slot)
        return action

    def _build(self) -> None:
        box = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.header = QtWidgets.QLabel("", box)
        self.header.setWordWrap(True)
        layout.addWidget(self.header)

        self.progress = QtWidgets.QProgressBar(box)
        self.progress.setMaximumHeight(14)
        # The default chunk is the platform accent — a heavy saturated blue in
        # FreeCAD's dark theme. Re-tint it to the "done" green so the bar reads
        # as part of the steps language, not as a foreign banner.
        self.progress.setStyleSheet(
            "QProgressBar { border: 1px solid palette(mid); border-radius: 3px;"
            " text-align: center; background: palette(base); }"
            f"QProgressBar::chunk {{ background-color: {_STATE_COLORS[sj.STATE_DONE].name()}; }}"
        )
        layout.addWidget(self.progress)

        self.tree = QtWidgets.QTreeWidget(box)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["#", "Step", "Op"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemSelectionChanged.connect(self._on_selection)
        # Double-click jumps into the parameter editor (it no longer re-runs
        # blindly — editing IS the point of double-clicking).
        self.tree.itemDoubleClicked.connect(lambda *_: self.editor.setFocus())
        header = self.tree.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        layout.addWidget(self.tree, stretch=3)

        self.toolbar = QtWidgets.QToolBar(box)
        self.toolbar.setIconSize(QtCore.QSize(16, 16))
        self.toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.act_next = self._action(
            "Next", "Run the next planned step", self._run_next, "SP_MediaPlay"
        )
        self.act_all = self._action(
            "Run all",
            "Run until a step fails or the plan ends",
            self._run_all,
            "SP_MediaSkipForward",
        )
        self.act_rollback = self._action(
            "Roll back",
            "Undo back to just before the selected step",
            self._rollback,
            "SP_MediaSeekBackward",
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
            "SP_TrashIcon",
        )
        self.act_replay = self._action(
            "Replay",
            "Roll back to the start and re-run the whole journal",
            self._replay,
            "SP_BrowserReload",
        )
        self.act_copy = self._action(
            "Copy params",
            "Copy the selected step's parameter JSON to the clipboard",
            self._copy_params,
            "SP_FileDialogDetailedView",
        )
        for action in (
            self.act_next,
            self.act_all,
            self.act_rollback,
            self.act_accept,
            self.act_reject,
            self.act_replay,
        ):
            self.toolbar.addAction(action)
        layout.addWidget(self.toolbar)

        self.detail_meta = QtWidgets.QLabel("", box)
        self.detail_meta.setWordWrap(True)
        layout.addWidget(self.detail_meta)

        self.detail_error = QtWidgets.QLabel("", box)
        self.detail_error.setWordWrap(True)
        self.detail_error.setStyleSheet(f"color: {_RED.name()};")
        layout.addWidget(self.detail_error)

        self.editor = QtWidgets.QPlainTextEdit(box)
        self.editor.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.editor.setPlaceholderText("Step parameters (JSON)")
        layout.addWidget(self.editor, stretch=2)

        edit_row = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton(self._std_icon("SP_DialogSaveButton"), " Save", box)
        self.btn_save.setToolTip("Save the edited parameters to the planned/failed step")
        self.btn_save.clicked.connect(self._save)
        edit_row.addWidget(self.btn_save)
        self.btn_rerun = QtWidgets.QPushButton(
            self._std_icon("SP_BrowserReload"), " Save && re-run", box
        )
        self.btn_rerun.setToolTip(
            "Save the parameters and re-run the step (rolls back to just before it first)"
        )
        self.btn_rerun.clicked.connect(self._save_rerun)
        edit_row.addWidget(self.btn_rerun)
        edit_row.addStretch(1)
        layout.addLayout(edit_row)

        self.status = QtWidgets.QLabel("", box)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.setWidget(box)

    # --- refresh ---------------------------------------------------------

    def refresh(self) -> None:
        """Redraw when the journal OR the drift state changed.

        The list only needs the journal text, but the status line also reports
        whether the journal still matches FreeCAD's undo stack — and a manual
        Ctrl+Z changes that WITHOUT touching the journal (FreeCAD does not
        restore document-level property changes on undo), so the journal digest
        alone cannot gate the status line.
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
        note = self._summary(doc, records)
        if text == self._digest and note == self._note:
            return
        self._digest = text
        self._note = note
        self._records = records
        self._render(records, self._header_text(doc, records, sj.meta_from_json(text)), note)

    def _header_text(self, doc, records, meta) -> str:
        text = f"{doc.Label or doc.Name} — {sj.done_count(records)}/{len(records)} done"
        description = str(meta.get("description") or "").strip()
        if description:
            text += f"\nPlan: {description}"
        return text

    def _summary(self, doc, records) -> str:
        text = (
            f"{len(records)} step(s), "
            f"{sj.done_count(records)} done, {sj.pending_count(records)} planned, "
            f"{sum(1 for r in records if r.accepted)} accepted"
        )
        try:
            names = list(getattr(doc, "UndoNames", []) or [])
        except Exception:
            names = []
        # Same anchor as step_engine._status: the last ATOMIC step. A trailing
        # execute_code record has no transaction, so anchoring on the last
        # completed record outright would hide a manual undo here too.
        last = sj.last_atomic_done(records)
        if last and last.transaction not in names:
            text += "\nOut of sync with FreeCAD's undo stack (undone manually?)"
        return text

    def _render(self, records, header: str, note: str) -> None:
        self.header.setText(header)
        self.header.setVisible(bool(header))
        total = len(records)
        self.progress.setVisible(total > 0)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(sj.done_count(records))
        self.progress.setFormat("%v/%m done")

        selected = self._selected_index()
        self.tree.blockSignals(True)
        self.tree.clear()
        for rec in records:
            label = rec.label + (f"  — {rec.error}" if rec.error else "")
            item = QtWidgets.QTreeWidgetItem([str(rec.index), label, rec.operation])
            item.setIcon(0, _state_icon(rec))
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
        # Fit the list to its content instead of letting it stretch: a short
        # plan would otherwise sit above a large dead grey area, and the space
        # is worth more to the parameter editor below.
        rows = self.tree.topLevelItemCount()
        row_h = self.tree.sizeHintForRow(0) if rows else 0
        self.tree.setMaximumHeight(
            min(max(self.tree.header().height() + rows * row_h + 6, 72), 340)
        )
        self.status.setStyleSheet("")
        self.status.setText(note)
        self._update_details()
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
        self._update_actions()

    def _update_details(self) -> None:
        rec = self._selected_record()
        if rec is None:
            self._details_for = 0
            self.detail_meta.setText("")
            self.detail_error.setText("")
            self.detail_error.setVisible(False)
            if not self.editor.document().isModified():
                self.editor.setPlainText("")
            return
        parts = [f"Step {rec.index}", rec.operation, _STATE_LABEL.get(rec.state, rec.state)]
        if rec.accepted:
            parts.append("accepted")
        if rec.timestamp:
            parts.append(rec.timestamp)
        if rec.duration_ms:
            parts.append(f"{rec.duration_ms} ms")
        gained = len(rec.objects_after) - len(rec.objects_before)
        if rec.state == sj.STATE_DONE and gained:
            parts.append(f"{gained:+d} object(s)")
        self.detail_meta.setText(" · ".join(parts))
        self.detail_error.setText(rec.error or "")
        self.detail_error.setVisible(bool(rec.error))
        # Do not clobber a half-edited JSON on every 1s refresh: only refill
        # when the selection moved or the editor is untouched.
        if self._details_for != rec.index or not self.editor.document().isModified():
            self.editor.setPlainText(json.dumps(rec.params, ensure_ascii=False, indent=2) or "{}")
            self.editor.document().setModified(False)
        self._details_for = rec.index

    def _update_actions(self) -> None:
        rec = self._selected_record()
        has_doc = FreeCAD.ActiveDocument is not None
        has_planned = any(r.state == sj.STATE_PLANNED for r in self._records)
        any_done = any(r.state == sj.STATE_DONE for r in self._records)
        self.act_next.setEnabled(has_doc and has_planned)
        self.act_all.setEnabled(has_doc and has_planned)
        self.act_replay.setEnabled(has_doc and any_done)
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
        try:
            res = step_engine.apply_op(doc, spec)
        except Exception as e:
            self._warn(f"{type(e).__name__}: {e}")
            return None
        self._digest = None
        self._note = None
        self.refresh()
        if not res.get("success") and warn:
            self._warn(str(res.get("error") or "The operation failed."))
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
            self._warn(error)

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
                f"\nStep(s) {plan['blocking']} are non-atomic (execute_code), "
                "so undo may revert the wrong change."
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

    def _copy_params(self, _checked=False):
        rec = self._selected_record()
        if rec is None:
            return
        QtGui.QGuiApplication.clipboard().setText(
            json.dumps(rec.params, ensure_ascii=False, indent=2)
        )

    def _edited_params(self) -> dict | None:
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

    def _warn(self, text: str) -> None:
        # No symbol prefixes: warnings stand out by color, like the Report view.
        self.status.setStyleSheet(f"color: {_RED.name()};")
        self.status.setText(text)

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
