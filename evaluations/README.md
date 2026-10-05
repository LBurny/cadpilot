# CADPilot evaluations

Read-only question/answer pairs that measure whether an LLM can actually use
the CADPilot MCP server, following the mcp-builder Phase 4 process.

## Files

- `cadpilot_eval.xml` — 10 `<qa_pair>` entries. Every question is answerable
  with read-only tool calls and every answer was verified against the shipped
  server (see the table below).

## Running

Point an MCP client at the CADPilot server and ask each question, then compare
the client's answer to `<answer>` with a string match. The checks use only the
knowledge/surface tools (`operation_help`, `recall_patterns`, the tools/list
schema), so FreeCAD does **not** need to be running — the server can answer
them even when the addon is down.

## Why these questions

Each one mirrors a real decision an agent makes while modeling: which axis a
revolve takes, what a rollback needs, how big a page of topology is, how a
screenshot is delivered. They force the client to consult the server's own
reference rather than guess, which is exactly the failure mode the reference
tooling exists to prevent.

## Adding geometry questions

Geometry-level evaluations (measure a feature, find a face index, verify a
mate) need a running FreeCAD 1.1 + the CADPilot addon and a pinned reference
document. To add them:

1. Open one of the bundled examples (`examples/DeskFan.FCStd`,
   `examples/Violin.FCStd`, …) or a document you commit under `evaluations/`.
2. Run the intended read-only tool calls (measure_geometry, get_topology,
   verify_assembly, …) and record the exact outputs.
3. Add a `<qa_pair>` whose answer is a single verifiable value, and state the
   required document in the question so the check is reproducible.

Keep answers stable: derive them from a committed document, never from a
scratch model, or the evaluation rots the moment the document changes.

## Current answers (verified 2026-10-05)

| # | Answer | Source |
|---|--------|--------|
| 1 | `Y` | `operation_help("revolution")` |
| 2 | `Z` | `operation_help("revolution")` |
| 3 | `to_step` | `operation_help("session")` |
| 4 | `0.1` | `operation_help("assemble")` / `assemble` schema default |
| 5 | `50` | `get_topology` schema default (max 200) |
| 6 | `384` | `operation_help("screenshots")` / `DEFAULT_MAX_DIM` |
| 7 | `screenshots/` | `operation_help("screenshots")` |
| 8 | `22` | `cad` operation enum (`CAD_OPERATIONS`) |
| 9 | `31` | `operation_help()` topic index (`CAD_OP_DOCS`) |
| 10 | `get_view` | `operation_help("screenshots")` |
