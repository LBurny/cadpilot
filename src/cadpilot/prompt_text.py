ASSET_CREATION_STRATEGY = """
Asset Creation Strategy for CADPilot

## Choose the mode first

- **Designing a part? → Sketch mode.** Draw a constrained 2D profile, then
  turn it into 3D. Never sculpt parts from raw primitives when a sketch
  expresses the shape.
- **Combining finished parts? → Assembly mode.** Mate parts with persistent
  joints instead of hand-placing them.
- Both modes run inside a modeling session (below) for step recording and
  rollback.

## Sketch mode (part design)

1. cad(operation="sketch") — atomic constrained sketch: geometry + constraints
   in one call; the result reports dof / fully_constrained. ALWAYS fully
   constrain (dof=0) before turning a profile into 3D.
   - plane: "XY"/"XZ"/"YZ" (+offset), {"face": [obj, "FaceN"]} on an existing
     solid face, or {"datum": name} on a datum plane.
   - external: [[obj, "EdgeN"|"VertexN"], ...] adds external geometry (GeoIds
     from -3 in list order); constraints may reference it, e.g.
     {"type": "coincident", "items": [[0, "start"], [-3, "start"]]} — this is
     how a new part references an existing part's geometry parametrically.
   - cad(operation="datum_plane") creates offset/face-attached datum planes
     for sketches that don't lie on a base plane or an existing face.
2. Single view → 3D: pad / pocket / revolution / groove (obj_name = the
   sketch). thickness / draft for shells and tapers.
3. Multi-view → 3D (visual hull): draw the part's silhouette in 2-3 views
   (Top=XY, Front=XZ, Side=YZ, one closed outer profile per sketch), then
   cad(operation="hull", obj_properties={"sketches": {...}}) — the solid is
   the intersection of the extruded views. Edit a view sketch and re-run hull
   with the same name to iterate; refine with fillet/chamfer/pocket after.
4. Parametric design: variables (Spreadsheet) + "=Spreadsheet.alias"
   expressions in sketch constraint values and feature lengths. To explore a
   design space, edit the cells and re-measure with measure_geometry() in a
   loop.

## Assembly mode (multi-part)

assembly_session — an independent state machine with PERSISTENT joints
(FreeCAD Assembly workbench). The mate-based counterpart to one-shot
assemble(); move a parent part, call solve, children follow.

- Flow: start(ground=part) → add_component(part)×N → mate(a, b,
  joint_type)×N → solve → verify → complete. rollback(to_step) undoes
  mistakes stepwise; status shows components/joints/steps.
- Mate ref: {"part": name, plus exactly one of face="FaceN" / anchor=name /
  point=[x,y,z]}. Prefer anchors: get_anchors() lists auto-derived connection
  points (axis_mid, face centers, bbox) and set_anchors() defines semantic
  ones (coord_frame="global" when your coordinates are global).
- trim={"winner": "inserted"|"base"} resolves interference declaratively:
  the winner keeps its shape and dims; the loser gets a non-destructive cut
  that rollback fully restores.
- verify returns per-joint residuals, islands, and gap profiles — trust those
  numbers, not screenshots.

## Modeling workflow (sessions)

For any non-trivial modeling task, work inside a session:

1. session_start(doc_name, create_document=True) — binds a session to the document.
2. Build with cad() — every successful mutation is recorded as a step
   (backed by a FreeCAD transaction).
3. Trial and error: use session_rollback(to_step) to backtrack instead of
   deleting and rebuilding. session_redo() restores rolled-back steps until a
   new cad() call. Check session_status() when unsure — it shows step count,
   suggestions, and risks (e.g. the model was edited in the GUI).
4. execute_code() works too but records NON-ATOMIC steps that block rollback
   unless forced — prefer cad() when the operation is expressible with it.
5. When satisfied: session_complete(save=True, description=..., tags=...) —
   saves the document and stores the whole workflow into the pattern store.

## Knowledge hierarchy (use in this order)

① Your own FreeCAD Python knowledge — always try this first.
② recall_patterns(query) — retrieval of workflows that worked before
  (from session_complete and save_pattern), when you are unsure.
③ inspect_freecad(doc_name, obj_name) or inspect_freecad(dotted_name="Part.makeLoft")
  — runtime API introspection as the last-resort reference.
After a non-trivial approach succeeds, store it with save_pattern() so it can be
recalled next time.

## Positioning & verification (free mode, outside assembly_session)

Blind absolute Placement is the #1 source of floating parts — avoid manually
computing Placement values; use relative moves and alignment instead:

   a. **Relative moves** — cad(operation="move") with translate/rotate
      positions objects incrementally (easiest and most error-proof).

   b. **Query global positions** — get_positioning_info() returns precise
      global coordinates of faces, edges, and vertices (center, normal, axis,
      radius, start/end points) for computing exact offsets.

   c. **Align to target** — align_shapes() positions an object relative to
      another: "touch" (face-to-face), "center", "axis" modes.

   d. **Verify** — measure_geometry() (global bbox, center_of_mass) and
      check_interference() confirm placement and detect collisions.

   e. **Connectivity auto-audit** — after every committed cad() mutation the
      framework re-audits the document and appends a "⚠ Connectivity" warning
      listing islands: groups of parts that don't touch the main assembly
      (touching = exact gap ≤0.5mm or volume intersection). Treat this warning
      as a BLOCKING issue: realign the listed parts with move/align_shapes/
      assemble so they genuinely touch before adding new parts. Parts that
      merely LOOK close in a screenshot but have a small gap are reported —
      trust the numbers, not the view. session_status() also surfaces
      unresolved islands as a disconnected_islands risk.

## Building content

0. Before starting, use get_objects() to confirm the current document state.

1. Prefer sketch mode for anything beyond simple stock shapes. Primitives
   (cad(operation="create_object") with Part::Box/Cylinder/...) are for quick
   stock, fixtures, and mating references — not the main design path.
   - When creating or editing many objects, prefer cad(operation="batch") to
     do them in one call (one undo unit, at most one screenshot).

2. Always assign clear and descriptive names to objects.

3. After editing, verify properties were applied with get_object().

4. Complex geometry: prefer cad() feature ops (boolean/fillet/chamfer/loft/
   sweep/mirror/pattern — atomic, rollback-able) over execute_code. Use
   get_topology first to pick edges/faces by index. execute_code remains
   for anything cad() cannot express; for long-running pure OCCT
   computations use execute_code_async() + get_task_result().
"""
