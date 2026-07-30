# CADPilot

**AI pilots FreeCAD.** CADPilot is an MCP (Model Context Protocol) server that gives AI clients (Cherry Studio, Claude Code, ZCode, Kimi Code, Claude Desktop, …) full control of [FreeCAD](https://www.freecad.org/): create documents, build constrained sketches, run parametric features, assemble parts with persistent joints, and verify geometry — all via tool calls over XML-RPC.

> Originally based on [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp) (MIT License).

## Installation

### Step 1: Install the FreeCAD addon

FreeCAD addon directory:

* Windows: `%APPDATA%\FreeCAD\Mod\`
* macOS:
  * FreeCAD 1.1: `~/Library/Application\ Support/FreeCAD/v1-1/Mod/`
  * FreeCAD 1.0: `~/Library/Application\ Support/FreeCAD/v1-0/Mod/`
* Linux:
  * Ubuntu: `~/.FreeCAD/Mod/` or `~/snap/freecad/common/Mod/` (if you install FreeCAD from snap)
  * Debian: `~/.local/share/FreeCAD/Mod`
  * Arch / CachyOS (FreeCAD 1.1 from `extra/freecad`): `~/.local/share/FreeCAD/v1-1/Mod/`

Copy the `addon/CADPilot` directory into the addon directory:

```bash
git clone https://github.com/LBurny/cadpilot.git
cd cadpilot

# For Linux (Ubuntu/Debian)
mkdir -p ~/.FreeCAD/Mod/
cp -r addon/CADPilot ~/.FreeCAD/Mod/

# For Linux (Arch/CachyOS, FreeCAD 1.1 from extra/freecad)
mkdir -p ~/.local/share/FreeCAD/v1-1/Mod/
cp -r addon/CADPilot ~/.local/share/FreeCAD/v1-1/Mod/

# For macOS (FreeCAD 1.1)
mkdir -p ~/Library/Application\ Support/FreeCAD/v1-1/Mod/
cp -r addon/CADPilot ~/Library/Application\ Support/FreeCAD/v1-1/Mod/

# For Windows (PowerShell)
Copy-Item -Recurse addon/CADPilot "$env:APPDATA\FreeCAD\Mod\"
```

Restart FreeCAD after installing. Select **CADPilot** from the workbench list, and start the RPC server with the **Start RPC Server** command in the **CADPilot** toolbar.

#### Auto-Start RPC Server

By default, the RPC server must be started manually each time FreeCAD opens. To start it automatically:

1. Open the **CADPilot** menu (switch to the CADPilot workbench first)
2. Check **Auto-Start Server**

The setting is saved to `cadpilot_settings.json` and persists across sessions. On the next FreeCAD launch, the RPC server will start automatically once the application finishes loading.

### Step 2: Install the MCP server

#### Recommended: uv (uvx)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first, then run the server directly with `uvx` — no explicit install needed, the package is fetched from PyPI on first use:

```bash
uvx cadpilot
```

Or install it as a persistent tool:

```bash
uv tool install cadpilot
```

#### PyPI (pip)

```bash
pip install cadpilot
cadpilot
```

The server speaks MCP on stdio and connects to FreeCAD's XML-RPC addon on `localhost:9875` — you normally don't run it by hand; your AI client launches it via the configs below.

## Client setup

All clients below use the same stdio configuration: **command** `uvx`, **args** `["cadpilot"]`.

### Cherry Studio

1. Open **设置 (Settings)** → **MCP 服务器 (MCP Servers)** → **添加服务器 (Add Server)**
2. Type: `stdio` (标准输入/输出)
3. Fill in:
   * 名称 (Name): `cadpilot`
   * 命令 (Command): `uvx`
   * 参数 (Arguments): `cadpilot`
4. Save and enable the server.

Or paste the JSON configuration directly:

```json
{
  "mcpServers": {
    "cadpilot": {
      "command": "uvx",
      "args": ["cadpilot"]
    }
  }
}
```

### Claude Code

One command:

```bash
claude mcp add cadpilot -- uvx cadpilot
```

Or add it to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "cadpilot": {
      "command": "uvx",
      "args": ["cadpilot"]
    }
  }
}
```

### ZCode

Add to `~/.zcode/cli/config.json` (user scope, applies to every workspace) under `mcp.servers`:

```json
{
  "mcp": {
    "servers": {
      "cadpilot": {
        "command": "uvx",
        "args": ["cadpilot"]
      }
    }
  }
}
```

For a single project, use `<repo>/.zcode/config.json` with the same structure. You can also manage it via **Settings → MCP** in the ZCode GUI.

### Kimi Code

Same layout as Claude Code. One command:

```bash
kimi mcp add cadpilot -- uvx cadpilot
```

Or add the server entry to Kimi Code's MCP configuration file:

```json
{
  "mcpServers": {
    "cadpilot": {
      "command": "uvx",
      "args": ["cadpilot"]
    }
  }
}
```

### Other clients (Claude Desktop, Cursor, …)

Any MCP client that supports stdio servers works with the standard snippet:

```json
{
  "mcpServers": {
    "cadpilot": {
      "command": "uvx",
      "args": ["cadpilot"]
    }
  }
}
```

### Startup options

By default, tool responses are text-only — screenshots are opt-in per call (`with_screenshot=true`) or via the `get_view` tool, which keeps token usage low and works with text-only models. Two startup flags change this:

* `--with-screenshots`: attach a screenshot to every mutation/read tool response by default (good for multimodal models).
* `--only-text-feedback`: never return screenshots, even when a call requests one (hard guarantee for text-only models).
* `--host <ip>`: connect to a FreeCAD instance on another machine (see Remote Connections).

```json
{
  "mcpServers": {
    "cadpilot": {
      "command": "uvx",
      "args": ["cadpilot", "--with-screenshots"]
    }
  }
}
```

### Developer mode

Run from a local clone instead of PyPI:

```json
{
  "mcpServers": {
    "cadpilot": {
      "command": "uv",
      "args": ["--directory", "/path/to/cadpilot/", "run", "cadpilot"]
    }
  }
}
```

## Remote Connections

By default the RPC server does not accept remote connections and listens on `localhost`. To control FreeCAD from another machine on your network:

### 1. Enable remote connections in FreeCAD

In the **CADPilot** toolbar:

1. Check **Remote Connections** — the RPC server will bind to `0.0.0.0` (all interfaces) on the next restart. For security reasons, it only accepts connections from the IP addresses or CIDR subnets specified in the **Allowed IPs** field. By default this is `127.0.0.1`.
2. Click **Configure Allowed IPs** and enter a comma-separated list of IP addresses or CIDR subnets that are allowed to connect, e.g.:

   ```
   192.168.1.100, 10.0.0.0/24
   ```

   `127.0.0.1` is always the default. Invalid entries are rejected with an error dialog. Restart the RPC server after changing these settings.

### 2. Point the MCP server at the remote host

Pass the `--host` flag with the IP address or hostname of the machine running FreeCAD:

```json
{
  "mcpServers": {
    "cadpilot": {
      "command": "uvx",
      "args": ["cadpilot", "--host", "192.168.1.100"]
    }
  }
}
```

The `--host` value is validated on startup — it must be a valid IPv4/IPv6 address or hostname.

## Tools

### Modeling

* `cad`: Unified CAD mutation tool. `operation` ∈ `create_object` / `edit_object` / `delete_object` / `batch` + feature ops `boolean` / `fillet` / `chamfer` / `loft` / `sweep` / `mirror` / `pattern` / `move` (parametric objects, edge/face selectors fed by `get_topology`, rollback-able steps). Boolean `tool` accepts a list of object names for multi-body operations. Batch operations perform a single `recompute()` after all ops for efficiency. `move` applies relative translate/rotate to the current Placement — the simplest way to reposition parts without manual coordinate math.
* `execute_code`: Execute arbitrary Python code in FreeCAD (full FreeCAD Python API, GUI-thread safe). Supports inline `with_screenshot` in a single RPC round trip. Recorded as a non-atomic session step.
* `execute_code_async`: Execute background-safe Python code (e.g. long OCCT computations); returns a `task_id`.
* `get_task_result`: Poll the status, captured `task_print()` output, and error of a background task.

### Modeling sessions (step recording + rollback)

* `session_start` / `session_pause` / `session_resume` / `session_list` / `session_complete`: Session lifecycle. Sessions persist to `~/.cadpilot/sessions/` and survive restarts. `session_complete` stores the whole workflow into the pattern store and can save the .FCStd.
* `session_status`: Step count, document state, next-step suggestions, and risks (state drift, non-atomic steps).
* `session_get_steps`: Full step log with descriptions, params, and state fingerprints.
* `session_rollback(to_step)` / `session_redo(n)`: Backtrack via FreeCAD's native transaction undo/redo — no delete-and-rebuild trial-and-error.
* `session_add_note`: Attach observations/assumptions to the log.

### Knowledge hierarchy (① own knowledge → ② pattern memory → ③ runtime introspection)

* `save_pattern` / `recall_patterns`: Store and retrieve reusable modeling workflows (`~/.cadpilot/patterns.json`).
* `inspect_freecad`: Runtime API introspection — an object's settable properties/methods, or a dotted API's docstring (e.g. `Part.makeLoft`).

### Geometry sensing (feedback loop)

* `measure_geometry`: Volume, area, bounding box, center of mass, element counts, and `is_valid` — verify design targets quantitatively after each step. All coordinates are in the global frame (Placement applied).
* `get_topology`: Faces/edges/vertices with semantic info (type, area/length, center, normal, radius, axis, start/end points), sorted by size with `limit`/`offset` pagination — the basis for face/edge selection in follow-up operations. All coordinates are global (Placement applied).
* `check_interference`: Distance and common volume between two objects — clearance/collision verification.

### Sketch mode (part design)

* `cad(operation="sketch")`: Atomic constrained sketch (geometry + constraints in one call) inside a PartDesign Body. Result reports `dof` / `fully_constrained` / solver diagnostics. Planes: `"XY"/"XZ"/"YZ"` (+offset), `{"face": [obj, "FaceN"]}`, or `{"datum": name}`. `external: [[obj, "EdgeN"|"VertexN"], ...]` adds external geometry (GeoIds from -3, start/end points only) for cross-part parametric references — out-of-body targets are bridged automatically via SubShapeBinder.
* `cad(operation="pad"/"pocket"/"revolution"/"groove"/"thickness"/"draft")`: Single-view sketch → 3D. On a face-attached sketch, pad fuses into / pocket cuts the supporting solid via attachment.
* `cad(operation="datum_plane")`: PartDesign datum plane on a base plane or existing face, with optional offset.
* `cad(operation="hull")`: Multi-view 2D→3D visual hull — draw the part's silhouette in 2-3 view sketches (Top=XY, Front=XZ, Side=YZ; one closed outer profile each); the solid is the intersection of the extruded views. Static `Part::Feature`; re-run with the same name to iterate after editing a view.
* `cad(operation="variables")`: Spreadsheet-driven parametrics; bind sketch constraints and feature lengths with `"=Spreadsheet.alias"` expressions.

### Assembly mode (persistent joints)

* `assembly_session`: Independent assembly state machine on top of FreeCAD 1.1's native Assembly workbench. Operations: `start` (ground part) → `add_component` → `mate` → `solve` → `verify` → `complete`, plus `unmate` / `rollback(to_step)` / `status`. Joints persist in the document — move a parent part and `solve` re-positions children. Mate references accept `face` / `anchor` / `point`; optional `trim={"winner": "inserted"|"base"}` performs declarative priority trimming (non-destructive, rollback-safe). `verify` reports per-joint residuals, islands, interferences, and per-mate gap profiles.

### Spatial positioning (assembly)

* `cad(operation="move")`: Relative translate/rotate on top of the current Placement — the simplest way to reposition parts. Supports `translate: {x, y, z}` and `rotate: {axis: {x, y, z}, angle: degrees}`.
* `get_positioning_info`: Global-coordinate spatial data for a specific face/edge/vertex — center, normal, axis, radius, start/end points (all transformed by the object's Placement). Essential for computing alignment offsets.
* `align_shapes`: Move an object so one of its elements aligns with a target element on another object. Modes: `"touch"` (face-to-face contact, normals opposed), `"center"` (center-to-center), `"axis"` (cylindrical axis alignment). Optional `offset` for gap/overlap.

### Inspection & documents

* `create_document`: Create a new document in FreeCAD. Supports inline `with_screenshot` in a single RPC round trip.
* `list_documents`: Get the list of open documents.
* `get_view`: Get a screenshot of the active view.
* `get_objects` / `get_object`: Get objects/properties in a document. ViewObject now includes `DisplayMode`, `LineColor`, `PointSize`, `LineWidth`, and `DrawStyle`.

Mutation operations via `cad()` capture their screenshot inline within the same RPC call when `with_screenshot=true` is passed (default: no screenshot — use `get_view` for on-demand views). Screenshots default to a downscaled size (768 px long edge) to save tokens; pass explicit `width`/`height` to `get_view` for full resolution.

## Development

```bash
git clone https://github.com/LBurny/cadpilot.git
cd cadpilot
uv sync
uv run pytest          # run the test suite
uv run cadpilot        # run the MCP server from source
```

## License

MIT — see [LICENSE](./LICENSE). Originally based on [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp) by Shirokuma (k tanaka).
