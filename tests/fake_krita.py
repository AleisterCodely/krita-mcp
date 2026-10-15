"""
A small in-memory stand-in for Krita's Python API (libkis), modelling the calls
the plugin makes. It checks our code against the documented API shape; it is
not a substitute for testing inside real Krita.
"""

import os
import sys
import types

from PyQt5.QtCore import QRect, QUuid
from PyQt5.QtGui import QColor, QImage


class InfoObject:
    def __init__(self):
        self._props = {}

    def properties(self):
        return dict(self._props)

    def setProperty(self, key, value):
        self._props[key] = value


class ManagedColor:
    def __init__(self, qcolor):
        self._q = QColor(qcolor)

    @staticmethod
    def fromQColor(qcolor, canvas=None):
        return ManagedColor(qcolor)

    def colorForCanvas(self, canvas):
        return QColor(self._q)


class Resource:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class Node:
    def __init__(self, doc, name, kind):
        self.doc, self._name, self._type = doc, name, kind
        self._uuid = QUuid.createUuid()
        self._children, self._parent = [], None
        self._visible, self._locked, self._alpha, self._inherit = True, False, False, False
        self._opacity, self._blend, self.label = 255, "normal", 0
        self.pixels = bytearray(doc.w * doc.h * 4)
        self.paint_calls = []

    # identity / tree
    def name(self): return self._name
    def setName(self, n): self._name = n
    def type(self): return self._type
    def uniqueId(self): return self._uuid
    def childNodes(self): return list(self._children)
    def parentNode(self): return self._parent

    def addChildNode(self, child, above):
        assert child._parent is None, "node already has a parent"
        if above is None:
            self._children.append(child)
        else:
            self._children.insert(self._children.index(above) + 1, child)
        child._parent = self
        return True

    def remove(self):
        self._parent._children.remove(self)
        self._parent = None
        return True

    # properties
    def visible(self): return self._visible
    def setVisible(self, v): self._visible = v
    def locked(self): return self._locked
    def setLocked(self, v): self._locked = v
    def alphaLocked(self): return self._alpha
    def setAlphaLocked(self, v): self._alpha = v
    def inheritAlpha(self): return self._inherit
    def setInheritAlpha(self, v): self._inherit = v
    def opacity(self): return self._opacity
    def setOpacity(self, v): self._opacity = v
    def blendingMode(self): return self._blend
    def setBlendingMode(self, v): self._blend = v
    def setColorLabel(self, v): self.label = v

    def bounds(self):
        xs, ys = [], []
        w = self.doc.w
        for i in range(3, len(self.pixels), 4):
            if self.pixels[i]:
                p = i // 4
                xs.append(p % w)
                ys.append(p // w)
        if not xs:
            return QRect()
        return QRect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)

    # pixels
    def pixelData(self, x, y, w, h):
        out = bytearray()
        for row in range(y, y + h):
            s = (row * self.doc.w + x) * 4
            out += self.pixels[s:s + w * 4]
        return bytes(out)

    def setPixelData(self, data, x, y, w, h):
        assert len(data) == w * h * 4
        for r in range(h):
            s = ((y + r) * self.doc.w + x) * 4
            self.pixels[s:s + w * 4] = data[r * w * 4:(r + 1) * w * 4]
        return True

    def projectionPixelData(self, x, y, w, h):
        return self.pixelData(x, y, w, h)

    def thumbnail(self, w, h):
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(QColor("#123456"))
        return img

    def save(self, path, xres, yres, info, rect):
        with open(path, "wb") as fh:
            fh.write(b"PNG layer %s" % self._name.encode())
        return True

    def duplicate(self):
        n = Node(self.doc, self._name, self._type)
        n.pixels = bytearray(self.pixels)
        return n

    def mergeDown(self):
        sibs = self._parent._children
        idx = sibs.index(self)
        if idx == 0:
            return None
        below = sibs[idx - 1]
        self.remove()
        return below

    # scripting paint API (Krita 5)
    def _record(self, kind, *args):
        view = self.doc.app.window.view
        self.paint_calls.append((kind, view._size, round(view._opacity, 3), view._blend,
                                 view._fg.colorForCanvas(None).name(),
                                 view._preset.name()) + args)

    def paintPath(self, path, stroke, fill): self._record("path", stroke, fill, path.elementCount())
    def paintRectangle(self, rect, stroke, fill): self._record("rect", stroke, fill)
    def paintEllipse(self, rect, stroke, fill): self._record("ellipse", stroke, fill)
    def paintPolygon(self, pts, stroke, fill): self._record("polygon", stroke, fill, len(pts))
    def paintLine(self, a, b): self._record("line")


class Document:
    def __init__(self, app, w, h, name, res):
        self.app, self.w, self.h, self._name, self.res = app, w, h, name, res
        self._root = Node(self, "root", "grouplayer")
        bg = Node(self, "Background", "paintlayer")
        self._root.addChildNode(bg, None)
        self._active = bg
        self.file = ""
        self.batch = False

    def width(self): return self.w
    def height(self): return self.h
    def name(self): return self._name
    def fileName(self): return self.file
    def resolution(self): return int(self.res)
    def xRes(self): return self.res
    def yRes(self): return self.res
    def bounds(self): return QRect(0, 0, self.w, self.h)
    def rootNode(self): return self._root
    def activeNode(self): return self._active
    def setActiveNode(self, n): self._active = n
    def createNode(self, name, kind): return Node(self, name, kind)
    def waitForDone(self): pass
    def refreshProjection(self): pass
    def setBatchmode(self, v): self.batch = v

    def thumbnail(self, w, h):
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(QColor("#2a1e33"))
        return img

    def saveAs(self, path):
        assert self.batch
        self.file = path
        with open(path, "wb") as fh:
            fh.write(b"KRA")
        return True

    def exportImage(self, path, info):
        assert self.batch
        with open(path, "wb") as fh:
            fh.write(b"IMG")
        return True


class View:
    def __init__(self):
        self._size, self._opacity, self._flow, self._blend = 40.0, 1.0, 1.0, "normal"
        self._fg, self._bg = ManagedColor("#000000"), ManagedColor("#ffffff")
        self._preset = Resource("b) Basic-5 Size Opacity")

    def canvas(self): return object()
    def brushSize(self): return self._size
    def setBrushSize(self, v): self._size = float(v)
    def paintingOpacity(self): return self._opacity
    def setPaintingOpacity(self, v): self._opacity = float(v)
    def paintingFlow(self): return self._flow
    def setPaintingFlow(self, v): self._flow = float(v)
    def currentBlendingMode(self): return self._blend
    def setCurrentBlendingMode(self, v): self._blend = v
    def foregroundColor(self): return self._fg
    def setForeGroundColor(self, c): self._fg = c
    def backgroundColor(self): return self._bg
    def setBackGroundColor(self, c): self._bg = c
    def currentBrushPreset(self): return self._preset

    def setCurrentBrushPreset(self, r):
        self._preset = r
        self._size, self._opacity = 25.0, 0.9  # presets reset size/opacity in Krita


class Window:
    def __init__(self, app):
        self.app, self.view = app, View()

    def activeView(self): return self.view
    def addView(self, doc): self.app.doc = doc


class Action:
    def __init__(self): self.count = 0
    def trigger(self): self.count += 1


class Filter:
    def __init__(self, name):
        self.name, self.cfg, self.applied = name, InfoObject(), []
        self.cfg.setProperty("level", 50)

    def configuration(self): return self.cfg
    def setConfiguration(self, c): self.cfg = c

    def apply(self, node, x, y, w, h):
        self.applied.append((node.name(), x, y, w, h, self.cfg.properties()))
        return True


class Krita:
    _inst = None

    def __init__(self):
        self.doc, self.window = None, Window(self)
        self.extensions, self.actions = [], {"edit_undo": Action(), "edit_redo": Action()}
        self.presets = {n: Resource(n) for n in (
            "b) Basic-5 Size Opacity", "c) Pencil-2", "d) Ink-2 Fineliner",
            "Chalk Grainy", "Oil Pastel Textured")}
        self._filters = {"noise": Filter("noise"), "blur": Filter("blur")}

    @classmethod
    def instance(cls):
        if cls._inst is None:
            cls._inst = Krita()
        return cls._inst

    def addExtension(self, e): self.extensions.append(e)
    def activeDocument(self): return self.doc
    def activeWindow(self): return self.window
    def createDocument(self, w, h, name, model, depth, profile, res): return Document(self, w, h, name, res)

    def openDocument(self, path):
        d = Document(self, 64, 48, os.path.basename(path), 72.0)
        return d

    def resources(self, kind):
        assert kind == "preset"
        return dict(self.presets)

    def action(self, name): return self.actions.get(name)
    def filter(self, name): return self._filters.get(name)
    def filters(self): return list(self._filters)


class Extension:
    def __init__(self, parent):
        self.parent = parent


def install():
    mod = types.ModuleType("krita")
    for obj in (Krita, Extension, InfoObject, ManagedColor):
        setattr(mod, obj.__name__, obj)
    sys.modules["krita"] = mod
    Krita._inst = None
    return mod
