"""The MCP server mounted on the API at `/mcp`, over streamable HTTP.

The same tools the stdio entrypoint serves, reachable from a client that cannot launch a
child process — a hosted editor, or anything talking to the control plane over the network
rather than sharing a machine with it.

## Why the key check is here and not a FastAPI dependency

The MCP transport is a separate ASGI application, and FastAPI's dependency tree stops at
that boundary: a `Depends(require_api_key)` on the router that attaches it is never
consulted for requests inside it. Attaching the MCP app without noticing that would have
published every tool — including `cancel_run` — unauthenticated, while the routes beside
it stayed guarded and the mistake looked like nothing at all. So the check is ASGI-level,
wrapping the app itself, and `tests/integration/test_mcp_server.py` asserts a 401 without
a key precisely because the failure mode is silent.

The key is read from the `X-API-Key` header, the same one every other route uses. There is
no query-parameter fallback: that exists for `EventSource`, which cannot set headers, and
an MCP client is not a browser.

## Routes rather than a mount

`app.mount("/mcp", …)` would only ever match `/mcp/…`; a request to `/mcp` itself does not
match the mount, so Starlette answers it with a 307 to `/mcp/`. `http://host/mcp` is what
goes in an editor's configuration, and a client that does not follow a redirect on a POST
sees the redirect rather than the server. Two exact routes instead, and the wrapper pins
the path the inner app routes on, so `/mcp` and `/mcp/` both reach it directly.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import orjson
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from api.service import ControlPlane, plane_from
from core.settings import Settings
from mcp_bridge.server import build_server
from observability.logging import get_logger

log = get_logger(__name__)

MOUNT_PATH = "/mcp"
# What the inner Starlette app routes on. The wrapper rewrites every request to this, so
# the outer path (`/mcp` or `/mcp/`) is the caller's choice rather than a redirect.
INNER_PATH = "/mcp"
METHODS = ["GET", "POST", "DELETE"]  # streamable HTTP: POST sends, GET streams, DELETE ends
UNAUTHORIZED = orjson.dumps({"detail": "unauthorized"})


class RequireApiKey:
    """ASGI middleware: reject anything without a configured `X-API-Key`.

    Written as a plain ASGI app rather than a Starlette `BaseHTTPMiddleware` because the
    MCP transport streams responses and `BaseHTTPMiddleware` buffers them through an
    intermediate task, which breaks server-sent events.
    """

    def __init__(self, app: Any, keys: frozenset[str]) -> None:
        self.app = app
        self.keys = keys

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self._authorised(scope):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(UNAUTHORIZED)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": UNAUTHORIZED})
            return
        await self.app({**scope, "path": INNER_PATH}, receive, send)

    def _authorised(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() != b"x-api-key":
                continue
            candidate = value.decode("latin-1")
            # compare_digest against every key so the time taken does not reveal which
            # matched — the same rule as `api.auth._matches`.
            return any(hmac.compare_digest(candidate, known) for known in self.keys)
        return False


def mount(app: Any, settings: Settings) -> Any:
    """Attach the MCP app at `/mcp` and return the server, for the lifespan to run.

    The returned server's `session_manager` must be entered by the caller's lifespan: it
    owns the task group every streamable-HTTP session runs in, and a sub-application's own
    lifespan is not run by the parent. `mcp_lifespan` below is that, as a context manager.

    `worktrees_dir` is passed even though the API usually runs on a different host from the
    worker: the tools it enables report a missing checkout rather than failing obscurely,
    and in single-host development — which is how the demo runs — the path is real.
    """
    server = build_server(
        lambda: _plane(app),
        provider=settings.llm_provider,
        worktrees_dir=settings.worktrees_dir,
        version=app.version,
    )
    guarded = RequireApiKey(
        server.streamable_http_app(
            streamable_http_path=INNER_PATH, transport_security=_transport_security(settings)
        ),
        settings.api_keys,
    )
    for path in (MOUNT_PATH, f"{MOUNT_PATH}/"):
        app.router.routes.append(Route(path, endpoint=guarded, methods=METHODS, name=f"mcp:{path}"))
    log.info("mcp_mounted", path=MOUNT_PATH)
    return server


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """Host-header validation, off unless `MCP_ALLOWED_HOSTS` names what to allow.

    Passed explicitly rather than left to the SDK, which turns the check on with a
    localhost-only allow-list whenever `host` is not given — so the first deployment
    behind a real hostname answers every MCP request with a bare 421 and nothing in it
    points at the setting that would fix it. The reasoning for the default is on the
    setting itself; the short version is that this transport has no ambient authority for
    a rebound DNS name to borrow.
    """
    allowed = settings.mcp_allowed_hosts
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed), allowed_hosts=list(allowed)
    )


@asynccontextmanager
async def mcp_lifespan(server: Any) -> AsyncIterator[None]:
    """Run the MCP session manager for as long as the app is up."""
    async with server.session_manager.run():
        yield


def _plane(app: Any) -> ControlPlane:
    return plane_from(app.state)
