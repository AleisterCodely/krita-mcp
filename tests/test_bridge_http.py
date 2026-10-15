"""End-to-end: MCP server -> HTTP bridge -> fake Krita main thread."""
import asyncio
import base64
import http.client
import json
import socket
import threading

import pytest

from kritamcp.bridge import BridgeServer, CommandQueue

TINY_PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cf00000301010018dd8db40000000049454e44ae426082"
)).decode()

COMMANDS = ["status", "stroke", "get_canvas", "save", "list_layers"]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    port = free_port()
    token = "test-token-" + "x" * 40
    (tmp_path / "token").write_text(token)
    queue = CommandQueue()
    server = BridgeServer(port, token, queue, COMMANDS, command_timeout=5)
    server.start()
    seen = []
    stop = threading.Event()

    def krita_main_thread():
        while not stop.is_set():
            item = queue.pop()
            if item is None:
                stop.wait(0.01)
                continue
            cid, cmd = item
            seen.append(cmd)
            if cmd["action"] == "get_canvas":
                result = {"status": "ok", "width": 1, "height": 1, "png_base64": TINY_PNG}
            elif cmd["action"] == "save":
                result = {"error": "already exists. Pass overwrite=true"}
            else:
                result = {"status": "ok", "echo": cmd}
            queue.set_result(cid, result)

    t = threading.Thread(target=krita_main_thread, daemon=True)
    t.start()
    monkeypatch.setenv("KRITA_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("KRITA_MCP_TOKEN", raising=False)
    yield {"port": port, "token": token, "seen": seen}
    stop.set()
    server.stop()


def raw(port, method="POST", path="/", body=b'{"action":"status","params":{}}', headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body if method == "POST" else None, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, (json.loads(data) if data else None)


def good_headers(b):
    return {"X-Krita-MCP-Token": b["token"], "Content-Type": "application/json"}


def test_valid_request(bridge):
    status, data = raw(bridge["port"], headers=good_headers(bridge))
    assert status == 200 and data["echo"]["action"] == "status"


def test_no_token_rejected_and_not_queued(bridge):
    status, _ = raw(bridge["port"], headers={"Content-Type": "application/json"})
    assert status == 401
    assert bridge["seen"] == []


def test_browser_simple_request_blocked(bridge):
    # What a malicious page can send with fetch(..., {mode: 'no-cors'}):
    status, _ = raw(bridge["port"], headers={
        "Origin": "https://evil.example", "Content-Type": "text/plain",
        "X-Krita-MCP-Token": bridge["token"]})
    assert status == 403
    assert bridge["seen"] == []


def test_dns_rebinding_host_blocked(bridge):
    h = good_headers(bridge)
    h["Host"] = "attacker.example:%d" % bridge["port"]
    assert raw(bridge["port"], headers=h)[0] == 403


def test_wrong_content_type(bridge):
    h = good_headers(bridge)
    h["Content-Type"] = "text/plain"
    assert raw(bridge["port"], headers=h)[0] == 415


def test_preflight_and_unknown_action_and_huge_body(bridge):
    assert raw(bridge["port"], method="OPTIONS")[0] == 403
    status, _ = raw(bridge["port"], body=b'{"action":"rm_rf","params":{}}', headers=good_headers(bridge))
    assert status == 400
    h = good_headers(bridge)
    h["Content-Length"] = str(50 * 1024 * 1024)
    status, _ = raw(bridge["port"], body=b"{}", headers=h)
    assert status == 413


def test_health_requires_token(bridge):
    assert raw(bridge["port"], method="GET", path="/health")[0] == 401
    status, data = raw(bridge["port"], method="GET", path="/health",
                       headers={"X-Krita-MCP-Token": bridge["token"]})
    assert status == 200 and data["plugin"] == "kritamcp"


def test_only_loopback(bridge):
    with pytest.raises(OSError):
        # Reaching it through a non-loopback interface must fail.
        ip = socket.gethostbyname(socket.gethostname())
        if ip.startswith("127."):
            raise OSError("no external interface; trivially loopback-only")
        socket.create_connection((ip, bridge["port"]), timeout=1)


def test_mcp_server_end_to_end(bridge, monkeypatch):
    import importlib
    monkeypatch.setenv("KRITA_URL", "http://127.0.0.1:%d" % bridge["port"])
    import server
    importlib.reload(server)
    from fastmcp import Client

    async def go():
        async with Client(server.mcp) as client:
            tools = {t.name for t in await client.list_tools()}
            assert {"krita_stroke", "krita_add_layer", "krita_export_layers", "krita_save"} <= tools

            await client.call_tool("krita_stroke", {
                "points": [[0, 0], [10, 10], [20, 5]], "pressures": [0.2, 1, 0.4],
                "size": 30, "preset": "Pencil", "layer": "Trees"})
            sent = bridge["seen"][-1]["params"]
            assert sent["size"] == 30 and sent["layer"] == "Trees" and sent["pressures"] == [0.2, 1, 0.4]
            assert "hardness" not in sent  # None values are dropped

            res = await client.call_tool("krita_get_canvas", {})
            kinds = [c.type for c in res.content]
            assert kinds == ["text", "image"]

            res = await client.call_tool("krita_save", {"path": "a.kra"}, raise_on_error=False)
            assert res.is_error and "overwrite" in res.content[0].text

    asyncio.run(go())


def test_mcp_server_wrong_token(bridge, monkeypatch):
    import importlib
    monkeypatch.setenv("KRITA_URL", "http://127.0.0.1:%d" % bridge["port"])
    monkeypatch.setenv("KRITA_MCP_TOKEN", "wrong")
    import server
    importlib.reload(server)
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="rejected the token"):
        server.send_command("status")
