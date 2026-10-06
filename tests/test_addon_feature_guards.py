"""Addon feature-op guards found by complex-model stress testing.

Three failure modes a real PartDesign model exposed, all of which used to be
SILENT (the tool reported success while the geometry was wrong):

* a sketch attached to a face by NAME lands on the wrong plane after the next
  feature re-derives face names — its pocket then cuts air;
* a PartDesign ``pattern`` arrayed the whole body (Draft's array copies the
  base Shape) or repeated nothing, so "6 holes" came out as 6 overlapping
  plates or as 1 hole;
* a cut that removed nothing reported plain success.

Plus, found by re-verifying a user bug report on 1.1.4:

* a PartDesign *transform* feature was created correctly but never became its
  Body's Tip, so the body kept showing the pre-pattern result while the tool
  reported success (``body.Tip`` is now pushed explicitly — see tip_policy);
* a fillet/chamfer on a base inside a Body was built as a document-root
  ``Part::Fillet``, which is not part of the Body at all.

The addon cannot be imported without FreeCAD, so these parse the source.
"""

import ast
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "addon" / "CADPilot" / "rpc_server"
_FEATURE = ast.parse((_ADDON / "feature_ops.py").read_text(encoding="utf-8"))
_SKETCHER = ast.parse((_ADDON / "sketcher_ops.py").read_text(encoding="utf-8"))


def _func(tree, name):
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _strings(node) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_face_attachment_accepts_a_direction_selector():
    body = _func(_SKETCHER, "_attach_sketch")
    assert "_resolve_semantic_face" in _strings(body) or any(
        isinstance(n, ast.Name) and n.id == "_resolve_semantic_face" for n in ast.walk(body)
    ), "plane.face must accept a direction token"


def test_semantic_face_resolution_uses_normals_and_extremity():
    # The direction words live in the module-level _FACE_WORDS table.
    words = _strings(_SKETCHER)
    for token in ("+X", "-X", "+Y", "-Y", "+Z", "-Z", "top", "bottom", "right", "left"):
        assert token in words, f"{token} is not handled"
    body = _func(_SKETCHER, "_resolve_semantic_face")
    # Resolution compares the face normal against the wanted axis...
    assert any(n.attr == "normalAt" for n in ast.walk(body) if isinstance(n, ast.Attribute))
    # ...and breaks ties by how far the face sits along it.
    assert any(n.attr == "CenterOfMass" for n in ast.walk(body) if isinstance(n, ast.Attribute))


def test_partdesign_pattern_uses_a_partdesign_pattern_not_a_draft_array():
    body = _func(_FEATURE, "_build_pd_pattern")
    assert "PartDesign::PolarPattern" in _strings(body)
    assert "PartDesign::LinearPattern" in _strings(body)
    # Originals must point at the FEATURE, not at the whole body's shape.
    assert any(isinstance(n, ast.Attribute) and n.attr == "Originals" for n in ast.walk(body))


def test_partdesign_pattern_refuses_to_return_a_no_op():
    """Silently returning one hole where six were asked for is worse than an
    error — the builder must compare volumes and raise."""
    body = _func(_FEATURE, "_build_pd_pattern")
    raises = [n for n in ast.walk(body) if isinstance(n, ast.Raise)]
    assert raises, "a no-effect pattern must raise"
    assert any("had no effect" in s for s in _strings(body))


def test_cut_no_op_is_detected_against_the_predecessor():
    body = _func(_FEATURE, "_cut_removed_nothing")
    assert "BaseFeature" in _strings(body), "the baseline is the feature's predecessor"
    assert any(isinstance(n, ast.Attribute) and n.attr == "Volume" for n in ast.walk(body))
    describe = _func(_FEATURE, "describe_feature")
    assert "_CUT_TYPES" in _strings(describe) or any(
        isinstance(n, ast.Name) and n.id == "_CUT_TYPES" for n in ast.walk(describe)
    ), "describe_feature must consult the cut types"
    assert "warnings" in _strings(describe)
    assert "pocket" in _strings(_FEATURE), "_CUT_TYPES must cover pocket"


def test_move_accepts_list_vectors():
    """The whole API takes plain coordinate lists; move's dict-only form used
    to crash with "'list' object has no attribute 'get'" on the natural input."""
    body = _func(_FEATURE, "_build_move")
    assert any(isinstance(n, ast.Name) and n.id == "_vec3" for n in ast.walk(body)), (
        "translate/rotate/placement vectors must go through a list-tolerant parser"
    )


def test_vec3_helper_parses_lists_and_dicts():
    helper = _func(_FEATURE, "_vec3")
    seg = ast.unparse(helper)
    assert "list" in seg and "tuple" in seg, "lists must be accepted"
    assert "dict" in seg, "dicts must stay accepted"
    assert "raise ValueError" in seg, "malformed vectors must raise a clear error"


def test_datum_plane_accepts_a_direction_face_token():
    """datum_plane's plane.face used to stuff the token straight into the
    attachment — '+Z' is no subelement, so it recomputed Invalid. It must use
    the same direction resolution as a sketch's plane.face."""
    body = _func(_FEATURE, "_build_datum_plane")
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "_resolve_semantic_face" for n in ast.walk(body)
    ), "datum_plane plane.face must resolve direction tokens"
    assert any(isinstance(n, ast.Attribute) and n.attr == "_FACE_WORDS" for n in ast.walk(body)), (
        "direction words must be recognized"
    )


def test_recompute_failure_reports_status_string():
    """'failed to recompute (check parameters/geometry)' alone sends the caller
    hunting blind; FreeCAD's own reason carries the actual cause. 1.1.4 exposes
    it as the METHOD getStatusString() — the old property read
    (getattr(feat, "StatusString", "")) always returned nothing, so a
    "Revolve axis intersects the sketch" reached the caller as a bare
    "check parameters/geometry" (live-verified through the MCP tool)."""
    body = _func(_FEATURE, "create_feature_gui")
    assert "_status_string" in _names(body), "the failure detail comes from _status_string"
    helper = _func(_FEATURE, "_status_string")
    assert "getStatusString" in {
        n.attr for n in ast.walk(helper) if isinstance(n, ast.Attribute)
    }, "the method form is the one FreeCAD 1.1.4 actually provides"
    assert any(
        isinstance(n, ast.Constant) and n.value == "StatusString" for n in ast.walk(helper)
    ), "keep the property form as a fallback for other versions"


def _names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def test_predecessor_shapes_are_settled_before_building():
    """FreeCAD builds a PartDesign shape LAZILY and a feature whose predecessor
    is unbuilt computes to a Touched/Null shape with no error. Live on 1.1.4:
    after reopening a saved model an identical pocket re-run (the same params
    that had built fine) produced no shape at all, and reading the SKETCH's
    Shape first made the same build return the analytic result exactly
    (58800 -> 57207.2 mm^3, one through hole). A recompute is not enough: the
    READ is what performs the build."""
    gui = _func(_FEATURE, "create_feature_gui")
    assert "_settle_shape_caches" in _names(gui), (
        "the referenced objects must be settled before the builder runs"
    )
    settle = _func(_FEATURE, "_settle_shape_caches")
    assert "Shape" in _strings(settle) or any(
        isinstance(n, ast.Attribute) and n.attr == "Shape" for n in ast.walk(settle)
    ), "settling means READING the Shape"
    assert "Tip" in _strings(settle), "a Body member's baseline is the Body's Tip"


def test_shape_volume_probe_sits_inside_the_guard():
    """The shape read was guarded but the volume probe was not, and a lazily
    built PartDesign shape raises FreeCAD's "shape is invalid" on the FIRST
    access while reading fine on the second — so a fillet OCC could not build
    reported those two words as the ENTIRE error: no object name, no cause, no
    workaround (live on 1.1.4; it cost a full investigation). The read now lives
    in one place, guarded and retried once."""
    reader = _func(_FEATURE, "_read_shape_state")
    assert "Volume" in _strings(reader), "the volume probe belongs inside the guard"
    assert "recompute" in {n.attr for n in ast.walk(reader) if isinstance(n, ast.Attribute)}, (
        "and the read must be retried once"
    )
    tries = [n for n in ast.walk(reader) if isinstance(n, ast.Try)]
    assert any("Volume" in _strings(stmt) for node in tries for stmt in node.body), (
        "the volume probe must be inside the retried try"
    )
    gui = _func(_FEATURE, "create_feature_gui")
    assert "_read_shape_state" in _names(gui), "and the builder must use it"


def test_failed_multi_edge_dress_up_names_the_edge():
    """A multi-edge fillet OCC refuses reported a bare "shape is invalid" while
    every edge was fine on its own (live on 1.1.4: the six bore rims of a
    patterned plate at 0.5 mm — single rims and the top rims as a pair worked,
    only the bottom rims fail as a set). "Invalid" is not actionable; "split the
    edges across several fillet calls" is, so the op retries edge by edge and
    says what it found."""
    build = _func(_FEATURE, "_build_fillet_chamfer")
    assert "_remember_dress_context" in _names(build), "the builder must leave the context"
    detail = _func(_FEATURE, "_dress_failure_detail")
    assert "probe" in _strings(detail) and "probe" in _names(detail), (
        "the diagnosis must re-point the feature at one edge at a time"
    )
    assert "_dress_shape_usable" in _names(build), "each retry is judged by its own shape"
    gui = _func(_FEATURE, "create_feature_gui")
    assert "_dress_retry_repair" in _names(gui), (
        "a Null shape from a stale recompute must be retried before failing the caller's step"
    )
    retry = _func(_FEATURE, "_dress_retry_repair")
    assert "probe" in _strings(retry) and "names" in _names(retry), (
        "the retry re-applies the SAME edge set"
    )
    gui = _func(_FEATURE, "create_feature_gui")
    refs = [n for n in ast.walk(gui) if isinstance(n, ast.Name) and n.id == "_dress_failure_detail"]
    assert len(refs) >= 2, "both failure branches (Invalid state and unreadable shape) must name it"


def test_polar_pattern_honours_center():
    """``center`` was read only by the Draft (Part-level) path; a PartDesign
    polar pattern always turned about the body's origin axis. Live on 1.1.4: a
    bolt circle drawn around a face centre (the hole at (60,35), plate 70x70x8)
    came out as ONE clean hole plus one half-hole clipped by the plate edge,
    with the tool reporting success — the 6-hole result is 36787.26 mm^3, the
    clipped one 38691.44 mm^3. The centre is expressed with a datum line, the
    same reference the GUI uses; a free Placement is what positions it
    (MapMode "Translate" over the origin line does NOT move it)."""
    builder = _func(_FEATURE, "_build_pd_pattern")
    assert "_centered_axis_line" in _names(builder), (
        "the PartDesign polar branch must use the centred axis line when center is given"
    )
    assert "center" in {
        c.value
        for c in ast.walk(builder)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
    }, "and it must read the spec's center"
    helper = _func(_FEATURE, "_centered_axis_line")
    assert any(isinstance(n, ast.Attribute) and n.attr == "Placement" for n in ast.walk(helper)), (
        "the datum line is positioned by its Placement"
    )
    assert "Deactivated" in {
        c.value
        for c in ast.walk(helper)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
    }, "attaching it (any other MapMode) leaves it on the origin"


def test_new_feature_is_pushed_as_its_body_tip():
    """Measured on 1.1.4: a polar pattern built from a flange pocket was correct
    (its own Shape was the 6-hole result) yet Body.Tip stayed on the pocket, so
    the body kept ONE hole while the op returned success. Trusting FreeCAD to
    advance the tip is not enough for transform features."""
    helper = _func(_FEATURE, "_advance_body_tip")
    assert "tip_policy" in _names(helper), "the tip decision belongs in tip_policy"
    assert any(isinstance(n, ast.Attribute) and n.attr == "Tip" for n in ast.walk(helper)), (
        "the helper must assign body.Tip"
    )
    assert "advances_tip" in {n.attr for n in ast.walk(helper) if isinstance(n, ast.Attribute)}, (
        "consult the whitelist before assigning a tip"
    )
    assert "should_advance" in {n.attr for n in ast.walk(helper) if isinstance(n, ast.Attribute)}, (
        "only a successor of the current tip may claim it, or a later feature is hidden"
    )
    # And the single creation entry point must actually call it.
    assert "_advance_body_tip" in _names(_func(_FEATURE, "create_feature_gui")), (
        "create_feature_gui must push the tip after building the feature"
    )


def test_tip_advance_tolerates_a_body_less_feature():
    """A Part-level base (or a document-root build) has no body: the helper must
    return quietly instead of raising — the guard already ran the whole op inside
    a transaction and an exception would discard a valid feature."""
    helper = _func(_FEATURE, "_advance_body_tip")
    assert any(isinstance(n, ast.Return) for n in ast.walk(helper))
    assert "None" in _strings(helper) or any(
        isinstance(n, ast.Constant) and n.value is None for n in ast.walk(helper)
    ), "a missing body must be a plain early return"


def test_dressup_inside_a_body_builds_the_partdesign_feature():
    """#2: Part::Fillet is a document-root object — not in Body.Group, it does
    not follow the Body's Placement, and Body.Tip = <it> is accepted silently
    while leaving the Body ['Touched', 'Invalid']."""
    body = _func(_FEATURE, "_build_fillet_chamfer")
    assert "tip_policy" in _names(body), "the Part-vs-PartDesign choice belongs in tip_policy"
    assert "newObject" in {n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)}, (
        "the PartDesign dress-up must be created inside the Body"
    )
    assert "dress_type" in {n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)}
    assert "dress_size_property" in {
        n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)
    }, "PartDesign::Fillet takes a scalar Radius, not the Part-level Edges tuples"


def test_dressup_named_on_a_body_aims_at_the_body_tip():
    """Naming the BODY is how a caller naturally asks for "round this part's
    rim", but a Body has no parent Body, so the Part-vs-PartDesign choice fell
    to the ROOT branch: a Part::Fillet outside Body.Group, Body.Tip still on the
    last feature, and the Body's displayed shape unchanged while the reply said
    "created successfully" (live: a 2 mm fillet on a bore rim left the Body at
    17591.5 mm^3, the filleted 17545.9 sitting beside it as a second solid).
    A Body base must resolve to its Tip and be dressed INSIDE the body."""
    body = _func(_FEATURE, "_build_fillet_chamfer")
    assert "PartDesign::Body" in ast.unparse(body), "the Body-as-base case must be detected"
    names = {n.id for n in ast.walk(body) if isinstance(n, ast.Name)}
    assert "named_body" in names and "target" in names
    # getattr(base, "Tip", None) — the Tip probe is a string constant
    assert "Tip" in _strings(body), "the Body's Tip is what a dress-up can be built on"
    # The edges must be resolved against the dress-up TARGET (the Tip), not the
    # named base: resolving first and redirecting after would read edge names
    # off a different shape.
    resolve = next(
        c
        for c in ast.walk(body)
        if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_resolve_elements"
    )
    assert getattr(resolve.args[0], "id", "") == "target"
    # ...and the reply must say which feature was dressed.
    assert "dressed_object" in ast.unparse(_func(_FEATURE, "describe_feature"))


def test_dressup_declares_its_report_global_unconditionally():
    """The bare-base branch writes _LAST_FEATURE_INFO (hidden_base), but the
    only `global` declaration used to sit INSIDE the named_body branch: correct
    only through Python's whole-function scoping, and dead the moment that
    branch is removed or reordered (the trap _build_color was caught by). The
    declaration must be unconditional at function top."""
    body = _func(_FEATURE, "_build_fillet_chamfer")
    top_level_globals = [
        n for n in body.body if isinstance(n, ast.Global) and "_LAST_FEATURE_INFO" in n.names
    ]
    assert top_level_globals, (
        "_LAST_FEATURE_INFO must be declared with a function-top `global` in"
        " _build_fillet_chamfer (the bare-base write depends on it)"
    )


def test_tip_advance_refreshes_the_body_appearance():
    """A new PartDesign feature rebuilds the Body's display node with FreeCAD's
    DEFAULT material, so an earlier colour visually VANISHES while every stored
    value still reports it — the worst kind of silent state (live, pixel-sampled:
    a red body rendered (185,45,45), a fillet on it made the view draw
    (110,116,120), and re-applying the appearance brought back (184,45,44)).
    create_feature_gui must refresh when the new feature became the Body's Tip."""
    text = ast.unparse(_func(_FEATURE, "create_feature_gui"))
    assert "_refresh_appearance" in text, "a tip advance must re-apply the appearance"
    assert "Tip" in text
    helper = ast.unparse(_func(_FEATURE, "_refresh_appearance"))
    # A FRESH Material is the point: FreeCAD treats an identical assignment as a
    # no-op, which is exactly why a same-value write does not repaint.
    assert "_fresh_material" in helper
    # ...and a genuine per-face design must survive untouched.
    assert "_uniform_material" in helper


def test_through_all_is_freecads_parametric_through_all():
    """`through_all` used to be IGNORED by the pad/pocket builder, which always
    set a numeric Length (default 10). A "through" hole therefore only went
    through while the body was thinner than 10 mm and silently grew a floor the
    moment a dimension changed: live on a flange, exact at 8 mm, +339 mm^3 of
    uncut material at 12 mm and +1696 mm^3 with 6 extra bottom faces at 20 mm —
    a blind hole, with the volume formula the only clue. The builder must set
    PartDesign's own Type, which stays through at any thickness."""
    body = _func(_FEATURE, "_build_padlike")
    assert "through_all" in ast.unparse(body)
    assert "ThroughAll" in _strings(body), "must use FreeCAD's parametric through-all"
    # The numeric-length path must become the ELSE branch, not the only branch.
    assert "_set_length" in ast.unparse(body)


def test_invalid_shape_error_names_the_object_and_reason():
    """Reported live: 'shape is invalid' was raised for a self-intersecting
    meridian, a broken reference and a corrupted dependency graph alike — three
    different causes, one opaque message. It must name the object and carry
    FreeCAD's own reason."""
    helper = _func(_FEATURE, "_invalid_shape_error")
    text = ast.unparse(helper)
    assert "produced an invalid Shape" in text
    assert ".Name" in text, "the object name is what makes the error actionable"
    assert "_status_string" in _names(helper), "FreeCAD's reason must be carried through"
    assert "_invalid_shape_error" in _names(_func(_FEATURE, "create_feature_gui"))


def test_pocket_direction_is_decided_from_the_geometry():
    """FreeCAD's OWN default pocket direction points away from the solid when the
    profile lies on the body's start plane, and it says nothing: a fresh
    PartDesign::Pocket with FreeCAD's untouched defaults (profile on the XY
    plane under a pad spanning z 0..8, Length 5) removed exactly 0 mm^3, while
    Reversed=True removed the analytic pi*r^2*length (measured natively on
    1.1.4, reproduced through CADPilot: 10053.0965 -> 9424.7780 mm^3). The GUI
    user ticks "Reversed" because the dialog shows the result; a caller over MCP
    got a silent no-op cut, the one outcome a `pocket` can never mean.

    So the builder must probe both directions: forward cuts nothing and reversed
    removes material means reversed wins, and the reply says so. An explicit
    `reversed` is the caller's decision and midplane is symmetric, so neither is
    overridden; when NEITHER direction cuts (a profile outside the solid, a bad
    attachment) the feature is left as asked for and the existing no-material
    warning explains it."""
    helper = ast.unparse(_func(_FEATURE, "_auto_detect_cut_direction"))
    assert "PartDesign::Pocket" in helper, "only a pocket has a direction to decide"
    assert "midplane" in helper, "midplane is symmetric, so there is nothing to decide"
    assert "reversed" in helper, "an explicit reversed must never be overridden"
    assert "feat.Reversed = True" in helper and "feat.Reversed = False" in helper, (
        "the probe must try BOTH directions, and put the caller's choice back"
    )
    # The decision needs a volume, so it must be taken against the material that
    # existed BEFORE the feature joined the body.
    assert "base_vol" in helper
    # ...and the builder has to call it after the direction properties are set,
    # with a volume read before the feature exists.
    built = ast.unparse(_func(_FEATURE, "_build_padlike"))
    assert "_auto_detect_cut_direction" in built
    assert built.index("feat.Reversed") < built.index("_auto_detect_cut_direction")
    assert "_body_material_volume" in built
    assert built.index("_body_material_volume") < built.index("body.newObject"), (
        "the material volume must be read before the feature exists"
    )


def test_the_auto_decided_direction_reaches_the_caller():
    """A geometry fix the caller cannot see is a silent inconsistency between the
    reply and the model: the auto-decided direction must be reported as a
    warning, and it must not leak into the NEXT op's reply."""
    reply = ast.unparse(_func(_FEATURE, "describe_feature_reply"))
    assert "_take_auto_direction_note" in reply, "the note must reach the reply"
    build = ast.unparse(_func(_FEATURE, "create_feature_gui"))
    # ast.unparse renders the empty string with single quotes.
    assert "_AUTO_DIRECTION_NOTE = ''" in build, (
        "the note is per-build: a replay also builds features and must not leave"
        " a note for the next caller's reply"
    )
    setter = ast.unparse(_func(_FEATURE, "_auto_detect_cut_direction"))
    assert "_AUTO_DIRECTION_NOTE" in setter
    # One epsilon, shared with the no-material warning, so the two cannot
    # disagree about whether a cut removed anything.
    assert "_CUT_EPS" in setter
    assert "_CUT_EPS" in ast.unparse(_func(_FEATURE, "_cut_removed_nothing"))


def test_an_attachment_never_names_a_body():
    """Attaching to a PartDesign **Body**'s face is a dependency cycle waiting
    for the next feature: the Body's Shape IS its Tip's result, so the moment a
    feature built on that profile joins the body the graph closes
    (Body.Shape -> Pocket -> profile sketch -> Body.Shape). FreeCAD neither
    fails nor warns there: the feature stays Touched forever and reading its
    Shape raises the bare "shape is invalid", which reads like a broken profile.

    Live-caught in a plan run (document Stress2): a bolt-hole sketch created with
    plane={"face": ["Body", "+Z"]} made every pocket on it fail with "produced an
    invalid Shape", and the failure cascaded into the polar pattern. Re-pointing
    the SAME sketch at the Pad feature made the identical pocket compute exactly
    57207.213 mm^3; through the plan engine the whole sequence then produced the
    analytic 49243.275."""
    helper = ast.unparse(_func(_SKETCHER, "attachment_ref"))
    assert "PartDesign::Body" in helper, "only a Body is the cycle risk"
    assert "Tip" in helper, "the Tip owns the same faces and breaks the cycle"
    assert "BaseFeature" in helper, "a body with no tip can still have material"
    # Both attachment sites must route through it, and the sketch one must do it
    # BEFORE resolving the face (the resolution reads that object's Shape).
    attach = ast.unparse(_func(_SKETCHER, "_attach_sketch"))
    assert "attachment_ref" in attach
    assert attach.index("attachment_ref") < attach.index("_resolve_semantic_face")
    assert "attachment_ref" in ast.unparse(_func(_FEATURE, "_build_datum_plane"))


def test_direction_probe_ignores_an_unreadable_shape():
    """_read_shape_state reports volume 0.0 on a failed read, which this probe
    would take for "removed EVERYTHING" and flip the cut of a feature that never
    computed at all. The error channel must decide before any volume compare."""
    text = ast.unparse(_func(_FEATURE, "_auto_detect_cut_direction"))
    assert "forward_error" in text and "reversed_error" in text
    assert text.index("forward_error") < text.index("forward_vol < base_vol"), (
        "the unreadable-shape bail-out must come first"
    )
