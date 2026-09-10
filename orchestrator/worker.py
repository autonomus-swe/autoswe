"""arq worker: one job per run."""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from arq.connections import RedisSettings

from core.errors import AutosweError, ConfigError
from core.settings import Settings, get_settings
from observability.logging import configure_logging, get_logger
from observability.tracing import configure_tracing
from orchestrator.deps import Deps
from orchestrator.runner import run
from orchestrator.state import DEFAULT_TEST_COMMAND, Phase, RunState
from storage import repo as db
from storage.db import session

log = get_logger(__name__)


async def load_initial_state(engine: Any, run_id: UUID) -> RunState:
    async with session(engine) as s:
        row = await db.get_run(s, run_id)
    if row is None:
        raise AutosweError(f"run {run_id} not found")
    return RunState(
        run_id=row.id,
        goal=row.goal,
        repo_url=row.repo_url,
        base_branch=row.base_branch,
        work_branch=row.work_branch,
        phase=Phase.SETUP,
        test_command=DEFAULT_TEST_COMMAND,
    )


async def run_job(ctx: dict[str, Any], run_id: str) -> str:
    settings = get_settings()
    settings.require_worker()
    deps = Deps.build(settings)
    try:
        state = await load_initial_state(deps.engine, UUID(run_id))
        final = await run(state, deps)
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


async def configure_worker(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    # Validate before touching global state: a worker missing its secrets should refuse
    # to start rather than accept a run and die halfway through it.
    settings.require_worker()
    apply_ca_bundle(settings)
    configure_tracing("autoswe-worker")
    log.info(
        "worker_started",
        provider=settings.llm_provider,
        model=settings.llm_model,
        sandbox_image=settings.sandbox_image,
    )


class WorkerSettings:
    functions = [run_job]  # noqa: RUF012
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 2
    job_timeout = 60 * 60
    max_tries = 1  # Phase 2 raises this once checkpoints allow resume
    on_startup = configure_worker
