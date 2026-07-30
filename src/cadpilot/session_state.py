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
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    """Root directory for MCP-side persistent state (sessions, patterns)."""
    return Path(os.environ.get("CADPILOT_HOME", Path.home() / ".cadpilot"))


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
        """Pop n steps from the redo buffer back onto the log (after a redo)."""
        restored: list[Step] = []
        for _ in range(min(n, len(self.redo_buffer))):
            step = self.redo_buffer.pop(0)
            step.step_number = len(self.steps) + 1
            self.steps.append(step)
            restored.append(step)
        self.updated_at = _now()
        return restored

    def non_atomic_steps_after(self, step_number: int) -> list[int]:
        return [s.step_number for s in self.steps[step_number:] if not s.atomic]

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
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelingSession:
        return cls(
            session_id=data["session_id"],
            name=data["name"],
            doc_name=data["doc_name"],
            status=data.get("status", "active"),
            steps=[Step.from_dict(s) for s in data.get("steps", [])],
            notes=list(data.get("notes", [])),
            redo_buffer=[Step.from_dict(s) for s in data.get("redo_buffer", [])],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


# --- persistence ------------------------------------------------------------


def new_session(name: str, doc_name: str) -> ModelingSession:
    return ModelingSession(session_id=uuid.uuid4().hex[:12], name=name, doc_name=doc_name)


def save_session(session: ModelingSession) -> Path:
    path = sessions_dir() / f"{session.session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_session(session_id: str) -> ModelingSession | None:
    path = sessions_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return ModelingSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError):
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
