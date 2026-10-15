"""
Krita MCP Server

Exposes Krita painting, layer and export tools over the Model Context Protocol.
Talks to the Krita plugin's loopback HTTP bridge, authenticating with the token
the plugin writes to ~/.config/krita-mcp/token (or KRITA_MCP_TOKEN).
"""

import base64
import json
import os
from typing import Literal, Optional

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

try:
    from fastmcp.utilities.types import Image
except ImportError:  # older fastmcp
    from fastmcp import Image

KRITA_URL = os.environ.get("KRITA_URL", "http://127.0.0.1:5678").rstrip("/")
CONFIG_DIR = os.environ.get("KRITA_MCP_CONFIG_DIR") or os.path.join(
    os.path.expanduser("~"), ".config", "krita-mcp")
TOKEN_HEADER = "X-Krita-MCP-Token"

# Must stay <= the plugin's COMMAND_TIMEOUT (300s).
DEFAULT_TIMEOUT = 60.0
SLOW_TIMEOUT = 300.0

Point = list[float]
BlendMode = str

mcp = FastMCP("krita-mcp")


def _token() -> str:
    token = os.environ.get("KRITA_MCP_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(os.path.join(CONFIG_DIR, "token"), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _headers() -> dict:
    token = _token()
    if not token:
        raise ToolError(
            "No Krita MCP token found. Start Krita with the plugin enabled once so it "
            "creates %s, or set KRITA_MCP_TOKEN." % os.path.join(CONFIG_DIR, "token"))
    return {TOKEN_HEADER: token}


def send_command(action: str, params: Optional[dict] = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Send a command to the Krita plugin. Raises ToolError on any failure."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        response = httpx.post(KRITA_URL, json={"action": action, "params": params},
                              headers=_headers(), timeout=timeout)
    except httpx.ConnectError:
        raise ToolError("Cannot connect to Krita. Is Krita running with the MCP plugin enabled?")
    except httpx.TimeoutException:
        raise ToolError("Krita took longer than %ds to answer" % timeout)
    try:
        data = response.json()
    except ValueError:
        raise ToolError("Unexpected response from Krita (HTTP %d)" % response.status_code)
    if response.status_code == 401:
        raise ToolError("Krita rejected the token. Restart this MCP server or check "
                        "KRITA_MCP_TOKEN matches ~/.config/krita-mcp/token.")
    if "error" in data:
        raise ToolError(data["error"])
    return data


def _json(data: dict) -> str:
    data = {k: v for k, v in data.items() if k != "status"}
    return json.dumps(data, indent=1)


# ------------------------------------------------------------------ status & documents

@mcp.tool()
def krita_health() -> str:
    """Check that Krita is running with the MCP plugin and the token works."""
    try:
        r = httpx.get(KRITA_URL + "/health", headers=_headers(), timeout=5.0)
        data = r.json()
    except httpx.HTTPError:
        return "Cannot connect to Krita. Make sure Krita is running with the MCP plugin enabled."
    if "error" in data:
        raise ToolError(data["error"])
    return "Krita is running (plugin %s, version %s)" % (data.get("plugin"), data.get("version"))


@mcp.tool()
def krita_status() -> str:
    """Current document, active layer, brush settings, colors, limits and the
    folders the plugin is allowed to read/write."""
    return _json(send_command("status"))


@mcp.tool()
def krita_new_canvas(
    width: int = 1920,
    height: int = 1080,
    name: str = "New Canvas",
    background: str = "#ffffff",
    transparent: bool = False,
    resolution: float = 300.0,
    first_layer: str = "Layer 1",
) -> str:
    """Create a new document with a 'Background' layer (filled with `background`
    unless transparent) and an empty paint layer on top, which becomes active."""
    return _json(send_command("new_canvas", locals()))


@mcp.tool()
def krita_open_file(path: str) -> str:
    """Open an image or .kra file. Path must be inside an allowed folder
    (see krita_status); relative paths are inside the output folder."""
    return _json(send_command("open_file", {"path": path}))


@mcp.tool(annotations={"destructiveHint": True})
def krita_save(path: str, overwrite: bool = False) -> str:
    """Save the document. Use .kra to keep layers as an editable source;
    .png/.jpg/.webp export a flattened image. Refuses to replace an existing
    file unless overwrite=True. Relative paths go to the output folder."""
    return _json(send_command("save", {"path": path, "overwrite": overwrite}, SLOW_TIMEOUT))


@mcp.tool(annotations={"destructiveHint": True})
def krita_export_layers(
    folder: str,
    layers: Optional[list[str]] = None,
    crop: bool = False,
    include_hidden: bool = True,
    overwrite: bool = False,
) -> str:
    """Export layers as separate PNGs plus a manifest.json (order, blend mode,
    opacity, offsets) for game engines. Defaults to every top-level layer/group,
    each at full canvas size so parallax layers stay aligned; crop=True trims
    each to its content and records x/y offsets."""
    return _json(send_command("export_layers", locals(), SLOW_TIMEOUT))


@mcp.tool()
def krita_get_canvas(preview_max: int = 1024, layer: Optional[str] = None,
                     filename: Optional[str] = None):
    """Look at the painting: returns a PNG preview (longest side <= preview_max)
    you can see. Pass `layer` to view a single layer or group. Pass `filename`
    to also export a full-resolution PNG into the output folder."""
    data = send_command("get_canvas", {"preview_max": preview_max, "layer": layer,
                                       "filename": filename}, SLOW_TIMEOUT)
    png = base64.b64decode(data.pop("png_base64"))
    return [_json(data), Image(data=png, format="png")]


@mcp.tool()
def krita_undo() -> str:
    """Undo the last action."""
    send_command("undo")
    return "Undone"


@mcp.tool()
def krita_redo() -> str:
    """Redo the last undone action."""
    send_command("redo")
    return "Redone"


# ------------------------------------------------------------------ brush & color

@mcp.tool()
def krita_set_color(color: Optional[str] = None, background: Optional[str] = None) -> str:
    """Set the foreground (paint) and/or background color as hex, e.g. '#b8a9c9'."""
    send_command("set_color", {"color": color, "background": background})
    return "Colors updated"


@mcp.tool()
def krita_set_brush(
    preset: Optional[str] = None,
    size: Optional[float] = None,
    opacity: Optional[float] = None,
    flow: Optional[float] = None,
    blend_mode: Optional[BlendMode] = None,
) -> str:
    """Set the default brush used by later strokes and shapes.

    preset: Krita brush preset name (exact or partial; see krita_list_brushes).
        Selecting a preset resets size/opacity to the preset's own values, so
        set size/opacity in the same call or after.
    size: brush diameter in pixels. opacity, flow: 0..1.
    blend_mode: Krita composite op id, e.g. normal, multiply, screen, overlay,
        soft_light, color_dodge, add, lighten, darken, erase.
    """
    return _json(send_command("set_brush", locals()))


@mcp.tool()
def krita_list_brushes(filter: str = "", limit: int = 50) -> str:
    """List brush presets whose name contains `filter` (case-insensitive).
    Useful filters for traditional media: pencil, crayon, pastel, chalk,
    charcoal, dry, bristle, texture."""
    data = send_command("list_brushes", {"filter": filter, "limit": limit})
    return "%d matching presets:\n%s" % (data["total_matches"], "\n".join(data["brushes"]))


# ------------------------------------------------------------------ painting

@mcp.tool()
def krita_stroke(
    points: list[Point],
    layer: Optional[str] = None,
    pressures: Optional[list[float]] = None,
    pressure: float = 1.0,
    pressure_affects: Literal["size", "opacity", "both", "none"] = "size",
    size: Optional[float] = None,
    opacity: Optional[float] = None,
    flow: Optional[float] = None,
    color: Optional[str] = None,
    preset: Optional[str] = None,
    blend_mode: Optional[BlendMode] = None,
    erase: bool = False,
    smooth: bool = True,
    engine: Literal["native", "pixel"] = "native",
    hardness: Optional[float] = None,
) -> str:
    """Paint one stroke through [[x, y], ...] canvas points.

    engine='native' uses Krita's real brush engine and the chosen preset, so
    textured presets (pencil, crayon, pastel, chalk) keep their grain. Any
    size/opacity/flow/color/preset/blend_mode given here applies to this stroke
    only; omitted ones come from krita_set_brush/krita_set_color.

    pressures: one 0..1 value per point for tapering (e.g. [0.2, 1, 1, 0.3]);
        otherwise `pressure` applies to the whole stroke. pressure_affects picks
        what pressure scales. Natively, varying pressure is drawn as short
        pieces of equal pressure, so very low-opacity brushes may show faint
        joins; use pressure_affects='none' for a single seamless stroke.
    smooth: draw a Catmull-Rom curve through the points (few points -> flowing line).
    erase: erase instead of paint.
    layer: layer name or id (default: active layer).
    engine='pixel' is a simple built-in soft round brush (no textures);
        `hardness` (0..1) only applies to it.
    """
    return _json(send_command("stroke", locals(), SLOW_TIMEOUT))


@mcp.tool()
def krita_draw_shape(
    shape: Literal["rectangle", "ellipse", "polygon", "line"],
    x: float = 0,
    y: float = 0,
    width: float = 100,
    height: float = 100,
    points: Optional[list[Point]] = None,
    x2: Optional[float] = None,
    y2: Optional[float] = None,
    fill: bool = True,
    outline: bool = False,
    layer: Optional[str] = None,
    color: Optional[str] = None,
    size: Optional[float] = None,
    opacity: Optional[float] = None,
    preset: Optional[str] = None,
    blend_mode: Optional[BlendMode] = None,
    erase: bool = False,
    engine: Literal["native", "pixel"] = "native",
) -> str:
    """Draw a rectangle/ellipse (x, y, width, height), a polygon (`points`, 3+)
    or a line (x, y -> x2, y2). fill uses the paint color; outline traces the
    edge with the current brush (`size` = outline width). Polygons are handy for
    blocking in silhouettes (tree masses, rocks, hills) before texturing."""
    return _json(send_command("draw_shape", locals(), SLOW_TIMEOUT))


@mcp.tool()
def krita_fill(x: float, y: float, radius: float = 50, layer: Optional[str] = None,
               color: Optional[str] = None, opacity: Optional[float] = None,
               engine: Literal["native", "pixel"] = "native") -> str:
    """Paint a filled circle centred at (x, y)."""
    return _json(send_command("fill", locals(), SLOW_TIMEOUT))


@mcp.tool(annotations={"destructiveHint": True})
def krita_clear(layer: Optional[str] = None, color: Optional[str] = None) -> str:
    """Clear a paint layer to transparent, or fill it with `color`."""
    return _json(send_command("clear", locals(), SLOW_TIMEOUT))


@mcp.tool()
def krita_get_color_at(x: int, y: int, layer: Optional[str] = None) -> str:
    """Eyedropper: color at a pixel of the whole image, or of one layer."""
    return _json(send_command("get_color_at", locals()))


# ------------------------------------------------------------------ layers

@mcp.tool()
def krita_list_layers() -> str:
    """Layer tree, top to bottom: ids, names, types, visibility, lock, alpha
    lock, inherit alpha, opacity, blend mode, bounds, and which is active."""
    return _json(send_command("list_layers"))


@mcp.tool()
def krita_add_layer(
    name: str,
    type: Literal["paint", "group"] = "paint",
    parent: Optional[str] = None,
    above: Optional[str] = None,
    below: Optional[str] = None,
    opacity: Optional[float] = None,
    blend_mode: Optional[BlendMode] = None,
    visible: Optional[bool] = None,
    alpha_locked: Optional[bool] = None,
    inherit_alpha: Optional[bool] = None,
    active: bool = True,
) -> str:
    """Add a paint layer or group. Placed at the top of `parent` (default: the
    document root) unless `above`/`below` names a sibling. New paint layers
    become active. inherit_alpha=True clips the layer to the layers beneath it
    in its group (Krita's clipping)."""
    return _json(send_command("add_layer", locals()))


@mcp.tool()
def krita_set_layer(
    layer: str,
    new_name: Optional[str] = None,
    opacity: Optional[float] = None,
    blend_mode: Optional[BlendMode] = None,
    visible: Optional[bool] = None,
    locked: Optional[bool] = None,
    alpha_locked: Optional[bool] = None,
    inherit_alpha: Optional[bool] = None,
    color_label: Optional[int] = None,
    active: Optional[bool] = None,
) -> str:
    """Change layer properties, or make it the active layer. alpha_locked=True
    keeps painting inside the layer's existing pixels (shading a shape without
    spilling outside it)."""
    return _json(send_command("set_layer", locals()))


@mcp.tool()
def krita_move_layer(layer: str, parent: Optional[str] = None,
                     above: Optional[str] = None, below: Optional[str] = None) -> str:
    """Move a layer into `parent` (group; default root), directly above/below a
    sibling, or to the top of the parent if neither is given."""
    return _json(send_command("move_layer", locals()))


@mcp.tool()
def krita_duplicate_layer(layer: str, name: Optional[str] = None) -> str:
    """Duplicate a layer (placed directly above the original)."""
    return _json(send_command("duplicate_layer", locals()))


@mcp.tool(annotations={"destructiveHint": True})
def krita_delete_layer(layer: str) -> str:
    """Delete a layer or group."""
    return _json(send_command("delete_layer", locals()))


@mcp.tool(annotations={"destructiveHint": True})
def krita_merge_down(layer: Optional[str] = None) -> str:
    """Merge a layer into the one below it."""
    return _json(send_command("merge_down", locals()))


# ------------------------------------------------------------------ filters

@mcp.tool()
def krita_list_filters(name: Optional[str] = None) -> str:
    """List Krita filter ids, or pass `name` to see that filter's settings."""
    return _json(send_command("list_filters", locals()))


@mcp.tool()
def krita_apply_filter(name: str, layer: Optional[str] = None,
                       properties: Optional[dict] = None,
                       region: Optional[list[int]] = None) -> str:
    """Apply a Krita filter (e.g. 'noise', 'blur', 'gaussian blur', 'oilpaint',
    'hsvadjustment') to a paint layer. `properties` overrides settings from
    krita_list_filters(name); `region` is [x, y, width, height]."""
    return _json(send_command("apply_filter", locals(), SLOW_TIMEOUT))


if __name__ == "__main__":
    mcp.run()
