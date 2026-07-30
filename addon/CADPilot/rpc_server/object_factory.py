"""Object creation dispatch for the RPC ``create_object`` handler.

All objects go through ``doc.addObject`` + recursive property assignment.
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
