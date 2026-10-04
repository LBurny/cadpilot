"""``execute_code`` must survive a snippet that closes or replaces the document.

Reproduced live on 1.1.4 (twice, while cleaning up test documents): a snippet
that calls ``FreeCAD.closeDocument(<the active document>)`` commits its changes,
then the wrapper's own bookkeeping reads the document NAME off a reference that
is now deleted and raises::

    ReferenceError: Cannot access attribute 'Name' of deleted object

so a snippet that had already succeeded is reported to the client as a failure.
The same landmine hits a snippet that replaces the active document
(``setActiveDocument``), which is the documented way to direct a step's journal
entry at another document.

The addon cannot be imported without FreeCAD, so this parses the source — the
same approach as the other addon guard tests.
"""

import ast
from pathlib import Path

_RPC_SERVER = (
    Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server" / "rpc_server.py"
)
_TREE = ast.parse(_RPC_SERVER.read_text(encoding="utf-8"))


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _parents(body):
    parents = {}
    for node in ast.walk(body):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_guarded(node, parents) -> bool:
    """True when ``node`` sits inside a try block or a contextlib.suppress."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.Try):
            return True
        if isinstance(current, ast.With):
            for item in current.items:
                if any(
                    isinstance(n, ast.Attribute) and n.attr == "suppress"
                    for n in ast.walk(item.context_expr)
                ):
                    return True
        current = parents.get(current)
    return False


def test_document_name_is_not_read_after_the_snippet_could_delete_it():
    body = _func(_TREE, "execute_code")
    parents = _parents(body)
    reads = [
        n
        for n in ast.walk(body)
        if isinstance(n, ast.Attribute)
        and n.attr == "Name"
        and isinstance(n.value, ast.Name)
        and n.value.id == "doc"
    ]
    assert reads, "the wrapper is expected to report which document it changed"
    unguarded = [n for n in reads if not _is_guarded(n, parents)]
    assert not unguarded, (
        "the snippet may close or replace the active document; reading doc.Name afterwards "
        "raises ReferenceError on the deleted object and reports a successful snippet as a failure"
    )
