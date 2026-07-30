"""Declarative priority trimming: non-destructive Part::Cut.

The loser part is cut by a Part::Cut feature (winner keeps its shape and
dimensions); the loser's assembly Link is re-pointed to the cut result, so
joints referencing the link survive. Rollback = delete the Cut + re-point
the link back (handled by joint_ops rollback_step).
"""

from .joint_ops import _global_shape, _local_shape


def apply_trim(doc, inserted_part: str, base_part: str, winner: str) -> dict | None:
    """Trim the loser of an intersection. Returns {'cut', 'overlap_mm3',
    'loser'} or None when there is no meaningful overlap.

    Frames: overlap is measured in GLOBAL space (link.Shape is placed);
    the baked cut is produced in the loser link's LOCAL (identity) frame so
    the static Part::Feature composes correctly with the link placement for
    both the solver and the viewer.
    """
    winner_part = inserted_part if winner == "inserted" else base_part
    loser_part = base_part if winner == "inserted" else inserted_part
    w_link = doc.getObject(f"L_{winner_part}")
    l_link = doc.getObject(f"L_{loser_part}")
    if None in (w_link, l_link):
        raise ValueError("trim requires both parts to be assembly components")

    w_global = _global_shape(w_link)
    l_global = _global_shape(l_link)
    overlap = w_global.common(l_global)
    if overlap.Volume < 1.0:  # mm^3 — below this it's a graze, not an insert
        return None

    # winner shape pulled back into the loser link's local frame
    w_in_loser_frame = w_global.transformGeometry(l_link.Placement.inverse().toMatrix())
    trimmed = _local_shape(doc, l_link).cut(w_in_loser_frame)
    feat = doc.addObject("Part::Feature", f"TrimCut_{loser_part}")
    feat.Shape = trimmed
    doc.recompute()
    l_link.LinkedObject = feat  # link (and its joints) now show the trimmed part
    for obj in (feat, doc.getObject(loser_part), doc.getObject(winner_part)):
        try:
            if obj is not None:
                obj.ViewObject.Visibility = False
        except Exception:
            pass
    return {"cut": feat.Name, "overlap_mm3": round(overlap.Volume, 2), "loser": loser_part}
