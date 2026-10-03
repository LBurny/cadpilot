# CADPilot Design Document

**English** | [Chinese](DESIGN.zh-CN.md)

CADPilot lets an AI client drive FreeCAD. It creates documents, builds constrained sketches and parametric features, puts parts together, and checks the result with real measurements. This document explains how it is built and why, for anyone who wants to understand the system, change it, or build something similar.

## 1. What the design tries to achieve

Four goals shape everything else.

**Complete control.** The AI should be able to do whatever a person does at the FreeCAD window: create and edit objects, sketch, apply features, assemble, and measure.

**A small context budget.** Every tool definition and every reply costs tokens in the model's context. CADPilot keeps the tool list small, keeps docstrings short, and answers with text unless a screenshot is asked for, so a long modeling session does not drown the conversation.

**Safe experimentation.** Modeling is trial and error. Every change must be reversible, so the AI can try an approach, step back, and try another one without rebuilding from scratch.

**A human in the loop.** The user sits at the FreeCAD window and watches the model grow. They should be able to see what the AI did, correct it by hand, and let the AI continue from their corrections. Supporting that turned out to be a subsystem of its own, described in section 5.

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

The MCP server and the addon are deliberately two programs that talk over XML-RPC, for three reasons.

**Isolation.** The MCP server never imports FreeCAD, so it can run anywhere, including on another machine (point it there with `--host`). When FreeCAD restarts, the server notices the dead connection, rebuilds its proxy and retries once.

**Lifecycles that do not match.** AI clients start and stop stdio servers freely, but FreeCAD is a long lived GUI application. The addon holds the CAD state while MCP servers come and go.

**Library compatibility.** The server imports `FastMCP` from `mcp.server.fastmcp` (MCP 1.x) and falls back to `MCPServer` (MCP 2.x) if that import fails.

The XML-RPC transport times out after 150 seconds. A socket timeout is not retried, because the operation may still be running on the FreeCAD side.

## 3. FreeCAD only allows one thread

FreeCAD's document tree and GUI are not thread safe: document work must happen on the main thread, while the XML-RPC server runs on its own. The addon therefore carries every request across, and a lot of care goes into doing that safely.

A call is put on a queue and the RPC thread waits for the answer. Every call gets its own response queue, so a call that times out cannot mix its answer up with the next one's. The main thread is woken at once by a Qt signal, with a timer as a backup, in case the signal is ever missed.

The addon stays out of the user's way. While a mouse button is held, or a dialog is open, queued work waits a beat, so an automated change never interrupts the user mid drag. The mouse check has a story behind it: after a background launch or a remote desktop session, Qt can report a pressed button that nobody pressed, and since a program cannot clear that state, work waited forever. `ping` stayed alive while every real call timed out, and the whole thing looked like a dropped connection. The pause now applies only when the main window is genuinely active.

Errors inside a queued task are caught, written to FreeCAD's report view, and returned to the caller as an error message. They never kill the loop. A shutdown marker stops the timer from rescheduling itself, so stopping the server ends cleanly.

Code submitted through `execute_code` runs in a copy of the addon's namespace. Assignments in user code therefore cannot corrupt the server's own variables.

## 4. Designing the tool surface

### 4.1 One `cad()` tool instead of thirty

Every tool definition is injected into the model's context when the client lists its tools, whether or not the model ever calls them. A wide surface is expensive before any work starts, so all mutations go through a single `cad(operation=...)` tool and the operation is a plain argument:

* object level: `create_object`, `edit_object`, `delete_object`, `batch`
* part features: `boolean` (several tools at once), `fillet`, `chamfer`, `loft`, `sweep`, `mirror`, `pattern`, `move`
* Sketcher and PartDesign: `variables`, `sketch`, `pad`, `pocket`, `revolution`, `groove`, `thickness`, `draft`, `datum_plane`, `hull`

### 4.2 Docstrings are priced

Docstrings cost tokens in every conversation, so each `@mcp.tool()` docstring is a one to three line summary plus brief argument descriptions. The long reference for every operation lives in `tool_docs.py` and the model requests it from `operation_help` when it actually needs the detail. A test keeps the total docstring volume under 11,000 characters so this cannot quietly regress.

### 4.3 A knowledge hierarchy

Prompts tell the model where to look first: its own knowledge, then `recall_patterns` (a persistent store of modeling recipes that worked before), then `inspect_freecad` (which lists an object's properties and methods, or a module's API, at runtime). A recipe that worked is saved back with `save_pattern` or `session_complete`, so the next session starts smarter.

## 5. Undo everywhere: transactions, sessions, and the step journal

Every committed change runs inside a FreeCAD transaction. Two journals sit on top of that and answer different questions. The **modeling session** is on the MCP side and covers the whole conversation. The **step journal** is on the FreeCAD side and is the review loop the user can see and drive.

### 5.1 Modeling sessions

A session (`session_start` through `session_complete`) records each change as a step: what was done, the parameters, the result, the list of objects in the document afterwards, and any notes the model or user attached. That object list is the fingerprint used to detect when the document no longer matches the log.

Rolling back to step N is then `doc.undo()` N times plus truncating the log. Nothing is deleted and rebuilt. The removed steps sit in a redo buffer until a new step arrives, which mirrors how FreeCAD's own redo works.

Every committed change is also audited: a read-only connectivity check runs after each `cad()` call and reports parts that got disconnected from the rest. The audit only warns, it never blocks a change, and it can be switched off or skipped automatically for very large documents.

Sessions and patterns are stored as JSON under `~/.cadpilot/` (or `$CADPILOT_HOME`), written with a temporary file and a rename so a crash cannot truncate them.

### 5.1.1 Making `execute_code` undoable

This one is worth a section of its own, because it changes what the tool feels like day to day.

In FreeCAD, changing a property does not create an undo entry by itself. Undo entries come from transactions. An AI that models through `execute_code` snippets, which many do because it is the most flexible tool available, produced work that no rollback could reach, and filled the journal with steps that blocked every rollback attempt.

The addon now wraps every snippet in a transaction, and afterwards reports whether that transaction actually produced an undo entry.

* If the document changed, the run is recorded as an ordinary step that owns exactly one undo entry. Its code is kept with the step, so rollback, re-run, and a full replay all work. A model built entirely from snippets replays like one built from declared operations.
* If nothing changed, the empty transaction commits for free, because an empty commit creates no undo entry. The run is recorded as read-only: it blocks no rollback and is not re-run.
* If the snippet raises, the transaction is aborted. A failing run leaves no half applied changes behind, which is a second bug this fixed.

Transactions do not nest, so a snippet that manages its own transaction simply merges into the wrapper. The wrapper only opens one when none is already pending.

### 5.2 The step journal and the steps panel

A session disappears when the MCP server exits, and a person at the FreeCAD window cannot see it at all. The **step journal** is a second log that lives on the document itself, stored in the `MCP_StepJournal` property, so it survives with the RPC server stopped. One engine drives it from two front ends:

* the `step_control` tool, with these verbs: `run_next`, `run_all`, `run_to`, `rollback_to`, `reexecute` (undo the step, then run it again with merged parameters), `accept` and `unaccept`, `reject`, `update` and `insert`, `replay` (step back to the start and rebuild everything from the journal), `snapshot`, `reset`;
* the **Steps panel**, a dock inside FreeCAD with the step list, a progress bar, an editable parameter view and a log console. It works with the MCP server down, including opening a saved document later and replaying how it was built.

Several rules make this loop safe to use:

* Journal entries are written inside each step's transaction, but FreeCAD's undo does not restore document properties, so the journal is reconciled by hand after every rollback and re-run. A `drift` flag, anchored on the most recent step that owns a transaction, is what notices that the user pressed Ctrl+Z.
* The undo count only steps that own a transaction. A read-only step owns none, and counting it would pop an earlier step's transaction off the stack instead.
* A read-only step does not block a rollback either, since it provably changed nothing. A step that may have changed the document without a transaction is what blocks, and forcing past it is the caller's explicit decision.
* Steps the user has accepted are a soft lock: rollback, re-run and replay refuse to cross one unless the caller passes `force=true`. `reject` is the deliberate act of destroying work, so it crosses freely.
* The same operation name arrives spelled two ways: batch sub operations are journaled exactly as the RPC schema spelled them (`action`), while steps authored for the journal use `operation`. Both the journal reader and the initial executor accept both spellings, so old journals are repaired instead of rejected.
* `run_all` and `replay` skip steps that cannot be re-executed instead of failing the whole run, and they report the skips. Every mutating reply also carries a compact snapshot of the journal, so callers rarely need a separate status call.

### 5.3 Human and AI, editing in both directions

The journal exists so that both sides can edit. When the user corrects a dimension in FreeCAD's property panel, a document observer mirrors the change back into the parameters of the step that produced that object. Re-running the step then keeps the user's value instead of teleporting the part back to what the AI originally wrote. Placement is always tracked, because a re-run re-applies the recorded parameters and would otherwise move the part back to the origin, and angles are stored in degrees to match how the property writer expects them. Feature steps use a fixed mapping of property names, with the rule that only sizes which are the same on every edge are mirrored back, because FreeCAD stores fillet and chamfer sizes per edge. When several steps touch the same object, the later step wins.

The `snapshot` verb covers the other direction: work the user did outside the journal entirely. It appends a marker that is done and accepted but not re-executable, which protects that manual work from being rolled away blindly, and its before and after object lists name exactly what the journal missed. The pending plan survives a snapshot, because a snapshot is a marker, not a change.

## 6. Sketches and parametric features

A sketch is built in one atomic transaction: the geometry (whose list order is the GeoId used by the constraints) and the constraints together, solved immediately. The reply reports the degrees of freedom, whether the sketch is fully constrained, and any solver warnings. A conflicting or malformed specification is rolled back and returns the solver's diagnostics, such as which constraints conflict.

Details worth knowing:

* `external` geometry may only reference start and end points of other objects' edges and vertices, because referencing a midpoint fails at solve time; that case is rejected up front with a clear message. Targets outside the sketch's body are bridged automatically with a `PartDesign::SubShapeBinder`, since PartDesign rejects external geometry from nowhere else.
* A datum plane attaches to an origin plane or to an existing face, and sketches can attach to a datum plane.
* **Attach a sketch by direction, not by face name.** `plane={"face": [obj, "+Z"]}` (also `-Z`, `+X`, `-X`, `+Y`, `-Y`, or the words top, bottom, left, right, front, back) picks the planar face whose normal points that way, and the outermost one when several do. The reason is that FreeCAD re-derives face names after every feature, so `Face5` on one feature can be a different face on the next. Attaching by name can land the sketch on a side wall, and the pocket then removes nothing while still reporting success. As a backstop, a pocket or groove that removes no material at all comes back with a warning.
* **Patterns repeat a feature, not the body.** On a PartDesign feature, `pattern` creates a `PartDesign::PolarPattern` or `LinearPattern` inside the same body with the feature as its original, which repeats that hole or rib across the part. For part level objects (a boolean result, a primitive), a Draft array replicates the object itself. FreeCAD 1.1 cannot be driven reliably into a working PartDesign pattern from Python, so when the transform has no visible effect the operation refuses with a clear error instead of returning a part with one hole where six were asked for.
* **Attachment fusion.** A pad or pocket whose sketch sits on a solid's face acts on that solid directly: the pocket cuts it, the pad merges into it. Doing a pad and then a boolean cut would be the wrong move here.
* `hull` builds a visual hull from two or three view profile sketches extruded along their normals and intersected. The result is a plain `Part::Feature` that survives reload, and running it again with the same name replaces the shape in place so it can be iterated.
* Any property value given as a string starting with `=` is bound through the ExpressionEngine instead of being set literally, which is what makes spreadsheet driven parametrics work through the whole chain.

## 7. Reading the geometry back

`measure_geometry`, `get_topology`, `check_interference` and `get_positioning_info` are read only queries over the shapes, dispatched to the GUI thread without a transaction.

Coordinates come back in global space, because a shape's own placement is already part of its stored geometry, so no manual transform is applied (a double transform bug in an early version made that rule explicit). Topology listings are sorted by size and paginated, and each face and edge carries its type, center, normal, radius or axis where relevant, which is what lets the model pick sensible edges for a fillet or faces for a sketch. Numbers are rounded to four significant digits, but only when reported; the math that positions parts uses full precision.

## 8. Positioning parts: anchors and assembly

The hardest problem in AI driven CAD is putting parts in the right place relative to each other. CADPilot answers it with data rather than screenshots.

**Anchors** name points and directions on an object. Some are derived automatically (the center and corners of the bounding box, the center of mass, the axis of the dominant cylindrical face, the centers of the largest planar faces), and the model or user can define more. Explicit anchors are stored on the object in local coordinates, so they follow it when it moves.

**`assemble`** takes a list of mates, each naming an object, an anchor, a target and a target anchor, plus a mode (`center`, `touch` or `axis`) and an optional offset. All mates apply in one transaction, then each anchor is resolved again at its new position to report the residual distance and angle per mate. If any mate exceeds its tolerance, the whole thing aborts.

**`align_shapes`** does the same for a single face to face, edge or vertex alignment.

**`verify_assembly`** audits the whole document: parts that touch nothing, pairs that overlap in space, named anchor pairs checked against a tolerance, and a contact graph built from the parts it already scanned, which groups touching parts together and reports any group that is disconnected from the main assembly.

### Assembly sessions with persistent joints

`assembly_session` is a separate state machine built on FreeCAD 1.1's native Assembly workbench: start with a grounded part, add components, mate them, solve, verify, complete. Components are `App::Link` objects that own a placement, and the joints persist in the document, so moving a parent part and re-solving moves its children.

A few rules keep it reliable, all verified on a live FreeCAD:

* The solver's preparation pass must run before the final solve. Repeated solve passes corrupt the solver's stored state.
* A component's shape is in global coordinates, but joint references are in the part's local frame, so residuals are measured geometrically (distance between faces, angle between normals) and never through the joint coordinate system's own math.
* A mate can name a face, an anchor or a point; when it names a face, the nearest vertex decides where the mate lands, which is what a user means by clicking there.
* `trim={"winner": ...}` performs a declarative priority trim: a non destructive cut baked into the losing part's local frame, with the link re-pointed at the result. Every step precomputes what it would take to undo, so a rollback is a single merged request executed atomically.
* The MCP side checks the addon's result before recording anything, so a failed request never leaves a half mutated session.

## 9. Screenshots, on the model's terms

Screenshots are optional and off by default. A single call can ask for one, the `--with-screenshots` flag makes them the default, and `--only-text-feedback` forbids them entirely for text only models. Mutating tools capture the screenshot inside the same request that does the work, in one round trip; when talking to an older addon they fall back to a second call. Captures are capped at 768 pixels on the long edge unless a size is given, and a few view types, such as TechDraw and Spreadsheet, return nothing at all.

## 10. Long computations

`execute_code_async` runs code in a background thread and returns a task id. Its status, printed output and any traceback are kept in a bounded in-memory registry, capped at fifty tasks, and polled through `get_task_result`. Background code reports through `task_print` or `FreeCAD.Console` rather than `print`, because redirecting the process wide `sys.stdout` from a thread would be a race. Background code must not touch the document or the GUI; that is what the synchronous `execute_code` is for.

## 11. Trust and boundaries

The RPC server listens on localhost by default. Remote access is an explicit opt-in that binds to all interfaces, and when it is on, a filtered server enforces an allowlist of IP addresses or network ranges; invalid entries are rejected at configuration time.

`execute_code` is arbitrary code execution by design. It is the escape hatch that makes the system able to do anything, and it is also the reason the threat model treats any client that can reach the server as fully trusted. That is the trade the project makes, and it is why remote access defaults to off behind an allowlist. The `--host` value is validated at startup.

## 12. When things go wrong

* **Reconnect.** The client rebuilds its proxy and retries once when the connection is dead, which covers a FreeCAD or addon restart.
* **Old and new keep working together.** A new client falls back to older addon behavior, and an old client keeps working against a newer addon.
* **Names are sanitized.** FreeCAD rewrites object names (spaces become underscores, duplicates get numbered), so every handler returns the name FreeCAD actually chose.
* **Version differences are probed.** Where FreeCAD 1.1 and older releases store the same setting differently, the addon checks at runtime which layout applies.
* **Hot reload.** During development the addon can be reloaded without restarting FreeCAD, with a documented repair path for the rare race between shutting the server down and starting it again.
* **Diagnosis without a live FreeCAD.** The `diagnose` tool runs entirely on the MCP side and never imports FreeCAD, so it still answers when FreeCAD is not running or is stuck. It reports the RPC endpoint, the FreeCAD process, the installed addon across every FreeCAD user directory, and the addon's bootstrap log, then ends with a concrete suggestion. Its most useful judgement: a port that is listening but does not answer a ping means the GUI thread is stuck, not that anything is misconfigured.
* **The addon log stays readable while the GUI is stuck**, because its handler deliberately does not route through the GUI thread. It is a small ring buffer plus a log file on disk, and each record is tagged with the request that caused it.
* **The bootstrap trap.** FreeCAD runs an addon's `InitGui.py` with `exec()`, which leaves functions defined in that file unable to see names imported at its top level. Any name the startup code needs is therefore imported inside the startup function itself. A crash there is written to a log file, whose location `diagnose` knows how to find.

## 13. How it is tested

The pytest suite, around 325 tests, covers the MCP server against a fake XML-RPC connection that records every call: response shaping, screenshot policy, reconnect behavior, session and pattern state machines, the `cad()` dispatcher, the assembly state machine including its failure paths, guidance heuristics, and the docstring budget.

The addon cannot be imported without FreeCAD, so a second layer of tests parses its source with Python's `ast` module and pins the contracts that would otherwise fail silently at runtime: the namespace rule in `InitGui.py`, the signal wiring of the steps panel, the two spellings of batch sub operation names, and the skip behavior of a replay. The diagnostics suite covers the per platform path layouts, a live local endpoint, a closed port, and each verdict the tool can reach.

What needs a real FreeCAD is verified by `tests/live_sketch_verify.py`, an end to end script that builds a parametric bracket, checks that spreadsheet values propagate, inspects what a failure reports back, and confirms undo works.