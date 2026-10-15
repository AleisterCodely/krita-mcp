"""
Krita-side command implementations. Everything here runs on Krita's main thread.
"""

import base64
import json
import os
import re

from krita import Extension, InfoObject, Krita, ManagedColor
from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, QTimer
from PyQt5.QtGui import QColor, QPainterPath

from . import raster, security
from .bridge import BridgeServer, CommandQueue

VERSION = "2.0.0"

LIMITS = {
    "max_side": 8192,
    "max_pixels": 50000000,
    "max_points": 5000,
    "max_brush": 1000,
    "max_pixel_engine_brush": 300,
    "max_preview": 2048,
}

LAYER_TYPES = {"paint": "paintlayer", "group": "grouplayer"}
ENGINES = ("native", "pixel")
PRESSURE_AFFECTS = ("size", "opacity", "both", "none")


class CommandError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def _num(params, key, default=None, lo=None, hi=None, integer=False):
    value = params.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError("%s must be a number" % key)
    if integer:
        value = int(round(value))
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise CommandError("%s must be between %s and %s" % (key, lo, hi))
    return value


def _points(raw, min_n=2):
    if not isinstance(raw, list) or len(raw) < min_n:
        raise CommandError("points must be a list of at least %d [x, y] pairs" % min_n)
    if len(raw) > LIMITS["max_points"]:
        raise CommandError("Too many points (max %d)" % LIMITS["max_points"])
    out = []
    for p in raw:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)):
            raise CommandError("Each point must be [x, y] numbers")
        out.append((float(p[0]), float(p[1])))
    return out


def _qcolor(value, key="color"):
    if not isinstance(value, str):
        raise CommandError("%s must be a hex string like #b8a9c9" % key)
    color = QColor(value)
    if not color.isValid():
        raise CommandError("Invalid %s: %r" % (key, value))
    return color


def _node_id(node):
    return node.uniqueId().toString().strip("{}").lower()


def _iter_nodes(node):
    for child in node.childNodes():
        yield child
        for sub in _iter_nodes(child):
            yield sub


def _slug(text):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", text).strip("_").lower() or "layer"


def _png_base64(qimage):
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    qimage.save(buf, "PNG")
    buf.close()
    return base64.b64encode(bytes(data)).decode("ascii")


class BrushOverride:
    """Temporarily applies brush settings and restores the user's afterwards."""

    def __init__(self, view, preset=None, size=None, opacity=None, flow=None,
                 blend_mode=None, color=None):
        self.view = view
        self.preset, self.size, self.opacity, self.flow = preset, size, opacity, flow
        self.blend_mode, self.color = blend_mode, color
        self.saved = None

    def __enter__(self):
        v = self.view
        self.saved = {
            "preset": v.currentBrushPreset(),
            "size": v.brushSize(),
            "opacity": v.paintingOpacity(),
            "flow": v.paintingFlow(),
            "blend": v.currentBlendingMode(),
            "fg": v.foregroundColor(),
        }
        if self.preset is not None:
            v.setCurrentBrushPreset(self.preset)  # first: presets reset size/opacity
        if self.size is not None:
            v.setBrushSize(self.size)
        if self.opacity is not None:
            v.setPaintingOpacity(self.opacity)
        if self.flow is not None:
            v.setPaintingFlow(self.flow)
        if self.blend_mode is not None:
            v.setCurrentBlendingMode(self.blend_mode)
        if self.color is not None:
            v.setForeGroundColor(ManagedColor.fromQColor(self.color, v.canvas()))
        return self

    def __exit__(self, *exc):
        v, s = self.view, self.saved
        if self.preset is not None and s["preset"] is not None:
            v.setCurrentBrushPreset(s["preset"])
        v.setBrushSize(s["size"])
        v.setPaintingOpacity(s["opacity"])
        v.setPaintingFlow(s["flow"])
        v.setCurrentBlendingMode(s["blend"])
        v.setForeGroundColor(s["fg"])
        return False


# --------------------------------------------------------------------------- extension

class KritaMCPExtension(Extension):

    def __init__(self, parent):
        super().__init__(parent)
        self.queue = CommandQueue()
        self.server = None
        self.timer = None
        self.policy = None
        self.config = None
        self.startup_error = None

    def setup(self):
        pass

    def createActions(self, window):
        if self.server is not None or self.startup_error is not None:
            return
        try:
            self.config = security.load_config()
            token = security.ensure_token(self.config["config_dir"])
            self.policy = security.PathPolicy(
                self.config["output_dir"], self.config["allowed_dirs"],
                forbidden_dirs=[self.config["config_dir"]],
            )
            os.makedirs(self.policy.output_dir, exist_ok=True)
            self.server = BridgeServer(self.config["port"], token, self.queue,
                                       commands=self.commands(), version=VERSION)
            self.server.start()
            print("[KritaMCP] listening on 127.0.0.1:%d" % self.config["port"])
        except Exception as exc:  # keep Krita usable even if the bridge can't start
            self.startup_error = str(exc)
            print("[KritaMCP] bridge disabled: %s" % exc)
            return

        self.timer = QTimer()
        self.timer.timeout.connect(self.process_commands)
        self.timer.start(30)

    @classmethod
    def commands(cls):
        return sorted(name[4:] for name in dir(cls) if name.startswith("cmd_"))

    def process_commands(self):
        item = self.queue.pop()
        if item is None:
            return
        command_id, command = item
        handler = getattr(self, "cmd_" + command["action"], None)
        try:
            if handler is None:
                raise CommandError("Unknown action")
            result = handler(command["params"])
        except (CommandError, security.SecurityError) as exc:
            result = {"error": str(exc)}
        except Exception as exc:
            result = {"error": "%s: %s" % (type(exc).__name__, exc)}
        self.queue.set_result(command_id, result)

    # ------------------------------------------------------------------ context

    def _doc(self):
        doc = Krita.instance().activeDocument()
        if doc is None:
            raise CommandError("No active document. Use krita_new_canvas or krita_open_file.")
        return doc

    def _view(self):
        window = Krita.instance().activeWindow()
        view = window.activeView() if window else None
        if view is None:
            raise CommandError("No active view")
        return view

    def _layer(self, doc, ref=None):
        if ref is None:
            node = doc.activeNode()
            if node is None:
                raise CommandError("No active layer")
            return node
        ref = str(ref).strip()
        ref_id = ref.strip("{}").lower()
        by_name = []
        for node in _iter_nodes(doc.rootNode()):
            if _node_id(node) == ref_id:
                return node
            if node.name() == ref:
                by_name.append(node)
        if not by_name:
            raise CommandError("Layer not found: %s" % ref)
        if len(by_name) > 1:
            raise CommandError("Layer name %r is used %d times; pass its id from "
                               "krita_list_layers instead" % (ref, len(by_name)))
        return by_name[0]

    def _paint_layer(self, doc, ref=None):
        node = self._layer(doc, ref)
        if node.type() != "paintlayer":
            raise CommandError("Layer %r is a %s; painting needs a paint layer"
                               % (node.name(), node.type()))
        if node.locked():
            raise CommandError("Layer %r is locked" % node.name())
        return node

    def _preset(self, name):
        presets = Krita.instance().resources("preset")
        if name in presets:
            return presets[name]
        for key, preset in presets.items():
            if name.lower() in key.lower():
                return preset
        raise CommandError("Brush preset not found: %s (use krita_list_brushes)" % name)

    @staticmethod
    def _native_ok(node):
        return hasattr(node, "paintPath")

    def _fill_bgra(self, node, w, h, bgra):
        rows = max(1, 4000000 // max(1, w))  # ~16 MB per chunk
        for y in range(0, h, rows):
            hh = min(rows, h - y)
            node.setPixelData(bytes(bgra) * (w * hh), 0, y, w, hh)

    def _layer_info(self, node, active_id):
        info = {
            "id": _node_id(node),
            "name": node.name(),
            "type": node.type(),
            "visible": node.visible(),
            "locked": node.locked(),
            "alpha_locked": node.alphaLocked(),
            "inherit_alpha": node.inheritAlpha(),
            "opacity": round(node.opacity() / 255.0, 3),
            "blend_mode": node.blendingMode(),
            "active": _node_id(node) == active_id,
        }
        b = node.bounds()
        info["bounds"] = [b.x(), b.y(), b.width(), b.height()]
        children = node.childNodes()
        if node.type() == "grouplayer":
            # top of the stack first, like Krita's Layers docker
            info["children"] = [self._layer_info(c, active_id) for c in reversed(children)]
        return info

    def _place(self, parent, node, above=None, below=None):
        """Insert `node` into `parent` above/below a sibling (default: top)."""
        siblings = [c for c in parent.childNodes()]
        if above is not None:
            parent.addChildNode(node, above)
            return
        if below is not None:
            ids = [_node_id(c) for c in siblings]
            idx = ids.index(_node_id(below))
            if idx > 0:
                parent.addChildNode(node, siblings[idx - 1])
                return
            # Put at the very bottom: add on top, then lift everything else above it.
            parent.addChildNode(node, None)
            anchor = node
            for sib in siblings:
                if _node_id(sib) == _node_id(node):
                    continue
                sib.remove()
                parent.addChildNode(sib, anchor)
                anchor = sib
            return
        parent.addChildNode(node, None)

    def _parent_and_neighbour(self, doc, params):
        parent = doc.rootNode()
        if params.get("parent") is not None:
            parent = self._layer(doc, params["parent"])
            if parent.type() != "grouplayer":
                raise CommandError("parent must be a group layer")
        above = below = None
        if params.get("above") is not None:
            above = self._layer(doc, params["above"])
        if params.get("below") is not None:
            below = self._layer(doc, params["below"])
        for ref in (above, below):
            if ref is not None and _node_id(ref.parentNode()) != _node_id(parent):
                raise CommandError("above/below layer must be inside the target parent")
        return parent, above, below

    def _apply_layer_props(self, node, params):
        if "name" in params and params["name"] is not None:
            node.setName(str(params["name"]))
        if params.get("opacity") is not None:
            node.setOpacity(int(round(_num(params, "opacity", lo=0, hi=1) * 255)))
        if params.get("blend_mode") is not None:
            node.setBlendingMode(str(params["blend_mode"]))
        for key, setter in (("visible", node.setVisible), ("locked", node.setLocked),
                            ("alpha_locked", node.setAlphaLocked),
                            ("inherit_alpha", node.setInheritAlpha)):
            if params.get(key) is not None:
                setter(bool(params[key]))
        if params.get("color_label") is not None:
            node.setColorLabel(_num(params, "color_label", lo=0, hi=8, integer=True))

    def _refresh(self, doc):
        doc.refreshProjection()

    # ------------------------------------------------------------------ document

    def cmd_status(self, params):
        out = {"version": VERSION, "limits": LIMITS,
               "output_dir": self.policy.output_dir, "allowed_dirs": self.policy.roots}
        doc = Krita.instance().activeDocument()
        if doc is not None:
            active = doc.activeNode()
            out["document"] = {
                "name": doc.name(), "file": doc.fileName(),
                "width": doc.width(), "height": doc.height(),
                "resolution": doc.resolution(),
                "active_layer": active.name() if active else None,
                "active_layer_id": _node_id(active) if active else None,
            }
        window = Krita.instance().activeWindow()
        view = window.activeView() if window else None
        if view is not None:
            preset = view.currentBrushPreset()
            out["brush"] = {
                "preset": preset.name() if preset else None,
                "size": view.brushSize(),
                "opacity": round(view.paintingOpacity(), 3),
                "flow": round(view.paintingFlow(), 3),
                "blend_mode": view.currentBlendingMode(),
                "foreground": view.foregroundColor().colorForCanvas(view.canvas()).name(),
                "background": view.backgroundColor().colorForCanvas(view.canvas()).name(),
            }
        return out

    def cmd_new_canvas(self, params):
        w = _num(params, "width", 1920, 1, LIMITS["max_side"], integer=True)
        h = _num(params, "height", 1080, 1, LIMITS["max_side"], integer=True)
        if w * h > LIMITS["max_pixels"]:
            raise CommandError("Canvas too large (max %d pixels)" % LIMITS["max_pixels"])
        res = _num(params, "resolution", 300.0, 1, 2400)
        name = str(params.get("name") or "New Canvas")
        transparent = bool(params.get("transparent", False))
        bg = _qcolor(params.get("background", "#ffffff"), "background")

        app = Krita.instance()
        doc = app.createDocument(w, h, name, "RGBA", "U8", "", float(res))
        window = app.activeWindow()
        if window is None:
            raise CommandError("No Krita window is open")
        window.addView(doc)

        root = doc.rootNode()
        kids = [k for k in root.childNodes() if k.type() == "paintlayer"]
        background = kids[0] if kids else doc.createNode("Background", "paintlayer")
        if not kids:
            root.addChildNode(background, None)
        background.setName("Background")
        if not transparent:
            self._fill_bgra(background, w, h, (bg.blue(), bg.green(), bg.red(), 255))

        paint = doc.createNode(str(params.get("first_layer") or "Layer 1"), "paintlayer")
        root.addChildNode(paint, None)
        doc.setActiveNode(paint)
        self._refresh(doc)
        return {"status": "ok", "width": w, "height": h, "active_layer": paint.name(),
                "active_layer_id": _node_id(paint)}

    def cmd_open_file(self, params):
        path = self.policy.resolve(params.get("path"), must_exist=True,
                                   extensions=security.OPEN_EXTENSIONS)
        app = Krita.instance()
        doc = app.openDocument(path)
        if doc is None:
            raise CommandError("Krita could not open %s" % path)
        window = app.activeWindow()
        if window:
            window.addView(doc)
        return {"status": "ok", "path": path, "name": doc.name(),
                "width": doc.width(), "height": doc.height()}

    def cmd_save(self, params):
        doc = self._doc()
        path = self.policy.resolve(params.get("path"), extensions=security.SAVE_EXTENSIONS,
                                   allow_overwrite=bool(params.get("overwrite", False)))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc.waitForDone()
        doc.setBatchmode(True)
        try:
            if path.lower().endswith(".kra"):
                ok = doc.saveAs(path)  # layered, editable source
            else:
                ok = doc.exportImage(path, InfoObject())  # flattened
        finally:
            doc.setBatchmode(False)
        if not ok:
            raise CommandError("Krita failed to save %s" % path)
        return {"status": "ok", "path": path,
                "layered": path.lower().endswith((".kra", ".psd", ".ora", ".tif", ".tiff"))}

    def cmd_export_layers(self, params):
        doc = self._doc()
        folder = self.policy.resolve(params.get("folder"), directory=True)
        overwrite = bool(params.get("overwrite", False))
        crop = bool(params.get("crop", False))
        include_hidden = bool(params.get("include_hidden", True))

        if params.get("layers"):
            if not isinstance(params["layers"], list):
                raise CommandError("layers must be a list of names or ids")
            nodes = [self._layer(doc, ref) for ref in params["layers"]]
        else:
            nodes = list(doc.rootNode().childNodes())  # top level, bottom to top
        if not include_hidden:
            nodes = [n for n in nodes if n.visible()]

        plan = []
        for order, node in enumerate(nodes):
            filename = "%02d_%s.png" % (order, _slug(node.name()))
            path = self.policy.resolve(os.path.join(folder, filename), extensions={".png"},
                                       allow_overwrite=overwrite)
            plan.append((order, node, filename, path))
        manifest_path = self.policy.resolve(os.path.join(folder, "manifest.json"),
                                            extensions={".json"}, allow_overwrite=overwrite)

        os.makedirs(folder, exist_ok=True)
        doc.waitForDone()
        full = doc.bounds()
        entries, skipped = [], []
        for order, node, filename, path in plan:
            rect = node.bounds() if crop else full
            if rect.isEmpty():
                skipped.append(node.name())
                continue
            try:
                ok = node.save(path, doc.xRes(), doc.yRes(), InfoObject(), rect)
            except TypeError:  # older Krita: no export rect support
                ok = node.save(path, doc.xRes(), doc.yRes())
                rect = full
            if not ok:
                raise CommandError("Failed to export layer %r" % node.name())
            entries.append({
                "order": order, "name": node.name(), "id": _node_id(node),
                "file": filename, "type": node.type(), "visible": node.visible(),
                "opacity": round(node.opacity() / 255.0, 3),
                "blend_mode": node.blendingMode(),
                "x": rect.x(), "y": rect.y(), "width": rect.width(), "height": rect.height(),
            })

        manifest = {"canvas": {"width": doc.width(), "height": doc.height()},
                    "order": "bottom_to_top", "layers": entries}
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        return {"status": "ok", "folder": folder, "exported": len(entries),
                "skipped_empty": skipped, "manifest": manifest_path}

    def cmd_get_canvas(self, params):
        doc = self._doc()
        doc.waitForDone()
        self._refresh(doc)
        max_side = _num(params, "preview_max", 1024, 64, LIMITS["max_preview"], integer=True)
        target = self._layer(doc, params["layer"]) if params.get("layer") else doc
        w, h = doc.width(), doc.height()
        scale = min(1.0, float(max_side) / max(w, h))
        tw, th = max(1, int(w * scale)), max(1, int(h * scale))
        image = target.thumbnail(tw, th)
        out = {"status": "ok", "width": w, "height": h, "preview_width": tw,
               "preview_height": th, "png_base64": _png_base64(image)}

        if params.get("filename"):
            name = str(params["filename"])
            if not name.lower().endswith(".png"):
                name += ".png"
            path = self.policy.resolve(name, extensions={".png"}, allow_overwrite=True)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            doc.setBatchmode(True)
            try:
                doc.exportImage(path, InfoObject())
            finally:
                doc.setBatchmode(False)
            out["path"] = path
        return out

    def cmd_undo(self, params):
        action = Krita.instance().action("edit_undo")
        if action is None:
            raise CommandError("Undo action unavailable")
        action.trigger()
        return {"status": "ok"}

    def cmd_redo(self, params):
        action = Krita.instance().action("edit_redo")
        if action is None:
            raise CommandError("Redo action unavailable")
        action.trigger()
        return {"status": "ok"}

    # ------------------------------------------------------------------ brush

    def cmd_set_color(self, params):
        view = self._view()
        if params.get("color") is not None:
            view.setForeGroundColor(ManagedColor.fromQColor(_qcolor(params["color"]), view.canvas()))
        if params.get("background") is not None:
            view.setBackGroundColor(
                ManagedColor.fromQColor(_qcolor(params["background"], "background"), view.canvas()))
        return {"status": "ok"}

    def cmd_set_brush(self, params):
        view = self._view()
        if params.get("preset"):
            view.setCurrentBrushPreset(self._preset(str(params["preset"])))
        if params.get("size") is not None:
            view.setBrushSize(_num(params, "size", lo=1, hi=LIMITS["max_brush"]))
        if params.get("opacity") is not None:
            view.setPaintingOpacity(_num(params, "opacity", lo=0, hi=1))
        if params.get("flow") is not None:
            view.setPaintingFlow(_num(params, "flow", lo=0, hi=1))
        if params.get("blend_mode") is not None:
            view.setCurrentBlendingMode(str(params["blend_mode"]))
        return self.cmd_status({}).get("brush", {"status": "ok"})

    def cmd_list_brushes(self, params):
        text = str(params.get("filter") or "").lower()
        limit = _num(params, "limit", 50, 1, 1000, integer=True)
        names = [n for n in sorted(Krita.instance().resources("preset").keys())
                 if text in n.lower()]
        return {"status": "ok", "brushes": names[:limit], "total_matches": len(names)}

    # ------------------------------------------------------------------ painting

    def _stroke_overrides(self, params, view, erase):
        preset = self._preset(str(params["preset"])) if params.get("preset") else None
        color = _qcolor(params["color"]) if params.get("color") else None
        blend = "erase" if erase else params.get("blend_mode")
        return BrushOverride(
            view, preset=preset,
            size=_num(params, "size", lo=1, hi=LIMITS["max_brush"]),
            opacity=_num(params, "opacity", lo=0, hi=1),
            flow=_num(params, "flow", lo=0, hi=1),
            blend_mode=str(blend) if blend else None, color=color,
        )

    def _engine(self, params, node):
        engine = params.get("engine", "native")
        if engine not in ENGINES:
            raise CommandError("engine must be one of %s" % ", ".join(ENGINES))
        if engine == "native" and not self._native_ok(node):
            raise CommandError("This Krita build has no scripting paint API (needs Krita 5+); "
                               "use engine='pixel'")
        return engine

    def _pixel_paint(self, doc, node, bbox, mask_fn, color, opacity, erase):
        x0, y0, w, h = bbox
        pixels = bytearray(node.pixelData(x0, y0, w, h))
        raster.composite(pixels, mask_fn(bbox), (color.red(), color.green(), color.blue()),
                         opacity, erase)
        node.setPixelData(bytes(pixels), x0, y0, w, h)

    def _view_color(self, view, params):
        if params.get("color"):
            return _qcolor(params["color"])
        return view.foregroundColor().colorForCanvas(view.canvas())

    def cmd_stroke(self, params):
        doc, view = self._doc(), self._view()
        node = self._paint_layer(doc, params.get("layer"))
        points = _points(params.get("points"))

        pressures = params.get("pressures")
        if pressures is not None:
            if (not isinstance(pressures, list) or len(pressures) != len(points)
                    or not all(isinstance(p, (int, float)) and 0 <= p <= 1 for p in pressures)):
                raise CommandError("pressures must list one 0..1 value per point")
        else:
            pressures = [_num(params, "pressure", 1.0, 0, 1)] * len(points)

        affects = params.get("pressure_affects", "size")
        if affects not in PRESSURE_AFFECTS:
            raise CommandError("pressure_affects must be one of %s" % ", ".join(PRESSURE_AFFECTS))
        if affects == "none":
            pressures = [1.0] * len(points)
        erase = bool(params.get("erase", False))
        smooth = bool(params.get("smooth", True))
        engine = self._engine(params, node)

        if engine == "native":
            with self._stroke_overrides(params, view, erase):
                base_size, base_opacity = view.brushSize(), view.paintingOpacity()
                samples = raster.smooth_polyline(points, pressures, 3.0, smooth)
                for pressure, pts in raster.pressure_runs(samples):
                    if pressure <= 0.01:
                        continue
                    if affects in ("size", "both"):
                        view.setBrushSize(max(1.0, base_size * pressure))
                    if affects in ("opacity", "both"):
                        view.setPaintingOpacity(max(0.01, base_opacity * pressure))
                    if len(pts) == 1:
                        pts = [pts[0], (pts[0][0] + 0.01, pts[0][1])]
                    path = QPainterPath(QPointF(*pts[0]))
                    for x, y in pts[1:]:
                        path.lineTo(QPointF(x, y))
                    node.paintPath(path, "ForegroundColor", "None")
        else:
            size = _num(params, "size", None, 1, LIMITS["max_pixel_engine_brush"])
            if size is None:
                size = min(view.brushSize(), LIMITS["max_pixel_engine_brush"])
            radius = max(0.5, size / 2.0)
            hardness = _num(params, "hardness", 0.5, 0, 1)
            opacity = _num(params, "opacity", None, 0, 1)
            if opacity is None:
                opacity = view.paintingOpacity()
            samples = raster.smooth_polyline(points, pressures, max(1.0, radius / 4), smooth)
            pad = radius + 2
            bbox = raster.clamp_bbox(min(s[0] for s in samples) - pad, min(s[1] for s in samples) - pad,
                                     max(s[0] for s in samples) + pad, max(s[1] for s in samples) + pad,
                                     doc.width(), doc.height())
            if bbox is None:
                raise CommandError("Stroke is entirely outside the canvas")
            self._pixel_paint(doc, node, bbox,
                              lambda b: raster.stroke_mask(samples, radius, hardness, affects, b),
                              self._view_color(view, params), opacity, erase)

        self._refresh(doc)
        return {"status": "ok", "engine": engine, "layer": node.name(), "points": len(points)}

    def cmd_draw_shape(self, params):
        doc, view = self._doc(), self._view()
        node = self._paint_layer(doc, params.get("layer"))
        shape = params.get("shape", "rectangle")
        fill = bool(params.get("fill", True))
        outline = bool(params.get("outline", params.get("stroke", False)))
        erase = bool(params.get("erase", False))
        if shape not in ("rectangle", "ellipse", "polygon", "line"):
            raise CommandError("shape must be rectangle, ellipse, polygon or line")
        if shape == "line":
            fill, outline = False, True
        if not fill and not outline:
            raise CommandError("Nothing to draw: set fill and/or outline")

        x = _num(params, "x", 0)
        y = _num(params, "y", 0)
        w = _num(params, "width", 100, 0)
        h = _num(params, "height", 100, 0)
        if shape == "polygon":
            poly = _points(params.get("points"), min_n=3)
        elif shape == "line":
            poly = [(x, y), (_num(params, "x2", x + w), _num(params, "y2", y + h))]
        elif shape == "ellipse":
            poly = raster.ellipse_polygon(x, y, w, h)
        else:
            poly = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]

        engine = self._engine(params, node)
        if engine == "native":
            stroke_style = "ForegroundColor" if outline else "None"
            fill_style = "ForegroundColor" if fill else "None"
            with self._stroke_overrides(params, view, erase):
                if shape == "rectangle":
                    node.paintRectangle(QRectF(x, y, w, h), stroke_style, fill_style)
                elif shape == "ellipse":
                    node.paintEllipse(QRectF(x, y, w, h), stroke_style, fill_style)
                elif shape == "polygon":
                    node.paintPolygon([QPointF(px, py) for px, py in poly], stroke_style, fill_style)
                else:
                    node.paintLine(QPointF(*poly[0]), QPointF(*poly[1]))
        else:
            color = self._view_color(view, params)
            opacity = _num(params, "opacity", None, 0, 1)
            if opacity is None:
                opacity = view.paintingOpacity()
            size = _num(params, "size", 2, 1, LIMITS["max_pixel_engine_brush"])
            pad = size / 2.0 + 2
            bbox = raster.clamp_bbox(min(p[0] for p in poly) - pad, min(p[1] for p in poly) - pad,
                                     max(p[0] for p in poly) + pad, max(p[1] for p in poly) + pad,
                                     doc.width(), doc.height())
            if bbox is None:
                raise CommandError("Shape is entirely outside the canvas")
            if fill:
                self._pixel_paint(doc, node, bbox, lambda b: raster.polygon_mask(poly, b),
                                  color, opacity, erase)
            if outline:
                loop = poly + [poly[0]] if shape != "line" else poly
                samples = [(px, py, 1.0) for px, py in loop]
                self._pixel_paint(doc, node, bbox,
                                  lambda b: raster.stroke_mask(samples, size / 2.0, 1.0, "none", b),
                                  color, opacity, erase)
        self._refresh(doc)
        return {"status": "ok", "shape": shape, "engine": engine, "layer": node.name()}

    def cmd_fill(self, params):
        r = _num(params, "radius", 50, 0.5, 4096)
        cx, cy = _num(params, "x", 0), _num(params, "y", 0)
        p = dict(params, shape="ellipse", x=cx - r, y=cy - r, width=2 * r, height=2 * r,
                 fill=True, outline=False)
        return self.cmd_draw_shape(p)

    def cmd_clear(self, params):
        doc = self._doc()
        node = self._paint_layer(doc, params.get("layer"))
        w, h = doc.width(), doc.height()
        if params.get("color"):
            c = _qcolor(params["color"])
            self._fill_bgra(node, w, h, (c.blue(), c.green(), c.red(), 255))
        else:
            self._fill_bgra(node, w, h, (0, 0, 0, 0))
        self._refresh(doc)
        return {"status": "ok", "layer": node.name()}

    def cmd_get_color_at(self, params):
        doc = self._doc()
        x = _num(params, "x", 0, 0, doc.width() - 1, integer=True)
        y = _num(params, "y", 0, 0, doc.height() - 1, integer=True)
        if params.get("layer"):
            data = self._layer(doc, params["layer"]).pixelData(x, y, 1, 1)
        else:
            data = doc.rootNode().projectionPixelData(x, y, 1, 1)
        if len(data) < 4:
            raise CommandError("Could not read pixel")
        b, g, r, a = data[0], data[1], data[2], data[3]
        return {"status": "ok", "color": "#%02x%02x%02x" % (r, g, b), "alpha": a}

    # ------------------------------------------------------------------ layers

    def cmd_list_layers(self, params):
        doc = self._doc()
        active = doc.activeNode()
        active_id = _node_id(active) if active else None
        layers = [self._layer_info(n, active_id) for n in reversed(doc.rootNode().childNodes())]
        return {"status": "ok", "order": "top_to_bottom", "layers": layers}

    def cmd_add_layer(self, params):
        doc = self._doc()
        kind = params.get("type", "paint")
        if kind not in LAYER_TYPES:
            raise CommandError("type must be one of %s" % ", ".join(LAYER_TYPES))
        name = str(params.get("name") or "Layer")
        parent, above, below = self._parent_and_neighbour(doc, params)
        node = doc.createNode(name, LAYER_TYPES[kind])
        self._place(parent, node, above, below)
        self._apply_layer_props(node, {k: v for k, v in params.items() if k != "name"})
        if params.get("active", True) and kind == "paint":
            doc.setActiveNode(node)
        self._refresh(doc)
        return {"status": "ok", "id": _node_id(node), "name": node.name(), "type": node.type()}

    def cmd_set_layer(self, params):
        doc = self._doc()
        node = self._layer(doc, params.get("layer"))
        props = dict(params)
        props.pop("layer", None)
        if "new_name" in props:
            props["name"] = props.pop("new_name")
        else:
            props.pop("name", None)
        self._apply_layer_props(node, props)
        if params.get("active"):
            doc.setActiveNode(node)
        self._refresh(doc)
        return {"status": "ok", "id": _node_id(node), "name": node.name()}

    def cmd_move_layer(self, params):
        doc = self._doc()
        node = self._layer(doc, params.get("layer"))
        parent, above, below = self._parent_and_neighbour(doc, params)
        for ref in (above, below, parent):
            if ref is not None and _node_id(ref) == _node_id(node):
                raise CommandError("A layer can't be moved relative to itself")
        if any(_node_id(n) == _node_id(parent) for n in _iter_nodes(node)):
            raise CommandError("Can't move a group into itself")
        node.remove()
        self._place(parent, node, above, below)
        self._refresh(doc)
        return {"status": "ok", "id": _node_id(node)}

    def cmd_duplicate_layer(self, params):
        doc = self._doc()
        node = self._layer(doc, params.get("layer"))
        dup = node.duplicate()
        node.parentNode().addChildNode(dup, node)
        dup.setName(str(params.get("name") or node.name() + " copy"))
        self._refresh(doc)
        return {"status": "ok", "id": _node_id(dup), "name": dup.name()}

    def cmd_delete_layer(self, params):
        doc = self._doc()
        if params.get("layer") is None:
            raise CommandError("layer is required")
        node = self._layer(doc, params["layer"])
        name = node.name()
        node.remove()
        self._refresh(doc)
        return {"status": "ok", "deleted": name}

    def cmd_merge_down(self, params):
        doc = self._doc()
        node = self._layer(doc, params.get("layer"))
        merged = node.mergeDown()
        if merged is None:
            raise CommandError("Nothing to merge into")
        self._refresh(doc)
        return {"status": "ok", "id": _node_id(merged), "name": merged.name()}

    # ------------------------------------------------------------------ filters

    def cmd_list_filters(self, params):
        app = Krita.instance()
        if params.get("name"):
            f = app.filter(str(params["name"]))
            if f is None:
                raise CommandError("Unknown filter: %s" % params["name"])
            props = f.configuration().properties()
            return {"status": "ok", "name": params["name"],
                    "properties": {k: (v if isinstance(v, (int, float, str, bool)) else str(v))
                                   for k, v in props.items()}}
        return {"status": "ok", "filters": sorted(app.filters())}

    def cmd_apply_filter(self, params):
        doc = self._doc()
        node = self._paint_layer(doc, params.get("layer"))
        name = str(params.get("name") or "")
        f = Krita.instance().filter(name)
        if f is None:
            raise CommandError("Unknown filter: %s (use krita_list_filters)" % name)
        props = params.get("properties") or {}
        if not isinstance(props, dict):
            raise CommandError("properties must be an object")
        if props:
            config = f.configuration()
            for key, value in props.items():
                config.setProperty(str(key), value)
            f.setConfiguration(config)
        region = params.get("region")
        if region is not None:
            if (not isinstance(region, list) or len(region) != 4
                    or not all(isinstance(v, (int, float)) for v in region)):
                raise CommandError("region must be [x, y, width, height]")
            rx, ry, rw, rh = [int(v) for v in region]
        else:
            rect = doc.bounds()
            rx, ry, rw, rh = rect.x(), rect.y(), rect.width(), rect.height()
        if not f.apply(node, rx, ry, rw, rh):
            raise CommandError("Filter %s failed" % name)
        doc.waitForDone()
        self._refresh(doc)
        return {"status": "ok", "filter": name, "layer": node.name()}
