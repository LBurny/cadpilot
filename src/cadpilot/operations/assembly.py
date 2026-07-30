"""assembly_session 工具的操作实现：规格校验 → RPC assembly_op → 状态机记录。

装配会话与建模会话（session_state）完全独立：这里不记录建模步骤，
只维护组件注册表、关节序列和预计算 undo 的装配步骤。
"""

from __future__ import annotations

import logging
from typing import Any

from .. import assembly_state as astate
from ..responses import ToolResponse, json_response, text_response

logger = logging.getLogger("CADPilot")

JOINT_TYPES = [
    "fixed",
    "revolute",
    "cylindrical",
    "slider",
    "ball",
    "distance",
    "parallel",
    "perpendicular",
    "angle",
]

_REF_KEYS = ("face", "anchor", "point")

_EMPTY_UNDO: dict[str, Any] = {
    "joints_to_delete": [],
    "cuts_to_delete": [],
    "links_restore": {},
    "links_repoint": {},
    "remove_links": [],
}


def _undo(**overrides) -> dict[str, Any]:
    undo = {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in _EMPTY_UNDO.items()}
    undo.update(overrides)
    return undo


def _validate_ref(ref: Any) -> str | None:
    if not isinstance(ref, dict) or "part" not in ref:
        return "mate refs must be dicts with 'part'"
    if sum(1 for k in _REF_KEYS if k in ref) != 1:
        return f"mate ref needs exactly one of {_REF_KEYS}"
    return None


def _link_of(session: astate.AssemblySession, part_name: str) -> str:
    comp = session.components.get(part_name)
    if comp:
        return comp["link"]
    return f"L_{part_name}"


def _rpc_error(res: Any, operation: str) -> ToolResponse | None:
    """addon 显式返回 {"success": False, ...} 时生成错误响应，否则返回 None。

    只认显式的 False——旧 addon 的结果可能没有 success 键，不能按 falsy 处理。
    """
    if isinstance(res, dict) and res.get("success") is False:
        return text_response(f"assembly {operation} failed: {res.get('error', 'unknown error')}")
    return None


def assembly_session_operation(
    conn,
    operation: str,
    doc_name: str | None = None,
    name: str | None = None,
    part: str | None = None,
    joint: str | None = None,
    a: dict | None = None,
    b: dict | None = None,
    joint_type: str = "fixed",
    trim: dict | None = None,
    to_step: int | None = None,
    gap_samples: int = 8,
) -> ToolResponse:
    if operation == "start":
        if not (doc_name and part):
            return text_response("start requires doc_name and part (the ground part)")
        spec = {"operation": "start", "ground": part, "name": name or ""}
        res = conn.assembly_op(doc_name, spec)
        if (err := _rpc_error(res, "start")) is not None:
            return err
        session = astate.start_session(doc_name, part, name or "")
        session.assembly_name = res.get("assembly", "MCP_Assembly")
        st = astate.record_step(session, "start", f"ground {part}", spec, undo=_undo())
        session.components[part] = {
            "link": res.get("ground_link", f"L_{part}"),
            "added_step": st.step_number,
        }
        astate.save(session)
        return json_response(
            {
                "started": session.name,
                "session_id": session.session_id,
                "assembly": session.assembly_name,
                **res,
            }
        )

    session = astate.current_session()
    if session is None or session.status != "active":
        return text_response("No active assembly session; call start first")

    if operation == "add_component":
        if not part:
            return text_response("add_component requires part")
        if part in session.components:
            return text_response(f"{part} is already a component")
        res = conn.assembly_op(session.doc_name, {"operation": "add_component", "part": part})
        if (err := _rpc_error(res, "add_component")) is not None:
            return err
        st = astate.record_step(
            session, "add_component", part, {"part": part}, undo=_undo(remove_links=[res["link"]])
        )
        session.components[part] = {"link": res["link"], "added_step": st.step_number}
        astate.save(session)
        return json_response(res)

    if operation == "mate":
        if joint_type not in JOINT_TYPES:
            return text_response(f"joint_type must be one of {JOINT_TYPES}")
        for ref in (a, b):
            err = _validate_ref(ref)
            if err:
                return text_response(err)
            if ref["part"] not in session.components:
                return text_response(f"{ref['part']} is not a component; add_component first")
        spec: dict[str, Any] = {
            "operation": "mate",
            "a": a,
            "b": b,
            "joint": joint_type,
            "name": joint or "",
        }
        if trim:
            if trim.get("winner") not in ("inserted", "base"):
                return text_response("trim.winner must be 'inserted' or 'base'")
            spec["trim"] = trim
        res = conn.assembly_op(session.doc_name, spec)
        if (err := _rpc_error(res, "mate")) is not None:
            return err
        undo = _undo(joints_to_delete=[res["joint"]])
        if res.get("pre_placement") and res.get("moved_link"):
            undo["links_restore"] = {res["moved_link"]: res["pre_placement"]}
        if trim and res.get("trim"):
            undo["cuts_to_delete"] = [res["trim"]["cut"]]
            loser = b["part"] if trim["winner"] == "inserted" else a["part"]
            undo["links_repoint"] = {_link_of(session, loser): loser}
        st = astate.record_step(session, "mate", f"{a['part']} -> {b['part']}", spec, undo)
        session.joints.append(
            {
                "step": st.step_number,
                "name": res["joint"],
                "joint_type": joint_type,
                "a": a,
                "b": b,
                "trim": trim,
            }
        )
        astate.save(session)
        return json_response(res)

    if operation == "solve":
        return json_response(conn.assembly_op(session.doc_name, {"operation": "solve"}))

    if operation == "unmate":
        if not joint:
            return text_response("unmate requires a joint name")
        res = conn.assembly_op(session.doc_name, {"operation": "unmate", "joint": joint})
        if (err := _rpc_error(res, "unmate")) is not None:
            return err
        session.joints = [j for j in session.joints if j["name"] != joint]
        astate.save(session)
        return json_response(res)

    if operation == "rollback":
        if to_step is None:
            return text_response("rollback requires to_step")
        spec = astate.plan_rollback(session, to_step)
        res = conn.assembly_op(session.doc_name, spec)
        if (err := _rpc_error(res, "rollback")) is not None:
            return err
        astate.truncate_after_rollback(session, to_step)
        return json_response({"rolled_back_to_step": to_step, **res})

    if operation == "verify":
        return json_response(
            conn.assembly_op(session.doc_name, {"operation": "verify", "gap_samples": gap_samples})
        )

    if operation == "status":
        return json_response(
            {
                "session": session.name,
                "doc": session.doc_name,
                "assembly": session.assembly_name,
                "ground": session.ground_part,
                "components": session.components,
                "joints": session.joints,
                "steps": len(session.steps),
            }
        )

    if operation == "complete":
        session.status = "completed"
        astate.save(session)
        astate.set_current(None)
        return text_response(f"Assembly session '{session.name}' completed")

    return text_response(f"Unknown assembly operation: {operation}")
