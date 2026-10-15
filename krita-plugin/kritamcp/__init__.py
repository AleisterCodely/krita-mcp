"""
Krita MCP Bridge: lets an MCP client (Claude, Codex, ...) paint in Krita.

The HTTP bridge only listens on 127.0.0.1 and requires the token stored in
~/.config/krita-mcp/token. See README.md for details.
"""

try:
    from krita import Krita
except ImportError:  # imported outside Krita (e.g. unit tests)
    Krita = None

if Krita is not None:
    from .extension import KritaMCPExtension

    Krita.instance().addExtension(KritaMCPExtension(Krita.instance()))
