"""Assembly sessions: component registry + joint sequence + precomputed-undo rollback.

独立于建模会话（session_state.py）的装配状态机。每个装配步骤在记录时就
预计算好 undo 负载（删哪些关节/裁剪、恢复哪些 Link 位姿、Link 重指向谁），
rollback 时聚合为单个 rollback_step RPC spec 发给 addon 原子执行。

Storage layout: ``<data_dir>/assembly/<session_id>.json``，data_dir 与建模会话
共用 ``$CADPILOT_HOME``（默认 ``~/.cadpilot``）。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .session_state import _now, data_dir

logger = logging.getLogger("CADPilot")

_lock = threading.Lock()
_current: AssemblySession | None = None


def assembly_dir():
    path = data_dir() / "assembly"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class AssemblyStep:
    """一个装配步骤；undo 是预计算的 rollback_step spec 字段。"""

    step_number: int
    operation: str  # start / add_component / mate / unmate
    description: str
    spec_echo: dict[str, Any] = field(default_factory=dict)
    undo: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)


@dataclass
class AssemblySession:
    session_id: str
    name: str
    doc_name: str
    assembly_name: str
    ground_part: str
    status: str = "active"  # active | completed
    components: dict[str, Any] = field(default_factory=dict)  # part -> {"link", "added_step"}
    joints: list[dict[str, Any]] = field(default_factory=list)
    steps: list[AssemblyStep] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


def save(session: AssemblySession) -> None:
    """原子写入（tmp + replace）：写盘失败不会损坏已保存的会话文件。"""
    session.updated_at = _now()
    path = assembly_dir() / f"{session.session_id}.json"
    payload = asdict(session)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def load(session_id: str) -> AssemblySession | None:
    """加载会话；文件缺失/损坏/结构不符时返回 None（与 session_state 一致）。"""
    path = assembly_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        d["steps"] = [s if isinstance(s, AssemblyStep) else AssemblyStep(**s) for s in d["steps"]]
        return AssemblySession(**d)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("cannot load assembly session %s: %s", session_id, exc)
        return None


def list_sessions() -> list[dict[str, Any]]:
    out = []
    for fn in assembly_dir().glob("*.json"):
        s = load(fn.stem)
        if s is None:
            logger.warning("skipping corrupt assembly session %s", fn)
            continue
        out.append(
            {
                "session_id": s.session_id,
                "name": s.name,
                "doc_name": s.doc_name,
                "status": s.status,
                "steps": len(s.steps),
                "updated_at": s.updated_at,
            }
        )
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out


def start_session(doc_name: str, ground: str, name: str = "") -> AssemblySession:
    sid = uuid.uuid4().hex[:12]
    session = AssemblySession(
        session_id=sid,
        name=name or f"assembly-{sid}",
        doc_name=doc_name,
        assembly_name="MCP_Assembly",
        ground_part=ground,
    )
    save(session)
    set_current(session)
    return session


def current_session() -> AssemblySession | None:
    with _lock:
        return _current


def set_current(session: AssemblySession | None) -> None:
    global _current
    with _lock:
        _current = session


def resume_session(session_id: str) -> AssemblySession | None:
    session = load(session_id)
    if session is None:
        return None
    set_current(session)
    return session


def record_step(
    session: AssemblySession, operation: str, description: str, spec_echo: dict, undo: dict
) -> AssemblyStep:
    step = AssemblyStep(
        step_number=len(session.steps) + 1,
        operation=operation,
        description=description,
        spec_echo=spec_echo,
        undo=undo,
    )
    session.steps.append(step)
    save(session)
    return step


def plan_rollback(session: AssemblySession, to_step: int) -> dict[str, Any]:
    """聚合 to_step 之后所有步骤的 undo（逆序）为单个 rollback_step spec。

    links_restore 记录的是每步**之前**的 Link 位姿快照；逆序遍历时
    setdefault 保留最靠后步骤的快照，即最接近 to_step 时刻的状态。
    """
    joints: list[str] = []
    cuts: list[str] = []
    restore: dict[str, Any] = {}
    repoint: dict[str, Any] = {}
    remove_links: list[str] = []
    for step in reversed([s for s in session.steps if s.step_number > to_step]):
        undo = step.undo
        joints += list(undo.get("joints_to_delete", []))
        cuts += list(undo.get("cuts_to_delete", []))
        for link, plc in undo.get("links_restore", {}).items():
            restore.setdefault(link, plc)
        repoint.update(undo.get("links_repoint", {}))
        remove_links += list(undo.get("remove_links", []))
    return {
        "operation": "rollback_step",
        "joints_to_delete": joints,
        "cuts_to_delete": cuts,
        "links_restore": restore,
        "links_repoint": repoint,
        "remove_links": remove_links,
    }


def truncate_after_rollback(session: AssemblySession, to_step: int) -> None:
    session.steps = [s for s in session.steps if s.step_number <= to_step]
    session.joints = [j for j in session.joints if j["step"] <= to_step]
    session.components = {p: c for p, c in session.components.items() if c["added_step"] <= to_step}
    save(session)
