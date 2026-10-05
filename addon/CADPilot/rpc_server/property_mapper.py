"""Property assignment from JSON-friendly dicts onto FreeCAD document objects."""

from dataclasses import dataclass, field
from typing import Any

import FreeCAD


@dataclass
class Object:
    name: str
    type: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


def _to_shape_color(val: Any) -> tuple[float, float, float, float]:
    """Normalise a color to a 4-float RGBA tuple.

    Accepts RGB triples (alpha defaults to 1.0) and RGBA quads, matching what
    FreeCAD's ``ShapeColor`` accepts.
    """
    if not isinstance(val, (list, tuple)) or len(val) not in (3, 4):
        raise ValueError(f"ShapeColor must be an RGB or RGBA sequence, got {val!r}.")
    r, g, b = (float(val[0]), float(val[1]), float(val[2]))
    a = float(val[3]) if len(val) == 4 else 1.0
    return (r, g, b, a)


def parse_reference_entry(entry: Any) -> tuple[str, Any]:
    """Normalise a single ``References`` entry to ``(object_name, sub_element)``.

    Accepts both the documented dict form
    ``{"object_name": "Box", "face": "Face1"}`` and the legacy
    ``["Box", "Face1"]`` pair form.
    """
    if isinstance(entry, dict):
        ref_name = entry.get("object_name", entry.get("Object"))
        face = entry.get("face", entry.get("Face"))
        if ref_name is None:
            raise ValueError(f"Reference entry {entry!r} is missing an 'object_name' key.")
        return ref_name, face
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return entry[0], entry[1]
    raise ValueError(
        f"Invalid reference entry {entry!r}; expected "
        "{'object_name': ..., 'face': ...} or [object_name, face]."
    )


def resolve_references(doc: FreeCAD.Document, val: Any) -> list[tuple[Any, Any]]:
    """Resolve a ``References`` list into ``(DocumentObject, sub_element)`` tuples."""
    refs = []
    for entry in val:
        ref_name, face = parse_reference_entry(entry)
        ref_obj = doc.getObject(ref_name)
        if ref_obj is None:
            raise ValueError(f"Referenced object '{ref_name}' not found.")
        refs.append((ref_obj, face))
    return refs


def set_spreadsheet_cells(ss, cells: dict) -> None:
    """Write ``{cell: [alias, value]}`` entries onto a Spreadsheet.

    Shared by the ``variables`` feature op and ``edit_object``. A Spreadsheet
    has a property literally named ``cells``, but assigning it through setattr
    fails with "Invalid type" — the contents must be written cell by cell.
    Values: number -> literal, string starting with '=' -> formula, other
    strings -> quoted text.
    """
    if not isinstance(cells, dict) or not cells:
        raise ValueError("cells must be a non-empty dict of {cell: [alias, value]}.")
    for cell, entry in cells.items():
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            raise ValueError(f"cells['{cell}'] must be [alias, value].")
        alias, value = entry
        if isinstance(value, bool):
            raise ValueError(f"cells['{cell}']: bool is not a valid value.")
        if isinstance(value, (int, float)):
            ss.set(cell, repr(value))
        elif isinstance(value, str):
            ss.set(cell, value if value.startswith("=") else f'"{value}"')
        else:
            raise ValueError(f"cells['{cell}']: unsupported value {value!r}.")
        try:
            ss.setAlias(cell, str(alias))
        except Exception as e:
            # FreeCAD's own "Invalid alias" names neither the cell nor the
            # alias, so the caller could not tell which entry was wrong.
            raise ValueError(
                f"cells['{cell}']: alias {alias!r} was rejected ({e}). Aliases must "
                "start with a letter and contain only letters/digits/underscores "
                "(no spaces, no leading digit, not a cell reference like 'A1')."
            ) from None


def set_object_property(
    doc: FreeCAD.Document, obj: FreeCAD.DocumentObject, properties: dict[str, Any]
):
    failures = []
    for prop, val in properties.items():
        try:
            # FIRST, before the PropertiesList branch: a Spreadsheet DOES have a
            # property literally named "cells" (assigned via getContents), and
            # the generic setattr path for it fails with "cells: Invalid type".
            if prop == "cells" and obj.TypeId == "Spreadsheet::Sheet" and isinstance(val, dict):
                set_spreadsheet_cells(obj, val)

            elif prop in obj.PropertiesList:
                # Expression binding: "=Spreadsheet.width * 2" routes to the
                # ExpressionEngine instead of a literal assignment, which is
                # how Spreadsheet-driven parametrics are wired up.
                if isinstance(val, str) and val.startswith("="):
                    obj.setExpression(prop, val[1:])

                elif prop == "Placement" and isinstance(val, dict):
                    if "Base" in val:
                        pos = val["Base"]
                    elif "Position" in val:
                        pos = val["Position"]
                    else:
                        pos = {}
                    rot = val.get("Rotation", {})
                    placement = FreeCAD.Placement(
                        FreeCAD.Vector(
                            pos.get("x", 0),
                            pos.get("y", 0),
                            pos.get("z", 0),
                        ),
                        FreeCAD.Rotation(
                            FreeCAD.Vector(
                                rot.get("Axis", {}).get("x", 0),
                                rot.get("Axis", {}).get("y", 0),
                                rot.get("Axis", {}).get("z", 1),
                            ),
                            rot.get("Angle", 0),
                        ),
                    )
                    setattr(obj, prop, placement)

                elif prop == "Placement" and isinstance(val, (list, tuple)):
                    # The dict form is the documented one; [x, y, z] is the
                    # shorthand a caller reaches for, and it used to fall
                    # through to the generic branch and die with the opaque
                    # "'list' object has no attribute 'get'".
                    if len(val) == 3 and all(isinstance(v, (int, float)) for v in val):
                        setattr(
                            obj, prop, FreeCAD.Placement(FreeCAD.Vector(*val), FreeCAD.Rotation())
                        )
                    else:
                        raise ValueError(
                            "Placement as a list must be [x, y, z]; for a rotation use the "
                            'dict form {"Base": {"x": .., "y": .., "z": ..}, '
                            '"Rotation": {"Axis": {"x": .., "y": .., "z": ..}, "Angle": deg}}.'
                        )

                elif isinstance(getattr(obj, prop), FreeCAD.Vector) and isinstance(val, dict):
                    vector = FreeCAD.Vector(val.get("x", 0), val.get("y", 0), val.get("z", 0))
                    setattr(obj, prop, vector)

                elif prop in ["Base", "Tool", "Source", "Profile"] and isinstance(val, str):
                    ref_obj = doc.getObject(val)
                    if ref_obj:
                        setattr(obj, prop, ref_obj)
                    else:
                        raise ValueError(f"Referenced object '{val}' not found.")

                elif prop == "References" and isinstance(val, list):
                    setattr(obj, prop, resolve_references(doc, val))

                else:
                    setattr(obj, prop, val)
            # ShapeColor is a property of the ViewObject
            elif prop == "ShapeColor" and isinstance(val, (list, tuple)):
                setattr(obj.ViewObject, prop, _to_shape_color(val))

            elif prop == "ViewObject" and isinstance(val, dict):
                for k, v in val.items():
                    if k == "ShapeColor":
                        setattr(obj.ViewObject, k, _to_shape_color(v))
                    else:
                        setattr(obj.ViewObject, k, v)

            else:
                setattr(obj, prop, val)

        except Exception as e:
            FreeCAD.Console.PrintError(f"Property '{prop}' assignment error: {e}\n")
            failures.append(f"{prop}: {e}")

    if failures:
        raise ValueError(
            "Failed to set propert"
            + ("y" if len(failures) == 1 else "ies")
            + ": "
            + "; ".join(failures)
        )
