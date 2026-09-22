"""`autoswe-mcp`: the MCP server over stdio, for an editor that launches it as a child.

One process, its own database and Redis connections, speaking MCP on stdin and stdout.
There is no HTTP hop: the tools call `api.service` directly, so an editor driving a run
gets the same code path as `curl` against the control plane and not a client of it.

## stdout belongs to the protocol

While serving, the SDK points file descriptor 1 at stderr, so a stray `print` inside a
tool does not corrupt the JSON-RPC stream. That protection starts when serving does, and
this module opens a database connection and a Redis connection before then — so logging is
configured to stderr first, ahead of anything that could log. The settings summary
`autoswe config` prints is deliberately absent here for the same reason.

## Authorisation is the process, not a key

The plan sketched an `AUTOSWE_API_KEY` check. There is nothing for it to protect: this
process reads `DATABASE_URL` and `REDIS_URL` from its own environment and connects
straight to both, so whoever can start it with those variables can already read and write
everything the key would have guarded. A check against a key supplied by the same
environment would be a lock whose key is taped to it. The HTTP transport at `/mcp` is a
different matter — it is reachable from off the machine, and it does require the header.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from api.service import ControlPlane
from core.settings import get_settings
from mcp_bridge.server import build_server
from observability.logging import configure_logging, get_logger
from storage.db import make_engine
from storage.redis import RedisBus

log = get_logger(__name__)


async def serve() -> None:
    """Open the connections, serve until the client closes stdin, then close them."""
    settings = get_settings()
    configure_logging(settings.log_level, stream=sys.stderr)

    engine = make_engine(settings.database_url, pool_size=2, max_overflow=1)
    bus = RedisBus(settings.redis_url)
    arq = await _arq_pool(settings)
    plane = ControlPlane(engine=engine, bus=bus, arq=arq)

    server = build_server(
        lambda: plane,
        provider=settings.llm_provider,
        worktrees_dir=settings.worktrees_dir,
        version=_version(),
    )
    log.info("mcp_stdio_starting", provider=settings.llm_provider)
    try:
        await server.run_stdio_async()
    finally:
        await bus.close()
        await engine.dispose()


async def _arq_pool(settings: Any) -> Any:
    """The queue, or None.

    None is a real state rather than a failure: `ControlPlane.create_run` writes the row
    either way and logs that the job was not enqueued, so a worker polling the table still
    picks it up. Refusing to start would turn a degraded queue into an editor that cannot
    even read a finished run.
    """
    from api.main import build_arq_pool

    try:
        return await build_arq_pool(settings)
    except Exception as e:
        log.error("arq_pool_failed", error=f"{type(e).__name__}: {e}")
        return None


def _version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        return pkg_version("autoswe")
    except PackageNotFoundError:  # running from a source tree that was never installed
        return "0"


def main() -> None:
    """Console-script entrypoint."""
    asyncio.run(serve())


if __name__ == "__main__":
    main()
