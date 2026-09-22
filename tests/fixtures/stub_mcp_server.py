"""A minimal external MCP server, for testing what happens when we mount one.

Run as a subprocess over stdio, the way a real one is. An in-process stub would skip the
half of `mcp_bridge/client.py` that is actually about processes — the environment the
child receives, a session outliving the task that made a call, a server that dies mid-run.

Three tools, each there to be a specific case:

- `echo`     read-only, so it should run without asking anybody.
- `write`    named as mutating in the configuration, so it should pause for approval. The
             server itself says nothing about that, which is the point: policy is the
             harness's to declare.
- `leaks`    returns the environment variable names the child was given, so a test can
             assert the worker's secrets were not among them.
- `explode`  raises, so a failing remote tool can be shown to arrive as a tool error
             rather than as a crashed step.
"""

from __future__ import annotations

import os
import sys

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="stub", version="0")


@server.tool()
async def echo(text: str) -> str:
    """Return the text you were given."""
    return f"echo: {text}"


@server.tool()
async def write(path: str, content: str) -> str:
    """Pretend to write a file. Declared mutating by whoever mounts this."""
    return f"wrote {len(content)} bytes to {path}"


@server.tool()
async def leaks() -> str:
    """The names of the environment variables this process was started with."""
    return ",".join(sorted(os.environ))


@server.tool()
async def explode() -> str:
    """Fail, on purpose."""
    raise RuntimeError("the remote tool exploded")


@server.tool()
async def not_allowed() -> str:
    """Offered by the server, absent from the allow-list, and so never mounted."""
    return "you should not be able to call this"


if __name__ == "__main__":
    if os.environ.get("STUB_REFUSE_TO_START"):
        sys.stderr.write("stub: refusing to start\n")
        raise SystemExit(1)
    server.run()
