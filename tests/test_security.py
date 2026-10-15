import os
import stat
import sys

import pytest

from kritamcp import security
from kritamcp.security import PathPolicy, SecurityError


def test_token_created_private_and_stable(tmp_path):
    t1 = security.ensure_token(str(tmp_path))
    t2 = security.ensure_token(str(tmp_path))
    assert t1 == t2 and len(t1) >= 40
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(tmp_path / "token").st_mode)
        assert mode == 0o600


def test_token_compare():
    assert security.token_matches("abc", "abc")
    assert not security.token_matches("abc", "abd")
    assert not security.token_matches("abc", "")
    assert not security.token_matches("", "")


@pytest.mark.parametrize("headers,expected", [
    ({"Host": "127.0.0.1:5678", "X-Krita-MCP-Token": "t", "Content-Type": "application/json"}, None),
    ({"Host": "localhost:5678", "X-Krita-MCP-Token": "t", "Content-Type": "application/json; charset=utf-8"}, None),
    ({"Host": "127.0.0.1:5678", "Content-Type": "application/json"}, 401),
    ({"Host": "127.0.0.1:5678", "X-Krita-MCP-Token": "x", "Content-Type": "application/json"}, 401),
    ({"Host": "evil.example:5678", "X-Krita-MCP-Token": "t", "Content-Type": "application/json"}, 403),
    ({"Host": "127.0.0.1:5678", "Origin": "https://evil.example", "X-Krita-MCP-Token": "t",
      "Content-Type": "application/json"}, 403),
    ({"Host": "127.0.0.1:5678", "X-Krita-MCP-Token": "t", "Content-Type": "text/plain"}, 415),
])
def test_header_checks(headers, expected):
    problem = security.check_request_headers(headers, "t", 5678, require_json=True)
    assert (problem[0] if problem else None) == expected


def test_config_env_and_file(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text('{"port": 6000, "allowed_dirs": ["~/art"]}')
    monkeypatch.setenv("KRITA_MCP_ALLOWED_DIRS", os.pathsep.join(["/a", "/b"]))
    cfg = security.load_config(str(tmp_path))
    assert cfg["port"] == 6000
    assert cfg["allowed_dirs"] == ["~/art", "/a", "/b"]


def test_config_rejects_bad_port(tmp_path):
    (tmp_path / "config.json").write_text('{"port": 80}')
    with pytest.raises(SecurityError):
        security.load_config(str(tmp_path))


@pytest.fixture
def policy(tmp_path):
    out = tmp_path / "out"
    extra = tmp_path / "game_assets"
    cfg = tmp_path / "cfg"
    for d in (out, extra, cfg):
        d.mkdir()
    return PathPolicy(str(out), [str(extra)], forbidden_dirs=[str(cfg)]), tmp_path


def test_relative_paths_land_in_output(policy):
    p, root = policy
    assert p.resolve("art.kra", extensions=security.SAVE_EXTENSIONS) == str((root / "out" / "art.kra").resolve())


def test_allowed_dir_ok(policy):
    p, root = policy
    target = root / "game_assets" / "bg" / "forest.png"
    assert p.resolve(str(target), extensions=security.SAVE_EXTENSIONS) == str(target.resolve())


@pytest.mark.parametrize("bad", ["../escape.png", "/etc/passwd", "~/.bashrc"])
def test_escapes_blocked(policy, bad):
    p, _ = policy
    with pytest.raises(SecurityError):
        p.resolve(bad, extensions=security.SAVE_EXTENSIONS)


def test_symlink_escape_blocked(policy):
    p, root = policy
    outside = root / "secret"
    outside.mkdir()
    os.symlink(outside, root / "out" / "link")
    with pytest.raises(SecurityError):
        p.resolve("link/x.png", extensions=security.SAVE_EXTENSIONS)


def test_extension_and_overwrite(policy):
    p, root = policy
    with pytest.raises(SecurityError):
        p.resolve("evil.bat", extensions=security.SAVE_EXTENSIONS)
    (root / "out" / "a.png").write_bytes(b"x")
    with pytest.raises(SecurityError):
        p.resolve("a.png", extensions=security.SAVE_EXTENSIONS, allow_overwrite=False)
    assert p.resolve("a.png", extensions=security.SAVE_EXTENSIONS, allow_overwrite=True)


def test_forbidden_dir_inside_root(tmp_path):
    out = tmp_path / "out"
    (out / "cfg").mkdir(parents=True)
    p = PathPolicy(str(out), forbidden_dirs=[str(out / "cfg")])
    with pytest.raises(SecurityError):
        p.resolve("cfg/token.png", extensions={".png"})


def test_must_exist_and_null_byte(policy):
    p, _ = policy
    with pytest.raises(SecurityError):
        p.resolve("missing.png", must_exist=True, extensions={".png"})
    with pytest.raises(SecurityError):
        p.resolve("a\x00.png")
