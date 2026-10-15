"""Drive the real plugin code (bridge + extension) through the real MCP server
code, against an in-memory model of Krita's Python API."""
import importlib
import json
import os
import socket
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QCoreApplication  # noqa: E402

from tests import fake_krita  # noqa: E402

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    port = free_port()
    cfg, out, game = tmp_path / "cfg", tmp_path / "out", tmp_path / "game"
    game.mkdir()
    monkeypatch.setenv("KRITA_MCP_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("KRITA_MCP_OUTPUT_DIR", str(out))
    monkeypatch.setenv("KRITA_MCP_ALLOWED_DIRS", str(game))
    monkeypatch.setenv("KRITA_MCP_PORT", str(port))
    monkeypatch.setenv("KRITA_URL", "http://127.0.0.1:%d" % port)
    monkeypatch.delenv("KRITA_MCP_TOKEN", raising=False)

    fk = fake_krita.install()
    for name in [m for m in sys.modules if m == "kritamcp" or m.startswith("kritamcp.")]:
        del sys.modules[name]
    import kritamcp  # registers the extension with the fake Krita
    app = fk.Krita.instance()
    ext = app.extensions[0]
    ext.createActions(app.window)
    assert ext.startup_error is None, ext.startup_error

    stop = threading.Event()

    def pump():  # stands in for Krita's QTimer on the main thread
        while not stop.is_set():
            ext.process_commands()
            stop.wait(0.002)

    threading.Thread(target=pump, daemon=True).start()
    import server
    importlib.reload(server)
    yield {"server": server, "app": app, "ext": ext, "out": out, "game": game, "cfg": cfg}
    stop.set()
    ext.server.stop()
    sys.modules.pop("krita", None)


def call(env, action, **params):
    return env["server"].send_command(action, params)


def test_token_file_created_private(env):
    assert (env["cfg"] / "token").exists()


def test_full_layered_workflow(env):
    from fastmcp.exceptions import ToolError
    app = env["app"]

    r = call(env, "new_canvas", width=40, height=30, background="#2a1e33")
    doc = app.doc
    bg = doc.rootNode().childNodes()[0]
    assert bg.name() == "Background" and bg.pixels[:4] == bytes([0x33, 0x1e, 0x2a, 255])
    assert r["active_layer"] == "Layer 1"

    call(env, "add_layer", name="Sky", below="Layer 1")
    call(env, "add_layer", name="Trees", type="group")
    call(env, "add_layer", name="Trunks", parent="Trees")
    call(env, "add_layer", name="Canopy", parent="Trees", above="Trunks", blend_mode="multiply", opacity=0.8)
    call(env, "add_layer", name="Glow", parent="Trees", below="Trunks")  # bottom of the group
    layers = call(env, "list_layers")["layers"]
    assert [l["name"] for l in layers] == ["Trees", "Layer 1", "Sky", "Background"]
    trees = layers[0]
    assert [c["name"] for c in trees["children"]] == ["Canopy", "Trunks", "Glow"]
    assert trees["children"][0]["blend_mode"] == "multiply" and trees["children"][0]["opacity"] == 0.8

    # Native stroke with taper: several sizes, settings restored afterwards.
    view = app.window.view
    call(env, "set_brush", preset="Pencil", size=30, opacity=0.7)
    before = (view._size, view._opacity, view._preset.name(), view._fg.colorForCanvas(None).name())
    call(env, "stroke", layer="Trunks", points=[[2, 2], [20, 15], [35, 25]],
         pressures=[0.1, 1.0, 0.2], color="#b8a9c9", preset="Oil Pastel")
    trunks = [n for n in doc.rootNode().childNodes()[-1].childNodes() if n.name() == "Trunks"][0]
    sizes = {c[1] for c in trunks.paint_calls}
    assert len(trunks.paint_calls) > 5 and min(sizes) < 8 and max(sizes) == 25.0
    assert {c[4] for c in trunks.paint_calls} == {"#b8a9c9"}
    assert {c[5] for c in trunks.paint_calls} == {"Oil Pastel Textured"}
    after = (view._size, view._opacity, view._preset.name(), view._fg.colorForCanvas(None).name())
    assert before == after

    # Uniform pressure -> one seamless path; erase uses the erase blend mode.
    trunks.paint_calls.clear()
    call(env, "stroke", layer="Trunks", points=[[2, 2], [30, 20]], erase=True)
    assert len(trunks.paint_calls) == 1 and trunks.paint_calls[0][3] == "erase"
    assert view._blend == "normal"

    # Pixel engine actually writes pixels.
    call(env, "stroke", layer="Sky", points=[[5, 5], [30, 5]], engine="pixel", size=6,
         color="#ff0000", opacity=1.0, hardness=1.0)
    sky = [n for n in doc.rootNode().childNodes() if n.name() == "Sky"][0]
    i = (5 * 40 + 15) * 4
    assert sky.pixels[i:i + 4] == bytes([0, 0, 255, 255])

    call(env, "draw_shape", shape="polygon", points=[[0, 29], [20, 10], [39, 29]], layer="Glow")
    call(env, "fill", x=10, y=10, radius=5, layer="Canopy", engine="pixel", color="#00ff00")

    # Painting a group or locked layer is refused.
    with pytest.raises(ToolError, match="paint layer"):
        call(env, "stroke", layer="Trees", points=[[0, 0], [1, 1]])
    call(env, "set_layer", layer="Sky", locked=True, new_name="Sky BG")
    with pytest.raises(ToolError, match="locked"):
        call(env, "stroke", layer="Sky BG", points=[[0, 0], [1, 1]])

    # Moving, duplicating, filters.
    call(env, "move_layer", layer="Layer 1", below="Sky BG")
    names = [l["name"] for l in call(env, "list_layers")["layers"]]
    assert names == ["Trees", "Sky BG", "Layer 1", "Background"]
    with pytest.raises(ToolError, match="itself"):
        call(env, "move_layer", layer="Trees", parent="Trees")
    r = call(env, "duplicate_layer", layer="Canopy")
    assert r["name"] == "Canopy copy"
    call(env, "set_layer", layer=r["id"], new_name="Canopy")
    with pytest.raises(ToolError, match="used 2 times"):
        call(env, "set_layer", layer="Canopy", visible=False)
    call(env, "apply_filter", name="noise", layer="Glow", properties={"level": 12})
    assert app.filter("noise").applied[-1][-1]["level"] == 12

    # Preview image.
    shot = call(env, "get_canvas", preview_max=64)
    assert shot["preview_width"] == 40 and shot["png_base64"]

    # Saving: sandboxed, no silent overwrite, .kra keeps layers.
    r = call(env, "save", path="forest.kra")
    assert r["layered"] and (env["out"] / "forest.kra").exists()
    with pytest.raises(ToolError, match="overwrite"):
        call(env, "save", path="forest.kra")
    call(env, "save", path="forest.kra", overwrite=True)
    with pytest.raises(ToolError, match="outside the allowed"):
        call(env, "save", path=os.path.expanduser("~/.bashrc.png"))
    with pytest.raises(ToolError, match="not allowed"):
        call(env, "save", path="payload.bat")
    with pytest.raises(ToolError, match="protected|outside"):
        call(env, "save", path=str(env["cfg"] / "token.png"))
    call(env, "save", path=str(env["game"] / "bg" / "forest.png"))

    # Layer export with manifest into the game folder.
    r = call(env, "export_layers", folder=str(env["game"] / "layers"))
    manifest = json.loads((env["game"] / "layers" / "manifest.json").read_text())
    assert [l["name"] for l in manifest["layers"]] == ["Background", "Layer 1", "Sky BG", "Trees"]
    assert manifest["layers"][0]["file"] == "00_background.png"
    with pytest.raises(ToolError, match="overwrite"):
        call(env, "export_layers", folder=str(env["game"] / "layers"))

    # Undo, status, delete.
    call(env, "undo")
    assert app.actions["edit_undo"].count == 1
    st = call(env, "status")
    assert st["document"]["width"] == 40 and st["brush"]["preset"] == "c) Pencil-2"
    call(env, "delete_layer", layer="Layer 1")
    assert "Layer 1" not in [l["name"] for l in call(env, "list_layers")["layers"]]


def test_validation_errors(env):
    from fastmcp.exceptions import ToolError
    call(env, "new_canvas", width=20, height=20)
    with pytest.raises(ToolError, match="between"):
        call(env, "new_canvas", width=100000, height=10)
    with pytest.raises(ToolError, match="pressures"):
        call(env, "stroke", points=[[0, 0], [5, 5]], pressures=[1])
    with pytest.raises(ToolError, match="Invalid color"):
        call(env, "stroke", points=[[0, 0], [5, 5]], color="notacolor")
    with pytest.raises(ToolError, match="preset not found"):
        call(env, "set_brush", preset="nope")
    with pytest.raises(ToolError, match="not found"):
        call(env, "open_file", path="missing.png")
