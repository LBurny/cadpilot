"""Object create/edit/delete primitives.

Shared by the RPC handlers and the step-journal engine, so both paths mutate
objects the same way. Each function returns a result dict (create) or ``True``
(edit/delete) on success and an error string on failure.
"""

import FreeCAD

from rpc_server import tip_policy
from rpc_server.property_mapper import Object, set_object_property


def create_object_gui(doc_name: str, obj: Object, recompute: bool = True):
    """Create an object in ``doc_name`` according to ``obj.type``.

    Returns the created object's actual ``Name`` on success (FreeCAD
    sanitises and de-duplicates requested names — ``Box`` may come back as
    ``Box001`` — and every later get_object/edit_object call needs the real
    one), or an error string on failure.

    When ``recompute`` is False, the caller is responsible for calling
    ``doc.recompute()`` after all objects are created (used by batch
    operations to avoid N recomputes for N objects).
    """
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
        return f"Document '{doc_name}' not found.\n"
    try:
        res = doc.addObject(obj.type, obj.name)
        set_object_property(doc, res, obj.properties)
        if recompute:
            doc.recompute()
        FreeCAD.Console.PrintMessage(f"{res.TypeId} '{res.Name}' added to '{doc.Name}' via RPC.\n")
        return {"success": True, "object_name": res.Name}
    except Exception as e:
        return str(e)


def edit_object_gui(doc_name: str, obj: Object):
    """Assign ``obj.properties`` to an existing object. True on success."""
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
        return f"Document '{doc_name}' not found.\n"

    obj_ins = doc.getObject(obj.name)
    if not obj_ins:
        FreeCAD.Console.PrintError(f"Object '{obj.name}' not found in document '{doc_name}'.\n")
        return f"Object '{obj.name}' not found in document '{doc_name}'.\n"

    try:
        has_expressions = any(
            isinstance(v, str) and v.startswith("=") for v in obj.properties.values()
        )
        set_object_property(doc, obj_ins, obj.properties)
        doc.recompute()
        if has_expressions and "Invalid" in [str(s) for s in obj_ins.State]:
            return (
                f"Expression(s) on '{obj.name}' failed to evaluate "
                "(check expression syntax and referenced cells/objects)."
            )
        FreeCAD.Console.PrintMessage(f"Object '{obj.name}' updated via RPC.\n")
        return True
    except Exception as e:
        return str(e)


# Containers reference their members for grouping and placement, not because
# they BUILD on them — FreeCAD lists the owning Body in every feature's InList,
# so a naive dependency check would refuse every delete inside a Body.
_CONTAINER_TYPES = frozenset(
    {
        "PartDesign::Body",
        "App::Part",
        "App::Origin",
        "App::DocumentObjectGroup",
        "App::GeoFeatureGroup",
    }
)


def _dependents(obj) -> list[str]:
    """Names of the objects (transitively) built ON ``obj``, containers excluded.

    Deleting a PartDesign feature that a later feature consumes does NOT delete
    the consumer: it is left with ``BaseFeature = None``, and a SUBTRACTIVE
    feature then turns ADDITIVE. Measured live on 1.1.4: a plate-with-hole
    (6283 mm^3) became the pocket's own cylinder (1571 mm^3) while the Body
    still reported ['Up-to-date'] — silently wrong geometry, reported as a
    successful delete.
    """
    seen: list[str] = []
    stack = [o for o in getattr(obj, "InList", []) if o is not None]
    while stack:
        dep = stack.pop()
        if dep.Name == obj.Name or dep.Name in seen or dep.TypeId in _CONTAINER_TYPES:
            continue
        seen.append(dep.Name)
        stack.extend(x for x in getattr(dep, "InList", []) if x is not None)
    return sorted(seen)


def repair_body_tips(doc) -> bool:
    """Re-point a PartDesign Body's Tip after a delete removed it.

    ``removeObject`` leaves ``Body.Tip`` dangling when the removed object WAS
    the tip: FreeCAD then reports the Body as ['Touched', 'Invalid'], reading
    ``Body.Shape`` raises "shape is invalid", and every later feature on that
    Body fails until the tip is set by hand. FreeCAD's GUI does this repair
    itself; the RPC path has to.

    The new tip is the LAST surviving member that can produce a solid
    (``tip_policy``'s whitelist), which is the end of the chain in feature
    order. A body left with only sketches keeps ``Tip = None`` on purpose:
    pinning the tip to a sketch would stop the next feature from claiming it
    (tip_policy only advances a tip the feature depends on) and the body would
    silently show nothing.
    """
    repaired = False
    for body in doc.Objects:
        if body.TypeId != "PartDesign::Body" or getattr(body, "Tip", None) is not None:
            continue
        members = [o for o in getattr(body, "Group", []) if tip_policy.advances_tip(o.TypeId)]
        if not members:
            continue  # an emptied Body has no tip to restore
        new_tip = members[-1]
        try:
            body.Tip = new_tip
            repaired = True
            FreeCAD.Console.PrintMessage(
                f"CADPilot: {body.Name}.Tip re-pointed to '{new_tip.Name}' after the delete.\n"
            )
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"CADPilot: could not restore {body.Name}.Tip: {e}\n")
    return repaired


def delete_object_gui(doc_name: str, obj_name: str):
    """Remove an object and recompute. True on success.

    Refuses when other objects are built on it (see :func:`_dependents`):
    FreeCAD would leave the consumers behind with their base link cleared,
    turning a subtractive feature additive and a body silently wrong.
    """
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
        return f"Document '{doc_name}' not found.\n"

    try:
        obj = doc.getObject(obj_name)
        if obj is None:
            return f"Object '{obj_name}' not found in document '{doc_name}'."
        dependents = _dependents(obj)
        if dependents:
            return (
                f"Cannot delete '{obj_name}': {len(dependents)} object(s) are built on it "
                f"({', '.join(repr(d) for d in dependents)}). Removing it would clear their "
                "base link — a PartDesign feature loses its BaseFeature and a SUBTRACTIVE "
                "feature turns ADDITIVE, so the model would change silently. Delete the "
                "dependents first (in reverse order), then this object."
            )
        doc.removeObject(obj_name)
        doc.recompute()
        # A body whose Tip was just deleted is left invalid; the delete is only
        # successful if the document is still usable afterwards.
        if repair_body_tips(doc):
            doc.recompute()
        FreeCAD.Console.PrintMessage(f"Object '{obj_name}' deleted via RPC.\n")
        return True
    except Exception as e:
        return str(e)
