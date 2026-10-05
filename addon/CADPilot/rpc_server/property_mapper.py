"""Property assignment from JSON-friendly dicts onto FreeCAD document objects."""

from dataclasses import dataclass, field
from typing import Any

import FreeCAD


@dataclass
class Object:
    name: str
    type: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


#: Names accepted by :func:`parse_color`. A short, obvious set only: a big
#: palette is what hex is for, and every name here is one a caller can guess.
NAMED_COLORS: dict[str, tuple[float, float, float]] = {
    "black": (0.0, 0.0, 0.0),
    "white": (1.0, 1.0, 1.0),
    "grey": (0.5, 0.5, 0.5),
    "gray": (0.5, 0.5, 0.5),
    "red": (0.8, 0.1, 0.1),
    "orange": (0.9, 0.5, 0.1),
    "yellow": (0.9, 0.8, 0.15),
    "green": (0.1, 0.7, 0.25),
    "blue": (0.15, 0.35, 0.8),
    "steel": (0.5, 0.6, 0.7),
}


def parse_color(val: Any) -> tuple[float, float, float, float]:
    """Normalise a color to the 4-float RGBA tuple FreeCAD accepts.

    ONE parser for every color entry point (``create_object``/``edit_object``
    ShapeColor and the ``color`` op), so the two cannot disagree about what a
    caller's value means. Accepted forms:

      - ``[r, g, b]`` / ``[r, g, b, a]``, floats in 0..1
      - ``[r, g, b]`` as INTEGERS with a component > 1 -> the 0-255 scale. The
        integer rule is what keeps the two scales apart: only [204, 26, 26]
        can mean 0-255, so a float [1.4, 0.2, 0.2] is reported as out of range
        instead of silently becoming 1.4/255
      - ``"#rrggbb"`` / ``"#rrggbbaa"``, ``#`` optional
      - a name from ``NAMED_COLORS`` ("red", "steel", …), case-insensitive
    """
    if isinstance(val, str):
        text = val.strip().lower()
        if text in NAMED_COLORS:
            return (*NAMED_COLORS[text], 1.0)
        hex_text = text[1:] if text.startswith("#") else text
        if len(hex_text) in (6, 8) and all(c in "0123456789abcdef" for c in hex_text):
            channels = [int(hex_text[i : i + 2], 16) / 255.0 for i in range(0, len(hex_text), 2)]
            a = channels[3] if len(channels) == 4 else 1.0
            return (channels[0], channels[1], channels[2], a)
        raise ValueError(
            f"color {val!r} is not recognised. Use [r,g,b] floats 0..1, "
            f"[r,g,b] ints 0-255, '#rrggbb' hex, or a name: "
            f"{', '.join(sorted(NAMED_COLORS))}."
        )
    if not isinstance(val, (list, tuple)) or len(val) not in (3, 4):
        raise ValueError(
            f"color must be an RGB/RGBA sequence, a '#rrggbb' string or a name, got {val!r}."
        )
    try:
        channels = [float(c) for c in val]
    except (TypeError, ValueError):
        raise ValueError(f"color components must be numbers, got {val!r}.") from None
    is_byte_scale = all(isinstance(c, int) and not isinstance(c, bool) for c in val)
    if is_byte_scale and max(channels[:3]) > 1.0:
        # 0-255 ints. FreeCAD stores out-of-range channels as given and clamps
        # only at render time, so an out-of-range value must be refused here —
        # it would otherwise be reported as a successful repaint.
        if not all(0 <= c <= 255 for c in channels):
            raise ValueError(
                f"color components must be within 0-255 when any exceeds 1, got {val!r}."
            )
        channels = [c / 255.0 for c in channels]
    elif not all(0.0 <= c <= 1.0 for c in channels):
        raise ValueError(f"color components must be 0..1 floats or 0-255 ints, got {val!r}.")
    r, g, b = channels[0], channels[1], channels[2]
    a = channels[3] if len(channels) == 4 else 1.0
    return (r, g, b, a)


def format_color(val: Any) -> str:
    """A color as "#rrggbb", for readback/echoes ("" when unparseable)."""
    try:
        r, g, b, _ = parse_color(val)
    except ValueError:
        return ""
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in (r, g, b))


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

            # Color, in every form parse_color accepts ([0..1] floats, 0-255
            # ints, "#rrggbb", a name). This must also come BEFORE the
            # PropertiesList branch: LineColor/PointColor ARE real properties
            # there, so a hex string used to reach a bare setattr and die with
            # FreeCAD's opaque type error. ShapeColor is not in PropertiesList
            # at all (a dynamic property that writes through to the persisted
            # ShapeAppearance), which is what the old branch below was for.
            elif (
                prop in ("ShapeColor", "LineColor", "PointColor")
                and isinstance(val, (list, tuple, str))
                and not (isinstance(val, str) and val.startswith("="))
            ):
                if getattr(obj, "ViewObject", None) is None:
                    raise ValueError(
                        f"{prop} needs a GUI: '{obj.Name}' has no ViewObject (console mode)."
                    )
                setattr(obj.ViewObject, prop, parse_color(val))

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
                    elif any(k in val for k in ("x", "y", "z")):
                        # The flat shorthand {"x": .., "y": .., "z": ..} — the
                        # same shape every other Vector property accepts — used
                        # to fall into the empty-pos branch and build an
                        # IDENTITY Placement: the tool reported success and the
                        # part never moved (live: a bore tool cylinder stayed at
                        # the origin, so the "hole" became a quarter-notch at a
                        # corner and the cut was 75 mm^3 short).
                        pos = val
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
            # ViewObject block: colors go through the same parser, everything
            # else (Transparency, DisplayMode, LineWidth, …) straight through.
            elif prop == "ViewObject" and isinstance(val, dict):
                for k, v in val.items():
                    if k in ("ShapeColor", "LineColor", "PointColor"):
                        setattr(obj.ViewObject, k, parse_color(v))
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
