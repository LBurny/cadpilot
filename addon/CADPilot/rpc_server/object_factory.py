"""Object create/edit/delete primitives.

Shared by the RPC handlers and the step-journal engine, so both paths mutate
objects the same way. Each function returns a result dict (create) or ``True``
(edit/delete) on success and an error string on failure.
"""

import FreeCAD

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


def delete_object_gui(doc_name: str, obj_name: str):
    """Remove an object and recompute. True on success."""
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        FreeCAD.Console.PrintError(f"Document '{doc_name}' not found.\n")
        return f"Document '{doc_name}' not found.\n"

    try:
        doc.removeObject(obj_name)
        doc.recompute()
        FreeCAD.Console.PrintMessage(f"Object '{obj_name}' deleted via RPC.\n")
        return True
    except Exception as e:
        return str(e)
