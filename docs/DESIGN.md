# CADPilot Design Document

**English** | [Chinese](DESIGN.zh-CN.md)

CADPilot lets an AI client drive FreeCAD: it creates documents, builds constrained sketches and parametric features, assembles parts, and verifies the result with real measurements. This document describes the architecture and the reasoning behind each design decision. It is written for readers who need to understand the system, extend it, or build something similar.

## 1. Design goals

Four goals shape the design.

**Complete control.** Anything a user can do at the FreeCAD window — create and edit objects, sketch, apply features, assemble, measure — the AI should be able to do as well.

**A small context budget.** Every tool definition and every reply consumes tokens in the model's context. CADPilot keeps the tool list small, keeps docstrings short, and returns text by default, so that a long modeling session does not exhaust the context window.

**Safe experimentation.** Modeling is iterative. Every change must be reversible, so the AI can attempt an approach, undo it, and try another without rebuilding from scratch.

**A human in the loop.** The user sits at the FreeCAD window and watches the model take shape. They must be able to see what the AI did, correct it by hand, and have the AI continue from the correction. Meeting this requirement became a subsystem of its own (section 5).

## 2. Two processes

```
AI client (Claude Code, Cherry Studio, ...)
   |  MCP over stdio
   v
cadpilot MCP server (src/cadpilot, Python >= 3.12, launched by uvx)
   |  XML-RPC over TCP, localhost:9875
   v
FreeCAD addon (addon/CADPilot, living inside the FreeCAD process)
   |
   v
FreeCAD document / GUI
```

The MCP server and the addon are two separate programs communicating over XML-RPC, for three reasons.

**Isolation.** The MCP server never imports FreeCAD, so it can run anywhere, including on another machine (directed there with `--host`). When FreeCAD restarts, the server detects the dead connection, rebuilds its proxy, and retries once.

**Mismatched lifecycles.** AI clients start and stop stdio servers at will, while FreeCAD is a long-lived GUI application. The addon holds the CAD state independently of MCP server lifetimes.

**Library compatibility.** The server imports `FastMCP` from `mcp.server.fastmcp` (MCP 1.x) and falls back to `MCPServer` (MCP 2.x) if that import fails.

The XML-RPC transport times out after 150 seconds. A socket timeout is not retried, because the operation may still be executing on the FreeCAD side; retrying could apply it twice.

## 3. The single-threaded constraint

FreeCAD's document tree and GUI are not thread safe: document work must happen on the main thread, while the XML-RPC server runs on its own. The addon therefore forwards every request to the main thread; the rest of this section describes the dispatch mechanism and its edge cases.

Each call is placed on a task queue, and the RPC thread blocks waiting for the answer. Every call has its own response queue, so a call that times out can never receive the next call's answer. The main thread is woken by a Qt signal, with a timer as a fallback in case the signal is lost.

The addon defers to interactive use: while a mouse button is held or a dialog is open, queued work is postponed, so an automated change never interrupts a drag in progress. The mouse-button check exists because after a background launch or a remote desktop session, Qt can report a button as pressed when none is, and a program cannot clear that state — so queued work waited forever. The symptom was `ping` answering normally while every real call timed out, indistinguishable from a dropped connection. The deferral now applies only while the main window is genuinely active.

Errors raised inside a queued task are caught, written to FreeCAD's report view, and returned to the caller as an error message; the processing loop is never killed. A shutdown sentinel stops the timer from rescheduling itself, so the server stops cleanly.

Code submitted through `execute_code` runs in a copy of the addon's namespace, so assignments in user code cannot corrupt the server's own variables.

## 4. Tool surface design

### 4.1 One `cad()` tool instead of thirty

Every tool definition is injected into the model's context when the client lists its tools, whether or not the model ever calls it. A wide surface therefore costs context before any work begins, so all mutations go through a single `cad(operation=...)` tool, with the operation as a plain argument:

* object level: `create_object`, `edit_object`, `delete_object`, `batch`
* part features: `boolean` (several tools at once), `fillet`, `chamfer`, `loft`, `sweep`, `mirror`, `pattern`, `move`
* Sketcher and PartDesign: `variables`, `sketch`, `pad`, `pocket`, `revolution`, `groove`, `thickness`, `draft`, `datum_plane`, `hull`

### 4.2 Docstring budget

Docstrings are paid for in every conversation, so each `@mcp.tool()` docstring is a one- to three-line summary plus brief argument descriptions. The full reference for each operation lives in `tool_docs.py` and is fetched through `operation_help` when the model actually needs the detail. A test enforces a total budget of 11,000 characters, so the constraint cannot regress unnoticed.

### 4.3 The knowledge hierarchy

Prompts tell the model where to look: its own knowledge first, then `recall_patterns` (a persistent store of modeling recipes that worked before), then `inspect_freecad` (which lists an object's properties and methods, or a module's API, at runtime). A recipe that proved out is saved back with `save_pattern` or `session_complete`, so later sessions build on it.

## 5. Reversibility: transactions, sessions, and the step journal

Every committed change runs inside a FreeCAD transaction. Two journals sit on top and answer different questions: the **modeling session**, on the MCP side, spans the whole conversation; the **step journal**, on the FreeCAD side, is the review loop the user can see and drive.

### 5.1 Modeling sessions

A session (`session_start` through `session_complete`) records each change as a step: the operation, its parameters, the result, the list of objects in the document afterwards, and any notes the model or user attached. The object list is the fingerprint used to detect when the document no longer matches the log.

Rolling back to step N runs `doc.undo()` once per removed step, then truncates the log; no part of the model is deleted or rebuilt. The removed steps sit in a redo buffer until a new step arrives, which mirrors FreeCAD's own redo semantics.

Undo is only a guarantee when a step actually owns an undo entry, and a property write alone creates none. A step recorded without a transaction therefore survived rollback, which is how a rollback could once report success while the objects it was asked to drop were still present. Rollback now inspects what the undo stack actually holds and takes one of three paths:

* `native`: every step in range owns a transaction and came off the stack — nothing extra happens.
* `partial`: some steps own no undo entry — the objects those steps introduced, as recorded by the journal, are removed by name, and the reply lists the step numbers, noting that their property changes cannot be restored.
* `rebuild`: everything up to the target can be re-created from the journal — the journal-built objects are removed and steps 1..N run again, an exact restore.

Every reply states which path was taken and what was removed.

Two details keep those paths honest. First, the undo result is verified, not assumed. The FreeCAD undo stack is shared with the GUI, and a manual edit interleaved on it pops under a rollback's name while the popped count still matches, so a count that looks right can still leave the model in the wrong state. After the undo, the journal compares object sets: whatever the rolled-back steps created must be gone, and whatever the target step should have must be present. Any discrepancy escalates to the rebuild path. Second, what a cleanup removes is decided by per-step before and after diffs, never by subtracting whole snapshots. Every snapshot lists the entire document, so at the target "before the journal" a snapshot subtraction would drag in objects that predate the journal and delete the user's own work. The journal removes only what the journal built.

Every committed change is audited as well: a read-only connectivity check runs after each `cad()` call and reports parts that became disconnected from the rest. The audit only warns — it never blocks — and can be disabled globally or skipped automatically for very large documents.

Sessions and patterns are stored as JSON under `~/.cadpilot/` (or `$CADPILOT_HOME`), written through a temporary file and a rename, so a crash cannot truncate them.

### 5.1.1 Making `execute_code` undoable

The mechanism is covered separately because it decides whether code-driven modeling can be rolled back at all.

In FreeCAD, changing a property does not create an undo entry by itself; undo entries come from transactions. An AI that models through `execute_code` snippets — a common pattern, since it is the most flexible tool available — produced changes that no rollback could reach, and journal entries that blocked every rollback attempt.

The addon now wraps every snippet in a transaction and reports whether that transaction actually produced an undo entry.

* If the document changed, the run is recorded as an ordinary step that owns exactly one undo entry. The code is kept with the step, so rollback, re-run, and a full replay all work; a model built entirely from snippets replays like one built from declared operations.
* If nothing changed, the empty commit creates no undo entry, so the run is recorded as read-only: it blocks no rollback and is not re-run.
* If the snippet raises, the transaction is aborted, so a failed run leaves no partially applied changes — a second defect eliminated by the same mechanism.

Transactions do not nest: a snippet that manages its own transaction merges into the wrapper, which opens one only when none is already pending.

### 5.2 The step journal and the steps panel

A session disappears when the MCP server exits, and it is invisible to a user working in FreeCAD. The **step journal** is a second log stored on the document itself, in the `MCP_StepJournal` property, so it survives with the RPC server stopped. One engine serves two front ends:

* the `step_control` tool, with these verbs: `run_next`, `run_all`, `run_to`, `rollback_to`, `reexecute` (undo the step, then run it again with merged parameters), `accept` and `unaccept`, `reject`, `update` and `insert`, `replay` (rewind to the start and rebuild everything from the journal), `snapshot`, `reset`;
* the **Steps panel**, a dock inside FreeCAD with the step list, a progress bar, an editable parameter view, and a log console. It works with the MCP server down, including opening a saved document later and replaying how it was built.

Several rules keep this loop safe:

* Journal entries are written inside each step's transaction, but FreeCAD's undo does not restore document properties, so the journal is reconciled explicitly after every rollback and re-run. A `drift` flag, anchored on the most recent step that owns a transaction, detects a manual Ctrl+Z.
* Rollback counts only steps that own a transaction. A read-only step owns none; counting it would pop an earlier step's transaction off the undo stack.
* A read-only step does not block a rollback either, since it demonstrably changed nothing. Blocking is reserved for a step that may have changed the document without a transaction; crossing one requires the caller's explicit `force`.
* Steps the user has accepted form a soft lock: rollback, re-run, and replay refuse to cross one unless the caller passes `force=true`. `reject` is the deliberate act of destroying work, so it is not bound by the lock.
* The same operation name arrives spelled two ways: batch sub-operations are journaled exactly as the RPC schema spelled them (`action`), while steps authored for the journal use `operation`. Both the journal reader and the initial executor accept both spellings, so journals written by older addons are repaired instead of rejected.
* `run_all` and `replay` skip steps that cannot be re-executed and report the skips instead of failing the whole run. Every mutating reply also carries a compact snapshot of the journal, so callers rarely need a separate status call.

### 5.3 Two-way edits between human and AI

The journal exists so that both sides can edit. When the user corrects a dimension in FreeCAD's property panel, a document observer mirrors the change into the parameters of the step that produced that object; re-running the step then keeps the user's value instead of reverting the part to what the AI originally wrote. Placement is always tracked, because a re-run re-applies the recorded parameters and would otherwise move the part back to the origin, and angles are stored in degrees to match the property writer's convention. Feature steps use a fixed mapping of property names, with one restriction: FreeCAD 1.1 stores fillet and chamfer sizes per edge, so only a size that is uniform across all edges can be mirrored back. When several steps touch the same object, the later step owns each property.

The observer must also stay quiet while the engine itself drives the document. Undo, redo, and step re-runs fire the same property-change notifications, but they write restored or already-recorded values. Treating those as manual edits was how a re-run could revert a correction the user had made minutes earlier: the undo restored the old value, the observer mirrored that restoration back into the journal as if it were an edit, and the re-run rebuilt the part at the old value. Engine-driven windows now mute the observer; a genuine edit in the property panel never happens inside one.

The `snapshot` verb covers the other direction: work the user did entirely outside the journal. It appends a marker that is done and accepted but not re-executable, which protects the manual work from being rolled away silently, and its before and after object lists name exactly what the journal missed. The pending plan survives a snapshot, because a snapshot is a marker, not a change. The common flow is one call: review the good steps, do the complex part by hand in the GUI, then save a snapshot with `accept_done`, which accepts every completed step in the same breath, and let the AI continue from there. Rolling back to the snapshot keeps the manual work; rolling back past it requires the same explicit `force` as any accepted step.

## 6. Sketches and parametric features

A sketch is built in one atomic transaction: the geometry (whose list order defines the GeoIds used by the constraints) and the constraints are committed together and solved immediately. The reply reports the degrees of freedom, whether the sketch is fully constrained, and any solver warnings. A conflicting or malformed specification is rolled back and returns the solver's diagnostics, such as which constraints conflict.

Further details:

* `external` geometry may only reference start and end points of other objects' edges and vertices, because referencing a midpoint fails at solve time; that case is rejected up front with a clear message. Targets outside the sketch's body are bridged automatically with a `PartDesign::SubShapeBinder`, since PartDesign rejects external geometry from outside the body.
* A datum plane attaches to an origin plane or to an existing face, and sketches can attach to a datum plane.
* **Attach a sketch by direction, not by face name.** `plane={"face": [obj, "+Z"]}` (also `-Z`, `+X`, `-X`, `+Y`, `-Y`, or the words top, bottom, left, right, front, back) selects the planar face whose normal points that way, the outermost one when several qualify. FreeCAD re-derives face names after every feature, so `Face5` on one feature can denote a different face on the next; attaching by name can land the sketch on a side wall, and the pocket then removes nothing while still reporting success. As a backstop, a pocket or groove that removes no material returns a warning.
* **Patterns repeat a feature, not the body.** On a PartDesign feature, `pattern` creates a `PartDesign::PolarPattern` or `LinearPattern` inside the same body with the feature as its original, repeating that hole or rib across the part. For part-level objects (a boolean result, a primitive), a Draft array replicates the object itself. FreeCAD 1.1 cannot be driven reliably into a working PartDesign pattern from Python, so when the transform has no visible effect the operation raises, rather than returning a part with one hole where six were requested.
* **Attachment fusion.** A pad or pocket whose sketch sits on a solid's face acts on that solid directly: the pocket cuts it, the pad fuses into it. Applying a pad and then a boolean cut would be wrong here.
* `hull` builds a visual hull from two or three view-profile sketches extruded along their normals and intersected. The result is a plain `Part::Feature` that survives reload, and running it again with the same name replaces the shape in place so it can be iterated.
* Any property value given as a string starting with `=` is bound through the ExpressionEngine instead of being set literally. This is the mechanism behind spreadsheet-driven parametrics across the whole chain.

## 7. Reading geometry back

`measure_geometry`, `get_topology`, `check_interference`, and `get_positioning_info` are read-only queries over the shapes, dispatched to the GUI thread without a transaction.

Coordinates come back in global space: a shape's stored geometry already includes its placement, so no additional transform is applied (an early double-transform bug made this rule explicit). Topology listings are sorted by size and paginated, and each face and edge carries its type, center, normal, radius, or axis where relevant — the information the model needs to choose edges for a fillet or faces for a sketch. Numbers are rounded to four significant digits at the reporting boundary only; the math that positions parts uses full precision.

## 8. Positioning parts: anchors and assembly

Relative placement of parts is the hardest problem in AI-driven CAD. CADPilot addresses it with data rather than screenshots.

**Anchors** name points and directions on an object. Some are derived automatically (the bounding-box center and corners, the center of mass, the axis of the dominant cylindrical face, the centers of the largest planar faces), and the model or user can define more. Explicit anchors are stored on the object in local coordinates, so they follow it when it moves.

**`assemble`** takes a list of mates, each naming an object, an anchor, a target and a target anchor, plus a mode (`center`, `touch`, or `axis`) and an optional offset. All mates are applied in one transaction, then each anchor is resolved again at its new position to report the residual distance and angle per mate. If any mate exceeds its tolerance, the whole operation aborts.

**`align_shapes`** is the single-element version: it aligns one face, edge, or vertex of an object with a target element.

**`verify_assembly`** audits the whole document: parts that touch nothing, pairs that overlap in space, named anchor pairs checked against a tolerance, and a contact graph built from the pairs already scanned, which groups touching parts together and reports any group disconnected from the main assembly.

### 8.1 Assembly sessions with persistent joints

`assembly_session` is a separate state machine built on FreeCAD 1.1's native Assembly workbench: start with a grounded part, add components, mate, solve, verify, complete. Components are `App::Link` objects that own a placement, and the joints persist in the document, so moving a parent part and re-solving moves its children.

Several rules keep it reliable, all verified against a live FreeCAD:

* The solver's preparation pass must run before the final solve; repeated solve passes corrupt the solver's stored state.
* A component's shape is in global coordinates while joint references are in the part's local frame, so residuals are measured geometrically — distance between faces, angle between normals — never through the joint coordinate system's own math.
* A mate can name a face, an anchor, or a point; when it names a face, the nearest vertex decides where the mate lands, matching what a GUI click selects.
* `trim={"winner": ...}` performs a declarative priority trim: a non-destructive cut baked into the losing part's local frame, with the link re-pointed at the result. Every step precomputes what undoing it would take, so a rollback is a single merged request executed atomically.
* The MCP side validates the addon's result before recording anything, so a failed request never leaves a half-mutated session.

## 9. Screenshot policy

Screenshots are optional and off by default. A single call can request one; `--with-screenshots` makes them the default; `--only-text-feedback` prohibits them outright, giving text-only models a hard guarantee; `file` delivery (the default `--screenshot-mode`) writes them to `$CADPILOT_HOME/screenshots/` and returns only the path, keeping base64 out of the context, while `--screenshot-mode image` restores inline base64 blocks. Mutating tools capture the screenshot inside the same request that does the work, in one round trip, and fall back to a second call against older addons. Captures are capped at 512 pixels on the long edge unless a size is given, and a few view types, such as TechDraw and Spreadsheet, return none at all.

## 10. Long-running computations

`execute_code_async` runs code in a background thread and returns a task id. Its status, printed output, and any traceback are kept in a bounded in-memory registry (at most fifty tasks) and polled through `get_task_result`. Background code reports through `task_print` or `FreeCAD.Console` rather than `print`, because redirecting the process-wide `sys.stdout` from a thread would race with every other user of it. Background code must not touch the document or the GUI; that is the domain of the synchronous `execute_code`.

## 11. Trust and boundaries

The RPC server listens on localhost by default. Remote access is an explicit opt-in that binds to all interfaces, and when it is on, a filtered server enforces an allowlist of IP addresses or network ranges; invalid entries are rejected at configuration time.

`execute_code` is arbitrary code execution by design. It is the escape hatch that guarantees the complete control promised in section 1, and it is also why the threat model treats any client that can reach the server as fully trusted. The trade-off is deliberate: remote access is off by default, and an allowlist guards it when enabled. The `--host` value is validated at startup.

## 12. Failure handling

* **Reconnect.** The client rebuilds its proxy and retries once when the connection is dead, which covers a FreeCAD or addon restart.
* **Forward and backward compatibility.** A new client falls back to older addon behavior, and an old client keeps working against a newer addon.
* **Names are normalized.** FreeCAD rewrites object names (spaces become underscores, duplicates get numbered), so every handler returns the name FreeCAD actually chose.
* **Version differences are probed.** Where FreeCAD 1.1 and older releases store the same setting differently, the addon checks at runtime which layout applies.
* **Hot reload.** During development the addon can be reloaded without restarting FreeCAD, with a documented repair path for the rare race between server shutdown and startup.
* **Diagnosis without a live FreeCAD.** The `diagnose` tool runs entirely on the MCP side and never imports FreeCAD, so it still answers when FreeCAD is not running or is stuck. It reports the RPC endpoint, the FreeCAD process, the installed addon across every FreeCAD user directory, and the addon's bootstrap log, then ends with a concrete suggestion. Its most useful verdict: a port that is listening but does not answer a ping means the GUI thread is stuck, not that anything is misconfigured.
* **The addon log stays readable while the GUI is stuck**, because its handler deliberately bypasses the GUI thread. It is a small ring buffer plus a log file on disk, and each record is tagged with the request that caused it.
* **The bootstrap trap.** FreeCAD runs an addon's `InitGui.py` with `exec()`, which leaves functions defined in that file unable to see names imported at its top level. Any name the startup code needs is therefore imported inside the startup function itself. A crash there is written to a log file whose location `diagnose` knows how to find.

## 13. Testing

The pytest suite — about 325 tests — covers the MCP server against a fake XML-RPC connection that records every call: response shaping, screenshot policy, reconnect behavior, the session and pattern state machines, the `cad()` dispatcher, the assembly state machine including its failure paths, guidance heuristics, and the docstring budget.

The addon cannot be imported without FreeCAD, so a second layer of tests parses its source with Python's `ast` module and pins contracts that would otherwise fail silently at runtime: the namespace rule in `InitGui.py`, the signal wiring of the steps panel, the two spellings of batch sub-operation names, and the skip behavior of a replay. The diagnostics suite covers the per-platform path layouts, a live local endpoint, a closed port, and every verdict the tool can reach.

Behavior that requires a real FreeCAD is verified by `tests/live_sketch_verify.py`, an end-to-end script that builds a parametric bracket, checks that spreadsheet values propagate, inspects what a failure reports back, and confirms undo works.