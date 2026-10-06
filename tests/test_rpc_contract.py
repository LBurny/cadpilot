"""The XML-RPC wire contract, checked against every copy of it at once.

The same ~34-method interface is written down in FOUR places, and until this
file nothing compared them:

1. ``freecad_client.FreeCADConnection`` — the proxy that actually puts bytes on
   the wire;
2. ``rpc_server.FreeCADRPC`` — the addon handler that receives them;
3. ``tests/conftest.FakeFreeCADConnection`` — the default test double;
4. the call sites in ``operations/`` and ``server.py``.

XML-RPC is positional and the addon dispatches by attribute name, so a rename
or an arity change on either side of the wire is a runtime ``Fault`` — and every
test that runs against the fake sees none of it. That is exactly how
``session(action='redo')`` shipped broken: ``core.py`` sends three positional
arguments (``doc_name, n, trust_journal``), the addon's ``redo_transactions``
took only two, and the fake accepted the third. The only guard against a repeat
was a single hand-written assertion for that one method — a fix generalised as
a special case. These tests are the generalisation:

* the two sides must expose the SAME wire methods (either direction: a rename
  is a fault, and an addon method no client calls is dead weight);
* every positional argument the client can send must land on an addon
  parameter, and the ``screenshot``/``doc_name`` tail ``_invoke_with_screenshot``
  appends must bind to parameters with exactly those names in that order (they
  are appended positionally, so a swapped pair would silently bind the document
  name into the screenshot slot);
* the fake must implement every wire method AND mirror the client method's own
  signature (the fake is called through the Python surface, not the wire — its
  contract is the client method's parameter list, canned);
* the operations and server layers may only call methods that exist on the
  connection.

What this deliberately does NOT check: return shapes. The fake returns canned
success dicts, so a field the addon stops producing still passes the suite —
closing that needs either a strict fake (shapes declared once) or live calls.
The guards here are the ones that can be decided from source alone.

The addon cannot be imported without FreeCAD, so every side is read with ``ast``.
"""

import ast
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CLIENT = _ROOT / "src" / "cadpilot" / "freecad_client.py"
_ADDON = _ROOT / "addon" / "CADPilot" / "rpc_server" / "rpc_server.py"
_CONFTEST = _ROOT / "tests" / "conftest.py"
_SRC = _ROOT / "src" / "cadpilot"

_CLIENT_CLASS = "FreeCADConnection"
_ADDON_CLASS = "FreeCADRPC"
_FAKE_CLASS = "FakeFreeCADConnection"

# Wrappers append the tail positionally; these are the parameter names each
# appended slot must bind to, in order.
_SCREENSHOT_TAIL = ("screenshot", "doc_name")

# Public client methods that put nothing on the wire. This list is the ONLY
# exemption: any other public method must make a literal ``_invoke`` call, and
# ``test_every_public_client_method_is_accounted_for`` holds the line. Without
# that rule a method whose wire call fails to parse (a dynamic name, a splat)
# would silently drop out of every check below and narrow the contract back to
# "whatever the extractor happens to see".
_NON_WIRE_METHODS = {"disconnect"}


@dataclass
class Wire:
    """What one client method puts on the wire."""

    rpc_name: str
    min_positional: int
    max_positional: int
    # wire index -> the parameter name that index must bind to (tail slots only)
    tail_names: dict = field(default_factory=dict)


def _parse(path):
    return ast.parse(Path(path).read_text(encoding="utf-8"))


def _class_methods(tree, class_name) -> dict:
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    assert cls is not None, f"class {class_name} not found"
    return {f.name: f for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _positional(fn) -> tuple[list[str], bool]:
    """(names of positional params without self, whether *args follows)."""
    decorated = {ast.unparse(d) for d in fn.decorator_list}
    assert not decorated & {"staticmethod", "classmethod"}, (
        f"{fn.name} is {sorted(decorated)}: _positional drops the first parameter "
        "assuming it is self/cls, so a static method would silently lose an "
        "argument — teach the extractor instead"
    )
    a = fn.args
    return [x.arg for x in a.posonlyargs + a.args][1:], a.vararg is not None


def _body_nodes(node):
    """Walk a function body WITHOUT descending into nested defs or lambdas.

    A nested helper's ``self._invoke(...)`` is not the enclosing method's wire
    call; attributing it would both invent wire traffic and satisfy the
    narrowing test for a method that makes none.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield child
        yield from _body_nodes(child)


def _defaults(fn) -> int:
    return len(fn.args.defaults)


def _wire_of(name: str, fn) -> Wire | None:
    """The wire calls a client method makes, merged across call sites.

    Returns None for a method with no literal wire call; the narrowing test
    decides whether that is allowed.
    """
    wire = None
    for call in _body_nodes(fn):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr not in ("_invoke", "_invoke_with_screenshot"):
            continue
        if not (isinstance(call.func.value, ast.Name) and call.func.value.id == "self"):
            continue
        if not (call.args and isinstance(call.args[0], ast.Constant)):
            continue
        rpc_name = call.args[0].value
        assert isinstance(rpc_name, str), f"{name}: RPC name is not a literal"
        splatted = [a for a in call.args[1:] if isinstance(a, ast.Starred)]
        assert not splatted, (
            f"{name}: a splatted wire argument cannot be counted statically, so it "
            "would silently shrink every arity check; spell the arguments out or "
            "extend _wire_of"
        )
        positional = len(call.args) - 1
        # The wrapper appends only the slots the call site asks for; a plain
        # _invoke call may carry no keywords at all (XML-RPC is positional).
        tail: tuple = ()
        kws = {k.arg for k in call.keywords}
        if call.func.attr == "_invoke_with_screenshot":
            unexpected = kws - set(_SCREENSHOT_TAIL)
            assert not unexpected, (
                f"{name}: the wrapper forwards only {list(_SCREENSHOT_TAIL)}, "
                f"keyword {sorted(unexpected)} would never reach the addon"
            )
            if "doc_name" in kws and "screenshot" not in kws:
                # The wrapper sends (None, doc_name): a placeholder occupies the
                # screenshot slot, so the tail is TWO wide with doc_name second.
                tail = _SCREENSHOT_TAIL
            else:
                tail = tuple(t for t in _SCREENSHOT_TAIL if t in kws)
        else:
            assert not kws, (
                f"{name}: _invoke is forwarded positionally, keyword {sorted(kws)} "
                "would never reach the addon"
            )
        if wire is None:
            wire = Wire(rpc_name, positional, positional + len(tail))
        assert wire.rpc_name == rpc_name, (
            f"{name} calls two different RPCs: {wire.rpc_name!r} and {rpc_name!r}"
        )
        wire.min_positional = min(wire.min_positional, positional)
        wire.max_positional = max(wire.max_positional, positional + len(tail))
        for idx, param in enumerate(tail, positional):
            # Same slot must mean the same thing at every call site.
            prev = wire.tail_names.setdefault(idx, param)
            assert prev == param, f"{name}: slot {idx} is both {prev!r} and {param!r}"
    return wire


def _client_wire() -> dict:
    out: dict = {}
    for name, fn in _class_methods(_parse(_CLIENT), _CLIENT_CLASS).items():
        if name.startswith("_"):
            continue
        wire = _wire_of(name, fn)
        if wire is not None:
            out[name] = wire
    return out


def _addon_public() -> dict:
    return {
        name: fn
        for name, fn in _class_methods(_parse(_ADDON), _ADDON_CLASS).items()
        if not name.startswith("_")
    }


def _check_signature(where: str, name: str, wire: Wire, fn) -> None:
    """The positional arguments the client sends must land on parameters."""
    params, vararg = _positional(fn)
    required = len(params) - _defaults(fn)
    if vararg:
        # Overflow lands in *args; the slot names past `required` are
        # unknowable, so the tail assertions below cannot apply.
        assert wire.max_positional >= required, (
            f"{where}.{name} requires {required} positional args, "
            f"but the client sends at most {wire.max_positional}"
        )
    else:
        assert wire.max_positional <= len(params), (
            f"{where}.{name} takes {len(params)} positional args "
            f"({params}), but the client can send {wire.max_positional} — "
            "XML-RPC is positional, so the extra one is a TypeError fault"
        )
        assert wire.min_positional >= required, (
            f"{where}.{name} requires {required} positional args, "
            f"but the client can send as few as {wire.min_positional}"
        )
        for idx, param in wire.tail_names.items():
            assert idx < len(params), (
                f"{where}.{name} has no parameter at index {idx} for the "
                f"appended {param!r} slot ({params})"
            )
            assert params[idx] == param, (
                f"{where}.{name}: the client appends {param!r} at index {idx} "
                f"but that is {params[idx]!r} — the tail is positional, so a "
                "swapped screenshot/doc_name pair binds silently to the wrong slot"
            )
    # XML-RPC invokes handler(*params): a required keyword-only parameter could
    # never bind, so the endpoint would fault on every call.
    required_kwonly = [
        a.arg for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True) if d is None
    ]
    assert not required_kwonly, (
        f"{where}.{name}: XML-RPC passes args positionally, so required "
        f"keyword-only params {required_kwonly} could never bind"
    )


def test_every_public_client_method_is_accounted_for():
    """A public client method must either put a literal RPC name on the wire or
    sit on the explicit exemption list. Anything else means the extraction lost
    it, and with it every check below — the contract silently narrows to
    whatever the extractor happens to see."""
    public = {
        name for name in _class_methods(_parse(_CLIENT), _CLIENT_CLASS) if not name.startswith("_")
    }
    wire = set(_client_wire())
    unaccounted = public - wire - _NON_WIRE_METHODS
    assert not unaccounted, (
        f"public client methods with no literal wire call and no exemption: "
        f"{sorted(unaccounted)} — add the call or extend _NON_WIRE_METHODS "
        "with a reason"
    )
    stale = _NON_WIRE_METHODS & wire
    assert not stale, f"exempted methods that do make wire calls: {sorted(stale)}"


def test_client_and_addon_expose_the_same_wire_methods():
    """Either direction of drift is a bug: a rename faults, and an addon
    endpoint nothing calls is dead weight that no test covers."""
    client, addon = _client_wire(), _addon_public()
    assert set(client) == set(addon), (
        f"client-only: {sorted(set(client) - set(addon))}; "
        f"addon-only: {sorted(set(addon) - set(client))}"
    )
    for name, wire in client.items():
        assert wire.rpc_name == name, (
            f"{name}() puts {wire.rpc_name!r} on the wire — a copy-paste that "
            "calls someone else's endpoint"
        )


def test_addon_signatures_accept_what_the_client_sends():
    addon = _addon_public()
    for name, wire in _client_wire().items():
        assert name in addon, f"the addon has no {name!r} endpoint — the equality test names it too"
        _check_signature("FreeCADRPC", name, wire, addon[name])


def test_fake_connection_implements_every_wire_method():
    """A missing fake method turns the path into an AttributeError, so a test
    that needs it either bolts one on with raise=False (drifting further from
    the real client) or never exercises the path at all."""
    missing = sorted(set(_client_wire()) - set(_class_methods(_parse(_CONFTEST), _FAKE_CLASS)))
    assert not missing, f"FakeFreeCADConnection is missing: {missing}"


def test_fake_mirrors_the_client_method_signatures():
    """The fake stands in for FreeCADConnection, so its contract is the client
    method's PYTHON surface, not the wire shape: core.py calls
    ``freecad.create_document(name, screenshot=...)``, and the fake must accept
    every call the client method accepts. Same positional parameters in the
    same order (positional calls bind by position), at least as many defaults
    (a caller may omit everything the client lets it omit), and every
    keyword-only name. The WIRE arity of the real connection is checked against
    the addon; checking the fake against the wire instead would pass for the
    wrong reason whenever the two shapes happen to coincide."""
    client_methods = _class_methods(_parse(_CLIENT), _CLIENT_CLASS)
    fake_methods = _class_methods(_parse(_CONFTEST), _FAKE_CLASS)
    for name in _client_wire():
        c_fn = client_methods[name]
        c_params, _ = _positional(c_fn)
        c_defaults = _defaults(c_fn)
        c_kwonly = [a.arg for a in c_fn.args.kwonlyargs]
        fake = fake_methods[name]
        f_params, _ = _positional(fake)
        f_kwonly = [a.arg for a in fake.args.kwonlyargs]
        assert f_params == c_params, (
            f"FakeFreeCADConnection.{name}{f_params} does not mirror the client "
            f"method {name}{c_params} — positional calls would bind to the "
            "wrong slot"
        )
        assert _defaults(fake) >= c_defaults, (
            f"FakeFreeCADConnection.{name} has {_defaults(fake)} defaults, the "
            f"client method has {c_defaults} — a call omitting a defaulted "
            "argument would raise TypeError"
        )
        unnamed = sorted((set(c_kwonly) - set(f_params)) - set(f_kwonly))
        assert not unnamed, f"FakeFreeCADConnection.{name} lacks keyword-only params {unnamed}"
        # A keyword the caller may OMIT must not be required on the fake.
        c_optional = {
            a.arg
            for a, d in zip(c_fn.args.kwonlyargs, c_fn.args.kw_defaults, strict=True)
            if d is not None
        }
        f_required = {
            a.arg
            for a, d in zip(fake.args.kwonlyargs, fake.args.kw_defaults, strict=True)
            if d is None
        }
        too_strict = sorted(c_optional & f_required)
        assert not too_strict, (
            f"FakeFreeCADConnection.{name} makes optional keywords {too_strict} "
            "required — an omitted call would raise TypeError"
        )


def _connection_receivers(fn) -> set:
    """Parameter names annotated as the connection, within one function.

    The name is whatever the author chose (``freecad`` by convention, ``conn``
    in operations/assembly.py); the ANNOTATION is the contract, so the receiver
    set is read off it instead of a hardcoded list that would silently narrow.
    """
    out = set()
    for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
        if a.annotation is not None and "FreeCADConnection" in ast.unparse(a.annotation):
            out.add(a.arg)
    return out


def _connection_attr_reads(node: ast.Attribute, receivers: set) -> str | None:
    """The attribute name when ``node`` is a member access on the connection.

    Covers the shapes the codebase uses: an annotated connection parameter
    (operations), the ``state.freecad_connection`` chain (server), and
    ``get_freecad_connection()`` inline calls (server). The bare name
    ``freecad`` stays recognized unannotated because it is the documented
    convention of the operation layer.
    """
    v = node.value
    if isinstance(v, ast.Name) and (v.id in receivers or v.id == "freecad"):
        return node.attr
    if isinstance(v, ast.Attribute) and v.attr == "freecad_connection":
        return node.attr
    if (
        isinstance(v, ast.Call)
        and isinstance(v.func, ast.Name)
        and v.func.id == "get_freecad_connection"
    ):
        return node.attr
    return None


def test_operations_only_call_real_connection_methods():
    """A typo'd connection method is an AttributeError against the real client.
    It survives the suite whenever the fake happens to carry the name the real
    connection does not (or vice versa), so the names are checked against the
    class that owns them — including the server layer's chained access, which
    is the wiring this file exists to guard."""
    known = set(_client_wire()) | _NON_WIRE_METHODS
    unknown: dict = {}
    for path in sorted(list((_SRC / "operations").glob("*.py")) + list(_SRC.glob("*.py"))):
        tree = _parse(path)
        # Module level: only the chain shapes mean anything (a bare name there
        # would be a global, and there are none).
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                attr = _connection_attr_reads(node, set())
                if attr is not None and attr not in known:
                    unknown.setdefault(attr, path.name)
        # Per function: annotated connection parameters.
        for fn in (
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            receivers = _connection_receivers(fn)
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute):
                    attr = _connection_attr_reads(node, receivers)
                    if attr is not None and attr not in known:
                        unknown.setdefault(attr, path.name)
    assert not unknown, f"operations reach for unknown connection methods: {unknown}"
