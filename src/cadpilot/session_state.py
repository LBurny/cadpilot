"""Modeling sessions: step recording, rollback bookkeeping, JSON persistence.

A session is bound to one FreeCAD document. Mutations made through ``cad()``
while a session is active are recorded as steps; each step corresponds to one
document transaction on the addon side, so ``session_rollback`` can undo them
with ``doc.undo()`` and truncate the log — the modeling analog of nsforge's
derivation rollback, backed by FreeCAD's native transaction stack.

Storage layout: ``<data_dir>/sessions/<session_id>.json`` where data_dir is
``$CADPILOT_HOME`` or ``~/.cadpilot``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Session ids are uuid4().hex[:12]. The regex is a path-traversal guard: a
# session_id is joined into a filename, so a caller-supplied "../.." must be
# refused before it ever touches the filesystem.
# Path-safety, not uuid-purity: tests and legacy stores use short
# ids like "s1". What matters is that no separator or dot can slip
# through, so the id can never traverse out of its directory.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch(str(session_id or "")))


def data_dir() -> Path:
    """Root directory for MCP-side persistent state (sessions, patterns)."""
    # `or` (not a default=) because CADPILOT_HOME="" must not resolve to the
    # process working directory: Path("") is ".", and sessions/patterns would
    # silently land wherever the server happened to start.
    return Path(os.environ.get("CADPILOT_HOME") or (Path.home() / ".cadpilot"))


def sessions_dir() -> Path:
    return data_dir() / "sessions"


_last_now: datetime | None = None
_now_lock = threading.Lock()


def _now() -> str:
    # Microsecond precision AND strictly increasing within the process:
    # list_sessions sorts by updated_at, but Windows' clock granularity
    # (~15.6 ms) returns identical timestamps for back-to-back saves.
    global _last_now
    with _now_lock:
        now = datetime.now()
        if _last_now is not None and now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
        return now.isoformat(timespec="microseconds")


@dataclass
class Step:
    """One recorded modeling step (= one committed document transaction)."""

    step_number: int
    operation: str  # cad operation: create_object / edit_object / ... / execute_code
    description: str
    params_summary: str = ""
    result_summary: str = ""
    objects_after: list[str] = field(default_factory=list)  # state fingerprint
    atomic: bool = True  # False → no matching transaction; rollback past it is unsafe
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Step:
        return cls(
            step_number=data["step_number"],
            operation=data["operation"],
            description=data.get("description", ""),
            params_summary=data.get("params_summary", ""),
            result_summary=data.get("result_summary", ""),
            objects_after=list(data.get("objects_after", [])),
            atomic=bool(data.get("atomic", True)),
            timestamp=data.get("timestamp", ""),
        )


@dataclass
class ModelingSession:
    session_id: str
    name: str
    doc_name: str
    status: str = "active"  # active | paused | completed
    steps: list[Step] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    redo_buffer: list[Step] = field(
        default_factory=list
    )  # truncated steps, restorable until a new step
    # The document's object set when the session BEGAN: rolling back to step 0
    # must restore exactly this, and without it the post-rollback check had
    # nothing to compare against (so a rollback that left objects behind still
    # reported success). None = unknown (session resumed from an older file),
    # reported as "unverified" rather than guessed.
    initial_objects: list[str] | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # --- step log ---------------------------------------------------------

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def add_step(
        self,
        operation: str,
        description: str,
        params_summary: str = "",
        result_summary: str = "",
        objects_after: list[str] | None = None,
        atomic: bool = True,
    ) -> Step:
        # A new committed transaction invalidates FreeCAD's redo stack too.
        self.redo_buffer.clear()
        step = Step(
            step_number=len(self.steps) + 1,
            operation=operation,
            description=description,
            params_summary=params_summary,
            result_summary=result_summary,
            objects_after=sorted(objects_after or []),
            atomic=atomic,
        )
        self.steps.append(step)
        self.updated_at = _now()
        return step

    def add_note(self, note: str, note_type: str = "observation") -> dict[str, Any]:
        entry = {
            "after_step": len(self.steps),
            "note": note,
            "note_type": note_type,
            "timestamp": _now(),
        }
        self.notes.append(entry)
        self.updated_at = _now()
        return entry

    def truncate_to(self, step_number: int) -> list[Step]:
        """Move steps after ``step_number`` into the redo buffer; returns them."""
        removed = self.steps[step_number:]
        self.redo_buffer = list(removed)
        self.steps = self.steps[:step_number]
        self.updated_at = _now()
        return removed

    def restore_steps(self, n: int) -> list[Step]:
        """Pop n steps from the redo buffer back onto the log (after a redo).

        Only ATOMIC steps count toward n and are restored: a non-atomic entry
        in the buffer owns no undo entry, so FreeCAD's redo never re-applied
        it and its effect never left the model — restoring its log row would
        desynchronize the log from the document by one step per skipped
        entry. Such entries are dropped as they surface.
        """
        restored: list[Step] = []
        while len(restored) < n and self.redo_buffer:
            step = self.redo_buffer.pop(0)
            if not step.atomic:
                continue
            step.step_number = len(self.steps) + 1
            self.steps.append(step)
            restored.append(step)
        self.updated_at = _now()
        return restored

    # --- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "doc_name": self.doc_name,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "notes": self.notes,
            "redo_buffer": [s.to_dict() for s in self.redo_buffer],
            "initial_objects": self.initial_objects,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelingSession:
        initial = data.get("initial_objects")
        return cls(
            session_id=data["session_id"],
            name=data["name"],
            doc_name=data["doc_name"],
            status=data.get("status", "active"),
            steps=[Step.from_dict(s) for s in data.get("steps", [])],
            notes=list(data.get("notes", [])),
            redo_buffer=[Step.from_dict(s) for s in data.get("redo_buffer", [])],
            initial_objects=None if initial is None else sorted(initial),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


# --- persistence ------------------------------------------------------------


def new_session(
    name: str, doc_name: str, initial_objects: list[str] | None = None
) -> ModelingSession:
    """Bind a session to a document; ``initial_objects`` is its starting state.

    Pass the document's current object names: they are the target of
    ``session_rollback(to_step=0)``, which otherwise has nothing to verify
    against. ``None`` means "unknown" (the addon could not be asked).
    """
    return ModelingSession(
        session_id=uuid.uuid4().hex[:12],
        name=name,
        doc_name=doc_name,
        initial_objects=None if initial_objects is None else sorted(initial_objects),
    )


def save_session(session: ModelingSession) -> Path:
    if not valid_session_id(session.session_id):
        # A session loaded from a hand-edited file could carry any string in
        # its id field; saving it would write outside CADPILOT_HOME.
        raise ValueError(f"invalid session id: {session.session_id!r}")
    path = sessions_dir() / f"{session.session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # A UNIQUE tmp per write: two overlapping mutations to one session (MCP
    # tools run on a thread pool) used to share one fixed `<sid>.tmp` and the
    # loser crashed on os.replace.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def persist_session(session: ModelingSession) -> str:
    """save_session for tool paths: a persistence failure must not escape the
    tool AFTER the mutation itself committed — the caller would see a hard
    protocol error for a change that actually succeeded, and likely retry it
    (double-apply). Returns "" on success or a warning to append."""
    try:
        save_session(session)
    except OSError as e:
        return (
            f"WARNING: the session could not be persisted ({e}); this step is "
            "recorded in memory only and will be lost when the server exits."
        )
    return ""


def load_session(session_id: str) -> ModelingSession | None:
    if not valid_session_id(session_id):
        return None
    path = sessions_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            # Valid JSON of the wrong shape is corrupt the same way: reading
            # it used to escape as AttributeError out of the tool.
            return None
        return ModelingSession.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None


def list_sessions() -> list[dict[str, Any]]:
    """Metadata for all persisted sessions, most recently updated first."""
    out = []
    directory = sessions_dir()
    if not directory.exists():
        return out
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue  # valid JSON of the wrong shape: skip, don't crash
        out.append(
            {
                "session_id": data.get("session_id", path.stem),
                "name": data.get("name", ""),
                "doc_name": data.get("doc_name", ""),
                "status": data.get("status", ""),
                "step_count": len(data.get("steps", [])),
                "updated_at": data.get("updated_at", ""),
            }
        )
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out


# --- current-session registry (mirrors nsforge tools/_state.py) --------------

_lock = threading.Lock()
_current_session: ModelingSession | None = None


def get_current_session() -> ModelingSession | None:
    with _lock:
        return _current_session


def set_current_session(session: ModelingSession | None) -> None:
    global _current_session
    with _lock:
        _current_session = session
