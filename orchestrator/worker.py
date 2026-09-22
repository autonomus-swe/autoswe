"""arq worker: one job per run."""

from __future__ import annotations

import os
import socket
from typing import Any
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.errors import ConfigError
from core.settings import Settings, get_settings
from observability.logging import configure_logging, get_logger
from observability.tracing import configure_tracing
from orchestrator.deps import Deps
from orchestrator.gc import run_gc
from orchestrator.nodes import RunResources
from orchestrator.resume import load_state, reattach
from orchestrator.runner import run

log = get_logger(__name__)


def worker_id() -> str:
    """Identifies this process as the holder of a repo lock."""
    return f"{socket.gethostname()}:{os.getpid()}"


async def run_job(ctx: dict[str, Any], run_id: str) -> str:
    """One run. Resumes from its last checkpoint, or starts fresh when there is none."""
    settings = get_settings()
    settings.require_worker()
    deps = Deps.build(settings)
    res = RunResources()
    try:
        state, resumed = await load_state(deps, UUID(run_id))
        if resumed:
            log.info("resuming", run_id=run_id, phase=state.phase.value, seq=state.seq)
            await reattach(state, deps, res, worker_id())
        final = await run(state, deps, res)
        return final.phase.value
    finally:
        await deps.aclose()


def apply_ca_bundle(settings: Settings) -> None:
    """Make ``requests`` and ``ssl`` trust the same CAs the OS does, when asked.

    ``requests`` (and so PyGithub) reads ``REQUESTS_CA_BUNDLE``; the stdlib reads
    ``SSL_CERT_FILE``. Anything already exported by the operator wins.
    """
    if settings.ca_bundle is None:
        return
    path = str(settings.ca_bundle)
    if not settings.ca_bundle.is_file():
        raise ConfigError(f"CA_BUNDLE does not exist: {path}")
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        os.environ.setdefault(var, path)
    log.info("ca_bundle_applied", path=path)


def start_metrics_server(port: int) -> None:
    """Expose this worker's registry, if a port was asked for.

    The worker has no HTTP server of its own, so `prometheus_client` brings one up on a
    background thread. A failure here is logged and ignored: a port already taken — the
    normal case when a second worker starts on the same host — is a reason to lose the
    metrics, not a reason to refuse the runs.
    """
    if port <= 0:
        return
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
        log.info("metrics_server_started", port=port)
    except Exception as e:
        log.warning("metrics_server_failed", port=port, error=f"{type(e).__name__}: {e}")


async def mount_mcp_servers(ctx: dict[str, Any], settings: Settings) -> None:
    """Connect to the external MCP servers this deployment configures, and register their
    tools.

    Once per worker process rather than once per run: the sessions are subprocesses, and
    starting `npx @modelcontextprotocol/server-github` for every run would add seconds to
    each and leave a process behind whenever one was cancelled.

    A bad *configuration* stops the worker — it is a file somebody wrote, and a worker that
    starts anyway would be silently running without the tools that file asked for, or
    worse, with a mutating tool routed somewhere it should not be. A server that will not
    *connect* does not: that is the network, and the right response is to run without it.
    """
    from mcp_bridge import config as mcp_config
    from mcp_bridge.client import MCPServers
    from tools import registry

    configs = mcp_config.load(settings.mcp_servers_file)
    if not configs:
        return
    servers = MCPServers(configs)
    ctx["mcp_servers"] = servers
    registry.register(await servers.start())


async def configure_worker(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    # Validate before touching global state: a worker missing its secrets should refuse
    # to start rather than accept a run and die halfway through it.
    settings.require_worker()
    apply_ca_bundle(settings)
    configure_tracing("autoswe-worker")
    start_metrics_server(settings.metrics_port)
    await mount_mcp_servers(ctx, settings)
    log.info(
        "worker_started",
        provider=settings.llm_provider,
        model=settings.llm_model,
        sandbox_image=settings.sandbox_image,
    )


async def shutdown_worker(ctx: dict[str, Any]) -> None:
    """Close the MCP sessions, which are child processes this worker started."""
    servers = ctx.get("mcp_servers")
    if servers is not None:
        await servers.aclose()


class _RedisOnlySettings(BaseSettings):
    """Just the Redis DSN, read without validating anything else.

    arq builds its pool from ``WorkerSettings.__dict__`` (see ``arq.worker.get_kwargs``),
    so ``redis_settings`` has to be a real value at import time — a descriptor is read
    straight out of the class dict and never resolved. Going through the full ``Settings``
    here would make importing this module require a complete configuration, which broke
    CI and any fresh clone. The worker still validates everything properly in
    ``configure_worker`` before it accepts a job.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    redis_url: str = "redis://localhost:6379/0"


class WorkerSettings:
    functions = [run_job]  # noqa: RUF012
    # Every ten minutes. `unique=True` is arq's default and is what matters with more than
    # one worker: the job is claimed once per tick rather than once per worker, so two
    # workers do not race to remove the same directory.
    cron_jobs = [  # noqa: RUF012
        cron(run_gc, minute={0, 10, 20, 30, 40, 50}, run_at_startup=False, max_tries=1)
    ]
    redis_settings = RedisSettings.from_dsn(_RedisOnlySettings().redis_url)
    max_jobs = 2
    job_timeout = 50 * 60
    max_tries = 3  # a retry resumes from the last checkpoint
    retry_jobs = True
    on_startup = configure_worker
    on_shutdown = shutdown_worker
