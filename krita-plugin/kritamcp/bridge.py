"""
HTTP bridge between the MCP server and Krita's main thread.

No Krita imports here: the HTTP thread only authenticates, validates and
queues commands. Krita's main thread drains the queue (see extension.py),
because the Krita API must not be touched from other threads.
"""

import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import security

MAX_BODY_BYTES = 8 * 1024 * 1024
COMMAND_TIMEOUT = 300  # seconds; the MCP server's per-call timeouts must not exceed this


class CommandQueue:
    """Hands commands from the HTTP thread to Krita's main thread and back."""

    def __init__(self):
        self._cv = threading.Condition()
        self._queue = []
        self._pending = set()
        self._results = {}
        self._ids = itertools.count(1)

    def submit(self, command):
        with self._cv:
            command_id = next(self._ids)
            self._queue.append((command_id, command))
            self._pending.add(command_id)
            return command_id

    def pop(self):
        with self._cv:
            return self._queue.pop(0) if self._queue else None

    def set_result(self, command_id, result):
        with self._cv:
            if command_id in self._pending:  # drop results nobody waits for
                self._results[command_id] = result
                self._cv.notify_all()

    def wait(self, command_id, timeout=COMMAND_TIMEOUT):
        with self._cv:
            done = self._cv.wait_for(lambda: command_id in self._results, timeout)
            self._pending.discard(command_id)
            if done:
                return self._results.pop(command_id)
            # Never started? Then don't run it later behind the caller's back.
            self._queue = [item for item in self._queue if item[0] != command_id]
            return {"error": "Timed out waiting for Krita to run the command"}


def _make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # stop a stalled client from blocking the single-threaded server
        server_version = "KritaMCP"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _check(self, require_json):
            problem = security.check_request_headers(
                self.headers, bridge.token, bridge.port, require_json
            )
            if problem:
                self._send(problem[0], {"error": problem[1]})
                return False
            return True

        def do_GET(self):
            if not self._check(require_json=False):
                return
            if self.path.split("?")[0] == "/health":
                self._send(200, {"status": "ok", "plugin": "kritamcp", "version": bridge.version})
            else:
                self._send(404, {"error": "Unknown endpoint"})

        def do_POST(self):
            if not self._check(require_json=True):
                return
            if self.path.split("?")[0] not in ("/", "/command"):
                self._send(404, {"error": "Unknown endpoint"})
                return

            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, {"error": "Content-Length required"})
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(413, {"error": "Request body too large or empty"})
                return

            try:
                command = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._send(400, {"error": "Invalid JSON"})
                return

            if not isinstance(command, dict):
                self._send(400, {"error": "Body must be a JSON object"})
                return
            action = command.get("action")
            params = command.get("params", {})
            if action not in bridge.commands:
                self._send(400, {"error": "Unknown action: %r" % (action,)})
                return
            if not isinstance(params, dict):
                self._send(400, {"error": "params must be a JSON object"})
                return

            command_id = bridge.queue.submit({"action": action, "params": params})
            self._send(200, bridge.queue.wait(command_id, bridge.command_timeout))

        def do_OPTIONS(self):
            # Never answer CORS preflights positively.
            self._send(403, {"error": "Not allowed"})

    return Handler


class BridgeServer:
    """Loopback-only HTTP server running on a daemon thread."""

    def __init__(self, port, token, queue, commands, version="2",
                 command_timeout=COMMAND_TIMEOUT):
        self.port = port
        self.token = token
        self.queue = queue
        self.commands = frozenset(commands)
        self.version = version
        self.command_timeout = command_timeout
        self._httpd = None
        self._thread = None

    def start(self):
        # Bind to 127.0.0.1 explicitly; never all interfaces.
        self._httpd = HTTPServer(("127.0.0.1", self.port), _make_handler(self))
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="KritaMCPBridge", daemon=True
        )
        self._thread.start()

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
