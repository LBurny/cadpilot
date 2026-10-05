"""Guards for the review-loop / dispatch fixes found in real use.

Every item here was a real report from a modeling session:

* ``step_control status`` returned the full records of a long session
  (tens of KB, every inspection step carrying its whole snippet) with no
  compact option;
* ``reexecute`` silently rolled the later done steps back to planned;
* ``update`` could not rename a done step, and ``reexecute`` ignored ``label``;
* ``step_plan`` accepted an execute_code step and then skipped it at run time;
* a fillet on a bare Part object left the base visible beside the result;
* a GUI dispatch sat in silence for the full 60 s timeout while the user held
  a mouse button, then reported it;
* ``get_view`` blamed the view type for every capture failure.

The addon cannot be imported without FreeCAD, so these parse the source.
"""

import ast
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server"
_SRC = Path(__file__).resolve().parents[1] / "src" / "cadpilot"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(tree: ast.Module, name: str):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _strings(node) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def _subscript_keys(node) -> set[str]:
    """Every ``something["key"]`` literal used inside ``node``."""
    return {
        n.slice.value
        for n in ast.walk(node)
        if isinstance(n, ast.Subscript)
        and isinstance(n.slice, ast.Constant)
        and isinstance(n.slice.value, str)
    }


# --- step_control status ------------------------------------------------------


def test_status_offers_a_summary_view():
    """The compact view: one row per step, no params. Without it a caller paid
    for every snippet it had ever inspected just to learn which step is next."""
    body = _func(_tree(_ADDON / "step_engine.py"), "_status")
    assert "summary" in _subscript_keys(body)
    assert "steps" in _strings(body) or "steps" in _subscript_keys(body)
    # The full-records default must survive for callers that need the params.
    assert "records" in _strings(body)


def test_status_accepts_limit_and_offset():
    """A middle ground between "one row per step" and "everything"."""
    body = _func(_tree(_ADDON / "step_engine.py"), "_status")
    keys = _subscript_keys(body)
    assert {"limit", "offset"} <= keys


# --- reexecute ---------------------------------------------------------------


def test_reexecute_reports_the_steps_it_rolled_back():
    """The rewind is by design, but it was silent: the reply carried a done
    count and nothing else, and the caller's later model had vanished."""
    body = _func(_tree(_ADDON / "step_engine.py"), "_reexecute")
    assert "rewound" in _strings(body) or "rewound" in _subscript_keys(body)
    assert "warning" in _strings(body)
    assert "planned" in " ".join(_strings(body))


def test_reexecute_applies_a_label_instead_of_storing_it_as_a_param():
    """`label` is presentation: it must reach rec.label, not land in params as
    an inert key."""
    body = _func(_tree(_ADDON / "step_engine.py"), "_reexecute")
    assert "label" in " ".join(_strings(body)), "reexecute must read a label argument"
    assigns_label = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "label" for t in n.targets)
    ]
    assert assigns_label, "reexecute must write rec.label"


# --- update / step_plan -------------------------------------------------------


def test_update_falls_back_to_renaming_a_done_step():
    tree = _tree(_ADDON / "step_engine.py")
    body = _func(tree, "_apply_op")
    assert "set_label" in _names(body), "update must be able to rename a done step"


def test_set_plan_refuses_a_bare_execute_code_step():
    """Accepted-then-skipped is worse than refused: a planned snippet without
    its code cannot run, so the plan call has to say so."""
    body = _func(_tree(_ADDON / "step_engine.py"), "_apply_op")
    text = " ".join(_strings(body))
    assert "code" in text and "execute_code" in text
    assert "without a 'code' key" in text


# --- fillet / chamfer on a bare Part object -----------------------------------


def test_part_level_dress_up_hides_its_base():
    """Part::Fillet/Chamfer draw a SECOND solid beside the base, so the caller
    was left with two overlapping parts and no hint which to keep."""
    body = _func(_tree(_ADDON / "feature_ops.py"), "_build_fillet_chamfer")
    assert "Visibility" in _names(body)
    assert "hidden_base" in _strings(body)
    # The color must come across BEFORE the base is hidden, or hiding a colored
    # base exposes a default-gray feature.
    assert "_inherit_appearance" in _names(body)


def test_describe_feature_mentions_a_hidden_base():
    body = _func(_tree(_ADDON / "feature_ops.py"), "describe_feature")
    assert "hidden_base" in _subscript_keys(body)


# --- GUI dispatch and user interaction ---------------------------------------


def test_dispatch_reports_user_back_pressure_without_burning_the_timeout():
    """Holding a mouse button held the queue back, and the call waited the whole
    timeout in silence before saying so. It now reports the reason as soon as
    the guard is provably holding the queue."""
    tree = _tree(_ADDON / "gui_dispatch.py")
    body = _func(tree, "dispatch_to_gui")
    assert "_USER_HOLD_GRACE" in _names(body), "the wait must be bounded by the grace window"
    assert "_defer_reason" in _names(body)
    # A deferral must short-circuit the wait, not sit inside the timeout branch.
    breaks = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and "_defer_reason" in _names(n.test)
        and any(isinstance(b, ast.Break) for b in ast.walk(n))
    ]
    assert breaks, "the wait loop must break out early on user-interaction back-pressure"
    grace = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_USER_HOLD_GRACE" for t in n.targets)
    )
    assert 0 < grace.value.value <= 5.0, "grace must be short enough to feel immediate"


# --- execute_code namespace and get_view diagnostics --------------------------


def test_execute_code_pre_imports_what_the_docstring_promises():
    """The tool has always promised Part and the snippets use App; neither was
    in the namespace, so the documented first line raised NameError."""
    tree = _tree(_ADDON / "rpc_server.py")
    imported = {
        alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names
    }
    assert "Part" in imported
    assigns_app = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "App" for t in n.targets)
    ]
    assert assigns_app, "App must alias FreeCAD for snippets"


def test_get_view_asks_for_the_real_failure_reason():
    """One message blamed the view type for every None: occluded window, empty
    PNG, dispatch timeout — all of them."""
    tree = _tree(_ADDON / "rpc_server.py")
    assert "get_last_screenshot_error" in {
        n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
    }
    body = _func(tree, "get_active_screenshot")
    assert "_last_screenshot_error" in _names(body)
    core = _tree(_SRC / "operations" / "core.py")
    assert "get_last_screenshot_error" in _names(_func(core, "get_view_operation"))
