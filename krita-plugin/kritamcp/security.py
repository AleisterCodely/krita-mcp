"""
Security helpers for the Krita MCP bridge.

No Krita imports here, so this module can be unit-tested outside Krita.

Threat model: the plugin runs an HTTP server inside Krita. Without these
checks, any local process, and any web page open in your browser, could send
it commands (including "save this canvas over some file"). We defend with:

  * a random shared-secret token, stored in a user-only file
  * rejecting browser requests (Origin header) and DNS rebinding (Host header)
  * requiring application/json (blocks "simple" cross-site form/text posts)
  * a path sandbox for every file read/write, with no silent overwrites
"""

import hmac
import json
import os
import secrets

CONFIG_DIR_DEFAULT = os.path.join(os.path.expanduser("~"), ".config", "krita-mcp")
TOKEN_FILENAME = "token"
CONFIG_FILENAME = "config.json"
TOKEN_HEADER = "X-Krita-MCP-Token"

DEFAULT_PORT = 5678
DEFAULT_OUTPUT_DIR = "~/krita-mcp-output"

SAVE_EXTENSIONS = frozenset({
    ".kra", ".png", ".jpg", ".jpeg", ".webp", ".psd", ".tif", ".tiff", ".ora", ".exr",
})
OPEN_EXTENSIONS = SAVE_EXTENSIONS | frozenset({".gif", ".bmp", ".tga"})


class SecurityError(Exception):
    """Raised when a request or path violates the security policy."""


def config_dir():
    return os.environ.get("KRITA_MCP_CONFIG_DIR") or CONFIG_DIR_DEFAULT


def load_config(directory=None):
    """Read config.json (optional) and environment overrides.

    config.json example:
        {"port": 5678,
         "output_dir": "~/krita-mcp-output",
         "allowed_dirs": ["~/games/my-game/assets/art"]}
    """
    directory = directory or config_dir()
    cfg = {"port": DEFAULT_PORT, "output_dir": DEFAULT_OUTPUT_DIR, "allowed_dirs": []}

    path = os.path.join(directory, CONFIG_FILENAME)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise SecurityError("config.json must contain a JSON object")
        if "port" in data:
            cfg["port"] = data["port"]
        if "output_dir" in data:
            cfg["output_dir"] = data["output_dir"]
        if "allowed_dirs" in data:
            cfg["allowed_dirs"] = data["allowed_dirs"]

    env = os.environ
    if env.get("KRITA_MCP_PORT"):
        cfg["port"] = env["KRITA_MCP_PORT"]
    if env.get("KRITA_MCP_OUTPUT_DIR"):
        cfg["output_dir"] = env["KRITA_MCP_OUTPUT_DIR"]
    if env.get("KRITA_MCP_ALLOWED_DIRS"):
        cfg["allowed_dirs"] = list(cfg["allowed_dirs"]) + [
            d for d in env["KRITA_MCP_ALLOWED_DIRS"].split(os.pathsep) if d
        ]

    try:
        cfg["port"] = int(cfg["port"])
    except (TypeError, ValueError):
        raise SecurityError("port must be an integer")
    if not 1024 <= cfg["port"] <= 65535:
        raise SecurityError("port must be between 1024 and 65535")
    if not isinstance(cfg["output_dir"], str) or not cfg["output_dir"].strip():
        raise SecurityError("output_dir must be a non-empty string")
    if not isinstance(cfg["allowed_dirs"], list) or not all(
        isinstance(d, str) for d in cfg["allowed_dirs"]
    ):
        raise SecurityError("allowed_dirs must be a list of folder paths")

    cfg["config_dir"] = directory
    return cfg


def ensure_token(directory=None):
    """Return the shared token, creating it (mode 0600) if missing."""
    directory = directory or config_dir()
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # Windows: per-user profile folder ACLs apply instead

    path = os.path.join(directory, TOKEN_FILENAME)
    existing = _read_file(path)
    if existing and len(existing) >= 32:
        return existing

    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return token


def read_token(directory=None):
    """Token for the MCP server side: env var first, then the token file."""
    env_token = os.environ.get("KRITA_MCP_TOKEN")
    if env_token:
        return env_token.strip()
    return _read_file(os.path.join(directory or config_dir(), TOKEN_FILENAME))


def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def token_matches(expected, provided):
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def allowed_hosts(port):
    return {"127.0.0.1:%d" % port, "localhost:%d" % port}


def check_request_headers(headers, expected_token, port, require_json):
    """Validate an incoming request. Returns (status, message) or None if OK.

    `headers` only needs a .get(name, default) method.
    """
    if headers.get("Origin") is not None:
        return 403, "Browser-originated requests are not allowed"
    host = (headers.get("Host") or "").strip().lower()
    if host not in allowed_hosts(port):
        return 403, "Unexpected Host header"
    if not token_matches(expected_token, headers.get(TOKEN_HEADER, "")):
        return 401, "Missing or invalid %s header" % TOKEN_HEADER
    if require_json:
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return 415, "Content-Type must be application/json"
    return None


def _norm(path):
    return os.path.normcase(os.path.realpath(path))


def _within(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


class PathPolicy:
    """Confines file access to the output folder plus explicitly allowed folders."""

    def __init__(self, output_dir, allowed_dirs=(), forbidden_dirs=()):
        self.output_dir = os.path.realpath(os.path.expanduser(output_dir))
        roots = [self.output_dir]
        for d in allowed_dirs:
            real = os.path.realpath(os.path.expanduser(d))
            if real not in roots:
                roots.append(real)
        self.roots = roots
        self._norm_roots = [_norm(r) for r in roots]
        self._forbidden = [_norm(os.path.expanduser(d)) for d in forbidden_dirs]

    def resolve(self, path, must_exist=False, extensions=None, allow_overwrite=True,
                directory=False):
        """Return an absolute, symlink-resolved path or raise SecurityError.

        Relative paths are taken relative to the output folder.
        """
        if not isinstance(path, str) or not path.strip():
            raise SecurityError("A non-empty path is required")
        if "\x00" in path:
            raise SecurityError("Invalid path")

        candidate = os.path.expanduser(path.strip())
        if not os.path.isabs(candidate):
            candidate = os.path.join(self.output_dir, candidate)
        real = os.path.realpath(candidate)
        normed = os.path.normcase(real)

        if not any(_within(normed, root) for root in self._norm_roots):
            raise SecurityError(
                "Path %s is outside the allowed folders (%s). Add its folder to "
                "allowed_dirs in %s if you want the agent to use it."
                % (real, ", ".join(self.roots), os.path.join(config_dir(), CONFIG_FILENAME))
            )
        if any(_within(normed, bad) for bad in self._forbidden):
            raise SecurityError("Path is inside a protected folder")

        if directory:
            if os.path.exists(real) and not os.path.isdir(real):
                raise SecurityError("%s exists and is not a folder" % real)
            return real

        ext = os.path.splitext(real)[1].lower()
        if extensions is not None and ext not in extensions:
            raise SecurityError(
                "File type %r is not allowed here (allowed: %s)"
                % (ext or "(none)", ", ".join(sorted(extensions)))
            )
        if os.path.isdir(real):
            raise SecurityError("%s is a folder, expected a file" % real)
        if must_exist and not os.path.exists(real):
            raise SecurityError("File not found: %s" % real)
        if not allow_overwrite and os.path.exists(real):
            raise SecurityError(
                "%s already exists. Pass overwrite=true to replace it." % real
            )
        return real
