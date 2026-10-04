# Changelog

## v0.5.9 (2026-10-04)

A second round of multi-agent stress modelling (a travel mug, a lathe-turned
vase, a multi-part cabinet hinge) went after the *scheduler-level* invariants —
whether the step journal, rollback and the spatial audit can be trusted at all.
Single-operation behaviour held up; how it is accounted for did not.

### Fixed

**Feature operations and their responses** — the first stress round (a
parametric flange, a Part-level duct and a hinge assembly) surfaced these:

- **`cad()` no longer throws the addon's findings away** (`operations/core.py`).
  Only `batch` merged the RPC payload, so every other operation dropped what the
  addon had just measured: a `pocket` whose cut removed no material (the profile
  sat outside the solid, or the sketch attached to the wrong face) reported
  plain "created successfully", and `sketch` never reported the
  `dof`/`fully_constrained` that `operation_help` promises. Warnings now go into
  the summary text — the same `WARNING - ...` shape the connectivity audit uses,
  so a silent no-op cannot read as success — and the remaining fields
  (`dof`, `fully_constrained`, `volume_mm3`, `solids`) ride along as a JSON
  payload. An operation with nothing extra to report still answers with its plain
  summary line.

- **Part-level `pattern` works on a document that is open but not ACTIVE**
  (`feature_ops.py`). Draft's array helpers create their result in
  `FreeCAD.ActiveDocument`, so patterning any other open document failed with
  "PropertyLink does not support external object" — the norm once more than one
  document is open (and exactly what a parallel multi-agent run produces). The
  target document is made active for the call and the user's previous active
  document is restored afterwards.

- **`pattern`'s `count` accepts an expression** (`feature_ops.py`).
  `count="=Vars.n_holes"` died inside `int()` ("invalid literal for int() with
  base 10") although every other operation accepts `=expr`, so a Spreadsheet
  could not drive an array. An unresolvable expression is now reported as such
  instead of silently evaluating to 0.

- **`loft` honours `obj_name`** (`feature_ops.py`). A base-less operation
  receives it as `spec["base"]`; reading only `spec["name"]` named every loft
  "Loft", contradicting `operation_help`.

- **Deleting a PartDesign Body's Tip leaves a usable Body**
  (`object_factory.py`). `removeObject` left `Body.Tip` dangling, so the Body
  reported `['Touched', 'Invalid']` and `Body.Shape` raised "shape is invalid" —
  every later feature on it failed until the tip was set by hand. The tip is
  re-pointed to the last surviving solid-producing member (never to a sketch:
  that would stop the next feature from claiming the tip and silently hide it).

- **The auto `faceN_center` anchor lies ON its face** (`assembly_ops.py`). A
  face's area centroid falls in the HOLE of an annular face — a bushing, washer
  or flange top — so an anchor-based mate died with "no planar face within 1 mm
  of the anchor/point". When the centroid is off the face the anchor falls back
  to the midpoint of the face's longest edge, which is always on it.

- **Documentation corrections** (`tool_docs.py`, `trim_ops.py`). `trim` produces
  a non-destructive baked cut (a static `TrimCut_*` Part::Feature), not a live
  `Part::Cut`; the assembly toolchain IS re-runnable from the journal
  (`EXECUTABLE_OPS` covers it); and anchor/point mate refs must resolve ON the
  part surface (the auto `axis_*`/`bbox_*` anchors are for `assemble`/`verify`
  checks, not mate refs).

**Journal, audit and assembly accounting** — the second stress round (a travel
mug, a lathe-turned vase, a cabinet hinge) went after the invariants that decide
whether the journal can be trusted at all:

- **The journal stops lying once a document has more than ~20 modelling steps**
  (`step_engine.py`, `rpc_server.py`). The "did this commit actually change
  anything?" probe compared `Document.UndoCount` before and after — but FreeCAD
  caps the undo stack (`MaxUndoSize`, 20 by default), so at the cap a REAL
  commit leaves the count pinned at 20. Every mutation after the twentieth was
  rewritten as read-only (`transaction=""`, `atomic=False`, `executable=False`):
  `step_control rollback_to`/`replay` silently skipped real steps,
  `plan_rollback` miscounted transactions, and a mutating `execute_code`
  reported "read-only: no document change". Measured live on 1.1.4: a real
  commit at the cap still moves `UndoNames` (oldest entry evicted, newest
  appended) while an EMPTY commit moves nothing — so all three commit sites
  (`_run_op_with_screenshot`, `downgrade_if_no_undo`, `run_record`) now compare
  `step_engine.undo_token()` (count + names), decisive where the count alone is
  blind.

- **`execute_code` attributes its work to the document that actually changed**
  (`rpc_server.py`, `operations/core.py`). The snippet's transaction, change
  probe and journal record were all bound to whichever document happened to be
  ACTIVE when the call arrived — so a snippet that switched documents (a
  documented move: set the active document so the step lands on the right one)
  had its change filed into the wrong document's journal, and the reply claimed
  "read-only: no document change" for a run that had in fact mutated something.
  Worse under multi-agent use: a sibling's snippet landed as an atomic step in
  this session's journal. The addon now fingerprints EVERY open document
  (`document_tokens`/`changed_documents`), reports the one that really changed
  along with `attributed` (was the change inside the transaction we own?) and
  `foreign_changes`, and the MCP side refuses to record a change it does not own,
  saying so instead of reporting read-only.

- **The connectivity audit works again on ordinary documents**
  (`assembly_ops.py`). Origin datums (`App::Line`/`App::Plane`/`App::Point`) are
  VISIBLE with an INFINITE bound box, and an empty sketch has no faces; handing
  either to OCC threw `Geom_RectangularTrimmedSurface::U parameters out of range`
  straight out of `distToShape`/`common`, killing the whole `verify_assembly`
  call — and the post-mutation auto-audit swallows exceptions, so it had
  silently stopped running on every default PartDesign document. Work geometry is
  now rejected up front (`_auditable_shape`), PartDesign features are excluded as
  body members (a member is not a separate solid — `Body.Shape` IS its tip's
  result, so auditing both reported the feature overlapping its own body as a
  6283 mm³ "interference"), and an unmeasurable pair is counted in
  `summary.skipped_unmeasurable` instead of aborting the scan.

- **Deleting an object that others are built on is refused**
  (`object_factory.py`). `removeObject` leaves the consumer behind with its base
  link cleared: a PartDesign feature loses `BaseFeature` and a SUBTRACTIVE
  feature turns ADDITIVE. Measured live: a plate-with-hole (6283 mm³) became the
  pocket's own cylinder (1571 mm³) while the Body still reported
  `['Up-to-date']` and the delete reported success. `delete_object` now lists the
  dependents (transitively, containers excluded — FreeCAD lists the owning Body
  in every feature's `InList`) and refuses, telling the caller to delete them in
  reverse order first.

- **`session_rollback(to_step=0)` verifies what the undo actually did**
  (`operations/core.py`, `session_state.py`). The post-rollback fingerprint check
  was guarded by `if sess.steps:` — and truncating to 0 leaves no steps, so the
  check never ran and a rollback that left objects behind reported success with
  an empty warning list. A session now records `initial_objects` at
  `session_start` (the state step 0 must restore), the check covers to_step=0,
  and leftovers are named. A session resumed from an older file has no recorded
  starting state and says "could not be verified" instead of guessing.

- **An axis-based joint can no longer be assembled wrongly and reported as
  success** (`joint_ops.py`). A mate ref carries only a face plus its nearest
  VERTEX, and the JCS is placed at that landmark — which on a CYLINDRICAL face
  lands the parts TANGENT. Measured live: a `revolute` pin/bore mate put the pin
  2.1 mm off the bore axis (exactly `bore_r - pin_r`) and 42 mm axially out,
  while the joint reported `residual 0.0` and `verify` found no interference.
  `revolute`/`cylindrical`/`slider` joints whose ref resolves to a non-planar
  face are now refused *before* anything moves, pointing at the anchor-based
  `assemble(mode="axis")` for axle-in-hole fits.

- **A read-only `execute_code` no longer breaks a pending plan's cursor**
  (`step_engine.py`). The record was appended AFTER the planned tail; the plan
  cursor walks from the first planned step, so `run_next`/`run_to` kept reporting
  "nothing to run" although planned steps were waiting. The record is now
  inserted before the tail.

- **A pattern `count` expression that does not resolve now says so**
  (`feature_ops.py`). A misspelled spreadsheet alias does not raise — the probe
  property simply stays 0 and goes `Invalid` — so the caller saw a baffling
  "pattern count must be >= 2" instead of being told which alias is wrong.

- **`snapshot` no longer claims a mutation** (`step_engine.py`). `mutated=True`
  with no transaction made the marker a rollback blocker, so a force-less
  rollback across it reported the non-atomic guard instead of the accepted
  soft-lock the step exists to impose.

## v0.5.8 (2026-10-04)

### Added

- **Every screenshot tool now lets the model choose `image` or `file` per
  call** (`server.py`, `responses.py`, `operations/core.py`). v0.5.6 added
  `--screenshot-mode file`, but it is a server-startup switch the model cannot
  reach — and on hosted clients nobody can. All eight screenshot-capable tools
  (`get_view`, `cad`, `execute_code`, `create_document`, `get_objects`,
  `get_object`, `set_anchors`, `assemble`) now take
  `screenshot_mode="image"|"file"`: return the inline base64 PNG, or write the
  PNG to `$CADPILOT_HOME/screenshots/` and return only the path. Omitting the
  parameter keeps the server's `--screenshot-mode` default, so nothing changes
  for existing setups. The default long edge drops from 512px to 384px —
  roughly half the pixels, cheaper in context — and an explicit
  `width`/`height` still overrides.

### Fixed

- **`get_view` honors `--only-text-feedback`** (`server.py`). It was the one
  tool that bypassed `ServerState.resolve_screenshot()`, so the "never return
  a screenshot" guarantee leaked through exactly the tool whose only job is
  screenshots; it now answers with a text notice instead.
- **Tool-description consistency pass** (`server.py`). All screenshot tools
  document `with_screenshot`/`screenshot_mode` with one shared Args line —
  most never documented them at all. `session_start` no longer claims a
  read-only `execute_code` run blocks rollback without force; such runs are
  simply not recorded. And the knowledge-hierarchy paragraph triplicated
  across `save_pattern`/`recall_patterns`/`inspect_freecad` now lives only at
  the two entry points. The docstring budget stays under 11,000 chars.

## v0.5.7 (2026-10-04)

### Fixed

- **The mouse guard no longer freezes you out of FreeCAD while a model is
  being built** (`gui_dispatch.py`). The guard is supposed to hold back queued
  MCP work while you drag the mouse, but a full morning's addon log showed it
  had not deferred once, while single tasks blocked the GUI thread for up to
  20 s. Three stacked races: Qt's `mouseButtons()` is event-delivered state —
  stale exactly while the event loop is busy — and the win32 physical state
  (`GetAsyncKeyState`) was only used to *veto* phantom holds, never to
  *detect* fresh ones, so a press that began mid-task stayed invisible and
  the next task started mid-drag; the drain loop checked the guards once
  before its first task, so a mid-drain press could not pause the backlog;
  and the 15 s hold cap punched a task straight through long inspection
  drags. On Windows the OS physical state is now authoritative in both
  directions (press → hold the queue, release → resume, never capped), the
  drain loop re-checks the mouse/popup/modal guards between tasks, and the
  phantom caps — time-based now, not heartbeat ticks — apply only to the
  non-Windows heuristic path. A call that does time out tells the model why
  and to simply retry.

## v0.5.6 (2026-10-04)

### Added

- **`--screenshot-mode file`** (`responses.py`, `screenshot_store.py`).
  MCP transports images as base64, and a client that echoes the raw tool result
  injects that blob into the conversation permanently — tens of thousands of
  tokens per screenshot, and it breaks the prompt cache. The server now offers
  a mode that writes the PNG to `$CADPILOT_HOME/screenshots/` (keeping the last
  20) and returns only the file path, so an agentic client can read it with its
  own file tool. The default stays `image`, and a failed write falls back to
  the inline image. `screenshot_content()` is now the single entry point for
  every screenshot content block, and the default screenshot long edge drops
  from 768px to 512px.

### Fixed

- **A PartDesign transform no longer leaves the Body one feature behind**
  (`feature_ops.py`, `tip_policy.py`). FreeCAD advances `Body.Tip` by itself for
  a pad/pocket but NOT for a transform: a polar pattern on a flange was correct
  in itself (15246 mm³, six holes) while the Body still showed one hole
  (15874 mm³) and the op reported success. The tip is now pushed explicitly
  after the feature is built, and only a successor of the current tip may claim
  it — judged by FreeCAD's own `OutList` — so a dress-up on a mid-chain feature
  cannot hide what follows it.
- **fillet/chamfer inside a Body build the PartDesign dress-up**
  (`feature_ops.py`). They previously produced a document-root `Part::Fillet`,
  which is not in `Body.Group`, does not follow the Body's Placement, and leaves
  the Body Invalid when assigned to `Tip`. Dressing a feature that is NOT the
  tip is now refused outright: FreeCAD moves the tip onto the new feature and
  silently drops every later one (measured: 15999 mm³ with the six bolt holes
  gone), and `Body.insertObject` is not a safe API for a mid-chain insert (it
  duplicated the Group entry and left the tip wrong). `thickness`/`draft` share
  the hazard and get the same gate.
- **GUI-dispatch timeouts name the real cause** (`gui_dispatch.py`). An open
  modal dialog, an open menu or a real drag now says so, instead of the
  misleading "the waker/heartbeat chain may be dead" — which is what a human
  operating FreeCAD alongside the model produces. A deferral lasting over 10 s
  is logged once so `get_addon_log` shows the stall.
- **Invalid-Shape errors name the object** (`feature_ops.py`): the message now
  carries the object's name and FreeCAD's `StatusString` with the likely causes
  instead of "<op> produced an invalid Shape" alone.
- **`execute_code` no longer reports a successful snippet as a failure**
  (`rpc_server.py`) when the snippet closed or replaced the document the call
  was bound to — reading `doc.Name` off the deleted reference raised
  `ReferenceError`.

## v0.5.5 (2026-10-04)

### Fixed

- **`cad()` feature ops reject reserved spec keys** (`feature_ops.py`): user
  `obj_properties` could clobber the internal `type`/`base` keys and silently
  break the feature; they are now rejected up front and internal keys always
  win.
- **`session_status` no longer cries "out of sync"** (`operations/core.py`) when
  the addon journal and the session step count differ — they legitimately do
  (the journal is document-lifetime, the session is session-lifetime); only
  undo-stack drift is reported.
- **`execute_code` on a foreign document is not recorded as a session step**
  (`rpc_server.py`, `operations/core.py`): the addon now reports which document
  changed, so `session_rollback` can no longer pop the wrong document's undo
  stack.
- **`step_control insert` wraps a single step dict into a list** and rejects
  empty/malformed input before the RPC, instead of failing opaquely addon-side.
- **`move` accepts plain `[x, y, z]` vectors** (`operations/core.py`): the
  dict-only form crashed with `'list' object has no attribute 'get'` on the
  natural input, even though the rest of the API takes lists.
- **`datum_plane`'s `plane.face` resolves direction tokens** (`+Z`, `top`, …)
  like a sketch's does, instead of stuffing `'+Z'` into the attachment and
  recomputing Invalid; recompute failures now include FreeCAD's `StatusString`.
- **Assembly mate refs report `landing`** (`joint_ops.py`): a mate result names
  the actual face/vertex per side and warns when that landing vertex sits far
  from the ref's intent point — face center, anchor position, or the point
  itself — because nearest-vertex choice is arbitrary on symmetric faces and a
  zero residual does not mean the part landed where the user meant.
  `point_on_face` is documented as the landing control, and anchor refs are now
  mapped into the link frame before nearest-vertex matching (they were compared
  in the wrong frame). `point_on_face` without a face is rejected instead of
  silently ignored.
- **Assembly rollback restores link placements BEFORE removing links**
  (`joint_ops.py`): removal hands the link's placement back to the part, so
  restoring after deletion silently leaked the post-mate placement.

## v0.5.4 (2026-10-04)

### Fixed

- **Roll back now really restores the model** (`step_engine.py`, `step_journal.py`).
  A rollback reported success while the objects it was asked to drop were still
  there. Two causes: a step with no transaction behind it cannot be undone by
  FreeCAD (a property change on its own creates no undo entry, and every
  `execute_code` step recorded before v0.5.2 owned none), and when no step in
  range owned a transaction the undo count was 0, so even the "the stack was
  shorter" warning never fired. The journal was rewound to `planned` anyway, so
  the log claimed a state the model was not in. Rollback now checks what the
  undo stack actually holds and picks one of three paths:
  - everything in range owns a transaction and came off the stack: unchanged
    behavior (`restored: "native"`, no extra work);
  - some steps own no undo entry: the objects the journal says those steps
    introduced are removed by name, so entities a rollback was asked to drop are
    gone. Property changes they made (placements, dimensions) cannot be
    restored, and the reply says so (`restored: "partial"`, with the stranded
    step numbers);
  - steps 1..N can all be re-created from the journal: the model is rebuilt by
    removing what the journal built and re-running 1..N, which is an exact
    restore (`restored: "rebuild"`).

  `reject` gets the same treatment for the steps it destroys. Every reply now
  carries `restored`, `removed` and `stranded`, and the MCP-side
  `session_rollback` no longer implies a full restore when the undo stack came
  up short.

- **Rollback verifies the undo result instead of trusting the count**
  (`step_engine.py`). The FreeCAD undo stack is shared with the GUI: a manual
  edit interleaved on it pops under the rollback's name while the popped count
  still matches, so a rollback could report `native` over a wrong model. After
  the undo, the journal now checks the object sets — objects the post-target
  steps created must be gone, journal-built objects expected at the target must
  be present — and escalates to the rebuild path on any discrepancy. `reexecute`
  refuses outright when its base could not be restored cleanly (it would
  otherwise duplicate objects under deduplicated names), pointing at
  `rollback_to`.
- **Rollback cleanup no longer deletes objects that predate the journal**
  (`step_journal.py`). Every `objects_after` snapshot lists the entire
  document, and the removal set was built by subtracting whole snapshots — at
  index 0 the target snapshot is empty, so a rebuild/reject of the first steps
  would delete the user's own, pre-journal objects. Removal sets are now
  per-record before/after diffs (`created_since`), naming only what the journal
  actually built. A rebuild also re-runs failed records (their transaction
  aborted, so re-running is safe), reports `success: false` when the re-run
  stops early, and the Steps panel prints result warnings (red) after a
  success, so a rebuild/partial degradation is visible in the dock.
- **Manual edits survive reexecute/replay for real** (`step_engine.py`). The
  parameter sync (object → journal, so a human's correction in FreeCAD's
  property panel is not silently reverted by a later re-run) had an undo echo:
  reexecute popped the edit's transaction, FreeCAD restored the OLD value, and
  the sync observer mirrored that restoration back into the journal as if it
  were a manual edit — the re-run then rebuilt at the old value. Engine-driven
  windows (undo/redo and step re-runs) now mute the observer (`_EngineQuiet`);
  a genuine GUI edit never happens inside one.
- **`snapshot` bundles the review** (`step_engine.py`): `params.accept_done`
  accepts every done step in the same call — "the good steps are reviewed, I
  did the complex part by hand, save a baseline, continue from here" is now one
  call instead of ten accept clicks. Its `objects_before` anchors on the last
  DONE record's snapshot; anchoring on the physical last record (possibly a
  planned tail with no snapshot) marked the user's whole document as "new".

## v0.5.3 (2026-10-04)

Found by stress-testing a parametric PartDesign model (variables → constrained
sketch → pad → pocket → 6-hole bolt ring). All three used to fail SILENTLY —
the tool reported success while the geometry was wrong.

### Fixed

- **`pattern` was wrong for every PartDesign feature** (`feature_ops.py`): it
  used Draft's array, which copies the base object's whole Shape — for a
  PartDesign feature that Shape is the entire body, so "pattern this hole"
  produced N overlapping copies of the whole part. It now creates a
  PartDesign::PolarPattern / LinearPattern inside the Feature's Body with
  `Originals=[feature]`. FreeCAD 1.1 still cannot be driven reliably that way
  (occurrences can come out coincident), so when the transform has no effect the
  op now **refuses with a clear error and an actionable workaround** instead of
  returning a part with one hole where six were asked for. Part-level bases keep
  using the Draft array.
- **Sketch face attachment can now be a direction** (`sketcher_ops.py`):
  `plane={"face": [obj, "+Z"]}` (also -Z, ±X, ±Y, top/bottom/left/right/
  front/back) resolves to the planar face facing that way. Face names (Face1,
  Face2, …) are re-derived after every feature, so a name read off one feature
  can silently mean a different face on the next one — the sketch then attaches
  to the wrong plane, its pocket cuts air, and everything still reports success.
- **A cut that removed nothing is now reported** (`feature_ops.py`):
  `describe_feature` compares a pocket/groove against its predecessor
  (`BaseFeature`) and returns a `warnings` entry naming the likely cause
  (profile outside the solid, or a stale face attachment).
- **The Steps panel detail pane shows the snippet** (`step_panel.py`,
  `step_engine.py`): an execute_code row rendered as an opaque `{}`; the code is
  now always kept in `params` and shown as editable, colourised-free source
  (edit it and press Re-run to iterate). Step rows also say what the snippet DID
  (`execute_code: +6 object(s): Hole1, …` / `read-only`).
- **Force prompts name the real step kind**: `blocking_text` no longer blames
  "execute_code" for a snapshot marker that is doing the blocking.

### Changed

- `ensure_panel()` rebuilds a Steps dock left over from an older class, so a hot
  reload picks up panel changes without restarting FreeCAD (verified: the
  detail pane switched from escaped JSON to multi-line source).
- `reexecute` merges params at the TOP level; a step's operation parameters live
  under `obj_properties` (a `variables` update must be sent as
  `{"obj_properties": {"cells": …}}`) — documented in `operation_help`.

## v0.5.2 (2026-10-04)

### Fixed

- **`execute_code` is now rollback-able** (`rpc_server.py`, `step_engine.py`):
  a bare document change in FreeCAD creates no undo entry, so an AI that models
  through `execute_code` — the common case — produced journal steps that
  `rollback` could not undo, and every rollback demanded `force`. The snippet is
  now wrapped in a transaction and the result reports `changed`:
  - a **mutating** run becomes an ATOMIC step that owns one undo entry, is
    re-runnable (the code is stored and re-executed through the shared
    `exec_snippet`), so `rollback_to` / `reexecute` / **`replay` rebuilds an
    execute_code-built model**;
  - a **read-only** run records a step that neither blocks a rollback nor is
    re-run (`mutated=False`), so trailing inspections no longer force `force`;
    the MCP session does not record it at all, keeping the log's
    one-transaction-per-step invariant.
- **A failing `execute_code` no longer leaves half-applied mutations**: the
  wrapper transaction is aborted when the snippet raises.
- AST regression tests pin both contracts
  (`tests/test_execute_code_atomic.py`), verified to fail against the previous
  source. Live-verified on FreeCAD 1.1.4: mutating snippet → atomic journal step
  → `rollback_to 0` with no force → `replay` restored the exact geometry.

## v0.5.1 (2026-10-04)

### Fixed

- **Batch sub-ops keyed `operation` now run on initial execution, not just on
  replay** (`rpc_server.py`): `_run_one_operation` read only the RPC schema's
  `action` key, so a batch written in the journal-native `operation` style
  failed every sub-op with the cryptic `unknown action: None` while the same
  journal replayed fine. Both sides of the journal boundary now accept both
  keys, and a sub-op missing both gets an explicit "no 'action' (or
  'operation') key" error.
- **`run_all`/`replay` no longer die on non-executable journal records**
  (`step_engine.py`): an `execute_code` inspection is a normal journal citizen,
  but re-running the plan stopped hard at it ("operation 'execute_code' is not
  re-executable"), making replay unusable after any inspection. Such records
  are now marked done and skipped, reported in the result's `skipped` list.
- AST regression tests pin both contracts (`tests/test_step_journal.py`,
  `tests/test_step_engine_ops.py`); the AGENTS.md hot-reload module list now
  includes `step_journal`/`step_engine`.

## v0.5.0 (2026-10-04)

### New features

- **Step journal + steps panel** (`step_journal.py` / `step_engine.py` /
  `step_panel.py`): a FreeCAD-side review loop that survives with the RPC
  server stopped, stored on the document itself (`MCP_StepJournal`). The
  `step_control` tool and the dock share one engine: run_next/run_all/run_to,
  rollback_to, reexecute, accept/unaccept (soft lock), reject (undo and drop
  the step plus everything after it), update, insert, replay (rebuild the
  model from the journal), snapshot and reset.
- **`snapshot` verb**: marks the current state as a done+accepted baseline for
  the "the user modeled off-journal, let the LLM continue from here" flow —
  the accepted soft lock protects manual work from a blind rollback, and the
  record names what the journal missed.
- **Manual-edit sync**: GUI edits to objects a step produced are mirrored back
  into that step's parameters, so human corrections survive
  reexecute/replay — including `Placement` (so a re-run does not teleport the
  part to the origin) and, on FreeCAD ≥1.1, per-edge fillet/chamfer sizes.
- **`diagnose` tool**: cross-platform fault diagnosis that runs on the MCP
  side, so it still answers when FreeCAD is down or frozen. It probes the RPC
  endpoint, the FreeCAD process, port listeners, the addon install, the
  bootstrap crash log, the addon log's freshness and the settings, and ends
  with a verdict plus the generic 5-step order to work through.
- **`get_addon_log` tool**: reads the addon's ring-buffer/rotating debug log
  (per-RPC request ids, GUI-dispatch and transaction traces) — readable even
  when FreeCAD's GUI thread is wedged.

### Fixed

- **The addon could fail to load entirely** (`InitGui.py`): FreeCAD runs it
  with a bare `exec()` into a namespace that is not the `__globals__` of the
  functions the file defines, so the module-level `import contextlib` was
  invisible inside a nested helper. `find_addon_dir()` raised
  `NameError: name 'contextlib' is not defined`, the bootstrap died, and the
  addon silently never loaded — no workbench, no RPC server, stale log. Every
  name the nested helpers need is now imported inside `_bootstrap()`, the
  crash log falls back to the addon dir before the executable's directory, and
  a test enforces the rule with an AST scope walk.
- **Batch steps could not be re-executed** (`step_journal.py`): `cad(batch)`
  sub-ops are journaled with the RPC schema's `action` key, but re-execution
  read `operation`, so `reexecute`/`replay` undid the step and then failed
  with "operation '' is not re-executable" — leaving the model one
  transaction behind. Both keys are now accepted, on the execution side, so
  journals written by an older addon are repaired rather than rejected.
- **Step rollback ate the wrong transaction** (`step_journal.py`):
  `plan_rollback`/`plan_reject` counted non-atomic records (e.g. an
  `execute_code` inspection) toward `undo_count`, so one undo too many was
  issued and an earlier step's object disappeared. Only transaction-bearing
  records count now.
- **Phantom mouse-button state no longer starves the GUI task queue**
  (`gui_dispatch.py`): the drag guard in `process_gui_tasks` deferred all
  queued tasks whenever Qt reported a pressed mouse button. After a
  background launch (or an RDP session) Qt can report `LeftButton` with no
  real interaction, and the state cannot be cleared programmatically —
  `ping` kept answering while every GUI-dispatched call (e.g.
  `execute_code`) timed out, looking like a dropped connection. The guard
  now only defers when the main window is actually active.

### Changed

- **Tool docstrings slimmed** (13.9k → 10.6k chars across all tools): the
  per-tool descriptions the client pays for on every `tools/list` keep only a
  summary and brief Args; the full references live in `tool_docs.py` and are
  served on demand by `operation_help`. The budget test now enforces
  < 11,000 chars.

## v0.4.0 (2026-07-30)

### Renamed to CADPilot

The project has been renamed from `freecad-mcp` to **CADPilot** (AI pilots FreeCAD). All identifiers updated:

- PyPI package: `freecad-mcp` → `cadpilot`
- Python module: `freecad_mcp` → `cadpilot`
- CLI command: `uvx freecad-mcp` → `uvx cadpilot`
- FreeCAD addon directory: `FreeCADMCP` → `CADPilot`
- Workbench name: "FreeCAD MCP" → "CADPilot"
- Environment variable: `FREECAD_MCP_HOME` → `CADPILOT_HOME`
- Data directory: `~/.freecad-mcp` → `~/.cadpilot`
- Settings file: `freecad_mcp_settings.json` → `cadpilot_settings.json`
- Logger name: `FreeCADMCPserver` → `CADPilot`

### New features (since v0.3.0)

- `datum_plane` and `hull` feature operations in `cad()`
- Assembly session with persistent joints (FreeCAD 1.1 Assembly workbench)
- Connectivity auto-audit after every `cad()` mutation
- Declarative priority trimming (`trim={"winner": ...}`)

### Fixed

- **`execute_code` namespace pollution**: user code now runs in a copy of the
  addon module's globals (same as `execute_code_async`) — assignments can no
  longer corrupt the RPC server's own namespace across calls.
- **Assembly state robustness** (`assembly_state.py`): `load()` returns `None`
  on missing/corrupt/structurally-invalid session files instead of raising
  (consistent with `session_state.load_session`); `save()` is now atomic
  (tmp + replace) so a failed write can't truncate a saved session; the
  `_current` registry is guarded by a lock.
- **`assembly_session` RPC error handling** (`operations/assembly.py`):
  `start` / `add_component` / `mate` / `unmate` / `rollback` now check the
  addon result for `{"success": false}` before consuming fields — a failed
  RPC returns the addon's error message and records nothing (previously
  crashed with `KeyError` after a half-mutated state). `mate` no longer
  crashes when the result carries trim data but the call passed no `trim`.
- **Dev tooling**: the ruff config parsed invalidly (`[tool.ruff.format]
  line-length`), silently disabling all lint/format runs; fixed and the whole
  tree re-linted/reformatted (136 findings resolved).

## v0.3.0

### New features

- **Constrained sketches + PartDesign** — eight new `cad()` feature ops enabling
  the full "variables → sketch → solid → dress-up" parametric chain:
  - `variables` — create/update a Spreadsheet parameter table (`cells: {"A1": [alias, value]}`,
    idempotent); everything downstream binds to it via `=Spreadsheet.alias`.
  - `sketch` — atomic constrained sketch (`Sketcher::SketchObject` inside a
    PartDesign Body, auto-created when absent). `geometry` (line/arc/circle/bspline/point;
    list order = GeoId) + `constraints` (coincident, horizontal, vertical, tangent,
    perpendicular, parallel, equal, symmetric, distance, distance_x, distance_y,
    radius, angle) are applied in one transaction and solved immediately.
    Point references are `[geo_id, "start"|"end"|"center"|"mid"]`; `[-1, *]` is the
    origin. `plane`: "XY"/"XZ"/"YZ" (+`offset`) or `{"face": [obj, "FaceN"]}`.
    Dimensional values accept `=expressions`. Results report `dof` /
    `fully_constrained`; under-constrained sketches succeed with a warning,
    conflicting/failed ones roll back with solver diagnostics
    (`ConflictingConstraints`, `RedundantConstraints`).
  - `pad` / `pocket` — PartDesign extrusion from a sketch (closed profile
    enforced), `length`/`reversed`/`midplane`.
  - `revolution` / `groove` — PartDesign revolve, `axis` ("X"/"Y"/"Z" sketch axes
    or `{"edge": [obj, "EdgeN"]}`) and `angle`.
  - `thickness` / `draft` — dress-up ops; FreeCAD ≥1.1 LinkSub `Base` and
    ≤1.0 `Faces` property layouts both supported. `pull_direction` takes
    `{"edge": [obj, "EdgeN"]}`.
- Live verification script: `scripts/live_sketch_verify.py` (runs against a live
  FreeCAD; builds a parametric bracket and checks volumes, expression
  propagation, failure diagnostics, and undo).

## v0.2.0 (2026-07-30)

### Breaking changes

- Four standalone tools are merged into a single unified **`cad()`** tool
  (nsforge `math()`-style dispatcher, reduces resident tool context):
  - `create_object` → `cad(operation="create_object", doc_name, obj_type, obj_name, ...)`
  - `edit_object` → `cad(operation="edit_object", ...)`
  - `delete_object` → `cad(operation="delete_object", ...)`
  - `execute_operations` → `cad(operation="batch", ops=[...])`
- **Removed** (modeling-only scope):
  - `run_fem_analysis` and all `Fem::` object creation support
    (addon `fem_executor.py` gone; no CalculiX/Gmsh dependency)
  - `get_parts_list` and `insert_part_from_library` (addon `parts_library.py` gone)
  - `reload_document`
- **RPC response format**: `get_objects`, `get_object`, and `list_documents` now
  return `{"success": true, "objects"/"object"/"documents": ...}` instead of bare
  lists/dicts. The MCP client (`freecad_client.py`) handles the conversion
  transparently, so MCP tool callers see the same data — but direct XML-RPC
  consumers must adapt.
- Clients calling the removed/merged tools must switch to `cad()`.

### New features

- **Modeling sessions**: `session_start` / `session_status` / `session_get_steps` /
  `session_rollback` / `session_redo` / `session_add_note` / `session_pause` /
  `session_resume` / `session_list` / `session_complete`.
  - Every committed mutation runs inside a FreeCAD transaction; `session_rollback`
    maps to native `doc.undo()` with log truncation, `session_redo` mirrors FreeCAD
    redo semantics (a new step clears the redo buffer).
  - `execute_code` steps are non-atomic and block rollback unless `force=True`.
  - Sessions persist as JSON under `$CADPILOT_HOME/sessions/` (default
    `~/.cadpilot/sessions/`).
- **Pattern memory**: `save_pattern` / `recall_patterns` — successful workflows can be
  stored and retrieved by keyword search (CJK-safe substring matching).
  `session_complete` can archive a session as a pattern.
- **Runtime introspection**: `inspect_freecad` — inspect an object's properties/methods
  or a dotted-name API docstring without leaving the session.

### Fixed

- **Double coordinate transform in geometry queries**: FreeCAD Shapes carry the
  object's Placement as their internal location, so `BoundBox`, `CenterOfMass`,
  `Vertex.Point`, `Face.Surface` (Axis/Center) and `Face.normalAt` already return
  GLOBAL coordinates. `measure_geometry` / `get_topology` / `get_positioning_info`
  applied `obj.Placement` a second time, returning wrong positions/normals/axes for
  any moved or rotated object (e.g. a rotated fuselage reported its bbox along -Z).
  All manual placement transforms removed; `placement.rotation.angle_deg` now
  actually reports degrees (was radians).
- **`align_shapes` radian/degree bug**: `Face.getAngle()` returns radians but
  `FreeCAD.Rotation(axis, angle)` expects degrees — touch/axis modes rotated by a
  far-too-small angle. Fixed with `math.degrees()`.
- **Expression binding**: in `cad()` create/edit `obj_properties`, string values starting with `=` are bound via the ExpressionEngine (`obj.setExpression`) instead of assigned literally — Spreadsheet-driven parametric design without new tools.
- **Feature operations**: `cad()` gains `boolean`/`fillet`/`chamfer`/`loft`/`sweep`/`mirror`/`pattern` — parametric FreeCAD objects (transactional, rollback-able), with edge/face selectors ("all" / indices / names) fed by `get_topology`.
- **Geometry sensing**: `measure_geometry` (volume/area/bbox/center of mass/validity), `get_topology` (paginated faces/edges/vertices with semantic info for selection), `check_interference` (distance + common volume) — quantitative feedback after each modeling step.
- **Spatial positioning** (the hardest problem in AI-driven CAD assembly):
  - `cad(operation="move")` — relative translate/rotate on top of current Placement
    (solves ~80% of positioning needs without manual coordinate math).
  - `get_positioning_info` — global-coordinate spatial data for a specific face/edge/vertex
    (center, normal, axis, radius, start/end points — all transformed by the object's Placement).
  - `align_shapes` — move an object so one of its elements aligns with a target element on
    another object. Modes: `"touch"` (face-to-face contact, normals opposed), `"center"`
    (center-to-center), `"axis"` (cylindrical axis alignment). Optional `offset` for gap/overlap.
- **Global coordinates everywhere**: `measure_geometry` now returns bounding box and center of
  mass in global coordinates (applies Placement transform). `get_topology` face/edge/vertex
  entries now include global-coordinate data: face `radius`/`axis` (cylindrical/conical/spherical),
  edge `start`/`end` vertices and `radius`/`axis` (circular), vertex global position.
- **Guidance**: mutation responses include lightweight `display_text` suggestions and
  risk warnings (state drift after rollback, non-atomic steps, document closed).

### Bug fixes

- **Consistent edge schema**: closed (full-circle) edges in `get_topology` /
  `get_positioning_info` now always carry an `end` point (equal to `start`) —
  previously the key was absent for single-vertex edges, breaking callers that
  iterate `start`/`end` uniformly.
- **`align_shapes` silent offset**: `offset` is only meaningful in `touch` mode;
  passing a non-zero offset in `center`/`axis` mode now returns a `warning`
  field instead of silently ignoring it.
- **Dead code**: removed an always-overwritten placement computation in
  `_build_move` (`feature_ops.py`).
- **Null shape serialization**: `serialize_shape` now checks `shape.isNull()` in
  addition to `shape is None`, preventing `AttributeError` on objects whose Shape
  property exists but is a null OCCT handle.
- **Mirror feature type**: `_build_mirror` now tries `Part::Mirroring` first and
  falls back to `Part::Mirror` only on type-not-found errors, with a clear
  `ValueError` if neither exists — no more silent `TypeError` on FreeCAD builds
  that only ship one of the two.
- **Thread safety**: `_now()` in `session_state.py` wrapped with a `threading.Lock`
  to prevent rare timestamp collisions in concurrent session operations.
- **Object name normalization**: `_normalize_object_names()` handles both string
  and dict elements from different RPC code paths, fixing `objects_after`
  fingerprint mismatches in session steps.
- **Interference threshold**: `check_interference` common-volume threshold raised
  from `1e-7` to `1e-4` mm³ to avoid false positives from floating-point noise.
- **Face normals**: `get_topology` now computes normals for ALL face types (not
  just `Plane`), using `face.normalAt()` at the face center.

### Improvements

- **Screenshot policy**: screenshots are opt-in per call (`with_screenshot`).
  Precedence: `--only-text-feedback` (hard off) > per-call `with_screenshot` >
  `--with-screenshots` (default-on). Screenshots are capped at 768px on the long edge
  by default to save tokens.
- **Inline screenshots**: `execute_code` and `create_document` now capture
  screenshots in the same GUI dispatch (single RPC round trip) instead of a
  separate `get_active_screenshot` call, halving latency for screenshot-enabled
  workflows.
- **Stability**: read-only RPCs (`get_objects`, `get_object`, `list_documents`) now
  dispatch onto the FreeCAD GUI thread; the XML-RPC client retries once on
  recoverable connection errors.
- **Merged RPC**: mutations and their optional screenshot are fetched in a single
  XML-RPC round trip; mutation results include an `objects` fingerprint (sorted
  object names) used for drift detection.
- **Batch single-recompute**: `cad(operation="batch", ...)` now skips per-object
  `doc.recompute()` and performs a single recompute after all ops, significantly
  faster for large batches.
- **Boolean multi-tool**: `cad(operation="boolean", tool=["Obj1","Obj2",...])`
  now accepts a list of tool objects — they are fused into a temporary compound
  before the boolean operation, enabling multi-body cuts/fuses in one step.
- **ViewObject serialization**: extended with `DisplayMode`, `LineColor`,
  `PointSize`, `LineWidth`, and `DrawStyle` properties for richer visual feedback.
- **Compatibility**: the new MCP server falls back gracefully against older addons
  (single-shot screenshot RPC, optional params); older clients keep working against
  the new addon.
  - Tests: pytest suite (119 tests) covering responses, operations, reconnect, session
  state, pattern store, guidance, cad() dispatch, name normalization, spatial
  positioning (move, get_positioning_info, align_shapes), and global-coordinate
  topology queries.
