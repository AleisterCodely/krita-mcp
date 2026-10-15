# Krita MCP Server

Let AI agents paint in [Krita](https://krita.org/) via the [Model Context Protocol](https://modelcontextprotocol.io/): canvases, layers and groups, strokes with Krita's real brush engine, shapes, filters, layered `.kra` saves and per-layer PNG exports for game engines.

```
MCP client (Codex, Claude, ...) ⇄ MCP server (server.py) ⇄ Krita plugin (HTTP on 127.0.0.1:5678 + token) ⇄ Krita
```

1. **Krita plugin** (`krita-plugin/`) runs inside Krita. It serves a loopback-only HTTP bridge and runs each command on Krita's main thread.
2. **MCP server** (`server.py`) is a [FastMCP](https://github.com/jlowin/fastmcp) server that turns tool calls into authenticated requests to the plugin.

Requires Krita 5.x for the native brush engine (`engine="native"`, the default). On older builds use `engine="pixel"`.

## Setup

### 1. Install the Krita plugin

Copy `krita-plugin/kritamcp/` (the whole folder) and `krita-plugin/kritamcp.desktop` into your Krita `pykrita` folder:

| OS | Path |
|----|------|
| Windows | `%APPDATA%\krita\pykrita\` |
| Linux | `~/.local/share/krita/pykrita/` |
| macOS | `~/Library/Application Support/krita/pykrita/` |

Enable it in **Settings → Configure Krita → Python Plugin Manager → Krita MCP Bridge**, then restart Krita. On first start the plugin creates `~/.config/krita-mcp/token` (on Windows: `C:\Users\<you>\.config\krita-mcp\token`).

### 2. Install the MCP server

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure your MCP client

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.krita]
command = "/path/to/krita-mcp/.venv/bin/python"   # Windows: C:\\path\\to\\krita-mcp\\.venv\\Scripts\\python.exe
args = ["/path/to/krita-mcp/server.py"]
tool_timeout_sec = 300   # saves, exports and filters on big canvases can be slow
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "krita": {
      "command": "/path/to/krita-mcp/.venv/bin/python",
      "args": ["/path/to/krita-mcp/server.py"]
    }
  }
}
```

The server reads the token file automatically. Start Krita before your agent's first Krita call.

## Security model

The plugin's HTTP bridge can paint, open files and save files, so it is locked down:

- **Loopback only:** binds to `127.0.0.1`, never all interfaces.
- **Shared-secret token:** every request needs the `X-Krita-MCP-Token` header matching `~/.config/krita-mcp/token` (created with owner-only permissions). Delete the file and restart Krita to rotate it.
- **No browsers:** requests with an `Origin` header are rejected, the `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` (blocks DNS rebinding), and POSTs must be `application/json` (blocks "simple" cross-site requests). CORS preflights are refused.
- **Path sandbox:** open/save/export only work inside the output folder (default `~/krita-mcp-output`) and folders you list in `allowed_dirs`. Symlink and `..` escapes are resolved and refused, and the token folder is always off-limits. Relative paths go to the output folder. Only image/`.kra` extensions are allowed.
- **No silent overwrites:** `krita_save` and `krita_export_layers` refuse to replace existing files unless `overwrite=true`.
- **Limits:** body size, canvas size, brush size and point counts are capped so a bad call can't hang Krita.

Any process running as your own user can still read the token. That is the same trust boundary as everything else in your account; the goal is to stop web pages and other users.

## Configuration

Optional `~/.config/krita-mcp/config.json`, read by the plugin at Krita startup:

```json
{
  "port": 5678,
  "output_dir": "~/krita-mcp-output",
  "allowed_dirs": ["~/games/my-game/assets/art"]
}
```

| Setting | Env override | Notes |
|---------|--------------|-------|
| `port` | `KRITA_MCP_PORT` | Set `KRITA_URL=http://127.0.0.1:<port>` for the MCP server too |
| `output_dir` | `KRITA_MCP_OUTPUT_DIR` | Default save/export location |
| `allowed_dirs` | `KRITA_MCP_ALLOWED_DIRS` (path-separator list, appended) | Extra folders the agent may open/save in |
| token | `KRITA_MCP_TOKEN` (server side) | Normally read from the token file |

## Tools

| Area | Tools |
|------|-------|
| Status | `krita_health`, `krita_status` (document, active layer, brush, colors, allowed folders) |
| Documents | `krita_new_canvas`, `krita_open_file`, `krita_save`, `krita_export_layers`, `krita_get_canvas`, `krita_undo`, `krita_redo` |
| Brush | `krita_set_brush` (preset, size, opacity, flow, blend mode), `krita_set_color` (fg/bg), `krita_list_brushes` |
| Painting | `krita_stroke`, `krita_draw_shape` (rectangle, ellipse, polygon, line), `krita_fill`, `krita_clear`, `krita_get_color_at` |
| Layers | `krita_list_layers`, `krita_add_layer`, `krita_set_layer`, `krita_move_layer`, `krita_duplicate_layer`, `krita_delete_layer`, `krita_merge_down` |
| Filters | `krita_list_filters`, `krita_apply_filter` |

### Strokes

`krita_stroke` takes `[[x, y], ...]` points and, for that stroke only, any of: `size`, `opacity`, `flow`, `color`, `preset`, `blend_mode`, `erase`, and `layer` (name or id). Omitted settings come from `krita_set_brush` / `krita_set_color`, and the user's brush settings are restored afterwards.

- `pressures`: one 0..1 value per point, for tapered, hand-drawn lines. `pressure_affects` chooses `size`, `opacity`, `both` or `none`.
- `smooth` (default on) draws a Catmull-Rom curve through the points, so a handful of points gives a flowing line.
- `engine="native"` (default) uses Krita's brush engine, so textured presets (pencil, crayon, pastel, chalk) keep their grain. Varying pressure is painted as short runs of equal pressure; with very transparent brushes you may see faint joins, which `pressure_affects="none"` avoids.
- `engine="pixel"` is a simple built-in soft round brush with `hardness`; use it for crisp masks or on Krita builds without the scripting paint API.

### Layers and editable sources

- Build the image in named layers and groups. `inherit_alpha` gives clipping to the layers below inside a group; `alpha_locked` keeps shading inside a shape's existing pixels.
- `krita_save("scene.kra")` keeps every layer as the editable source. PNG/JPG/WebP saves are flattened.
- `krita_export_layers(folder)` writes one PNG per top-level layer or group plus `manifest.json` (order, opacity, blend mode, position). By default each PNG is full canvas size so parallax layers line up; `crop=true` trims each layer and records its offset.
- `krita_get_canvas` returns a PNG preview the agent can see (pass `layer` to look at one layer).

## Development

```bash
pip install pytest PyQt5
QT_QPA_PLATFORM=offscreen python -m pytest tests
```

The tests cover the security checks, the pixel engine, the HTTP bridge (including browser-style and DNS-rebinding requests), and the full MCP → bridge → plugin path against an in-memory model of Krita's API (`tests/fake_krita.py`). That model is not real Krita, so check new Krita-API calls inside Krita.

## License

MIT
