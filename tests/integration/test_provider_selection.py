"""A run chooses its provider, and the worker builds the one the row names.

`runs.provider` has been a column since Phase 1 and nothing read it. The worker built from
`LLM_PROVIDER` whatever the row said, so the value was a note rather than a decision — and
`llm_calls.provider`, which the Phase 6 criterion asks to inspect, was evidence of the
process's configuration rather than of the run's.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from contracts import Budget
from core.settings import get_settings, load_settings
from gateway import providers
from orchestrator.deps import Deps
from orchestrator.worker import fail_run, run_job, run_provider
from storage import repo as db
from storage.db import session
from tests.integration.conftest import api_app

pytestmark = pytest.mark.integration
KEY = "test-key-123456"
BODY = {"repo_url": "https://github.com/acme/demo", "goal": "Implement subtract(a, b)"}


# ---- the API surface ---------------------------------------------------------------------


async def test_a_run_records_the_provider_it_asked_for(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployment default is set to something else on purpose.

    With both the same, a route that ignored the body entirely would store the right value
    anyway and this would pass while proving nothing — which is what it did until a
    mutation that deleted `body.provider or` survived it.
    """
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    async with api_app(engine) as (client, _arq):
        res = await client.post(
            "/runs", json={**BODY, "provider": "openai_compat"}, headers={"X-API-Key": KEY}
        )
    assert res.status_code == 202
    async with session(engine) as s:
        row = await db.get_run(s, uuid.UUID(res.json()["run_id"]))
    assert row is not None and row.provider == "openai_compat"


async def test_a_run_without_one_takes_the_deployments_default(engine: AsyncEngine) -> None:
    async with api_app(engine) as (client, _arq):
        res = await client.post("/runs", json=BODY, headers={"X-API-Key": KEY})
    async with session(engine) as s:
        row = await db.get_run(s, uuid.UUID(res.json()["run_id"]))
    assert row is not None
    assert row.provider == load_settings(env_file=None).llm_provider


@pytest.mark.parametrize("provider", ["anthropic", "gpt5", ""])
async def test_a_provider_this_build_cannot_make_is_refused_at_the_door(
    engine: AsyncEngine, provider: str
) -> None:
    """422 rather than a queued run that dies in a worker the caller cannot see.

    `anthropic` is the interesting one: the plan is written for a build whose default it
    is, and this build has no such provider. The name is recognised and refused, and the
    message names what does work.
    """
    async with api_app(engine) as (client, arq):
        res = await client.post(
            "/runs", json={**BODY, "provider": provider}, headers={"X-API-Key": KEY}
        )
    assert res.status_code == 422
    assert "openai_compat" in res.text
    assert arq.jobs == []  # nothing was queued


# ---- the worker ---------------------------------------------------------------------------


async def make_run(engine: AsyncEngine, provider: str) -> uuid.UUID:
    async with session(engine) as s:
        return await db.create_run(
            s,
            repo_url="https://github.com/acme/demo",
            base_branch="main",
            goal="do the thing",
            budget=Budget(),
            provider=provider,
        )


async def test_the_worker_reads_the_provider_from_the_row(engine: AsyncEngine) -> None:
    """Read from the row rather than carried on `RunState`, because a resumed run loads a
    checkpoint and never reads the row — including checkpoints written before the column
    decided anything."""
    run_id = await make_run(engine, "openai_compat")
    assert await run_provider(engine, run_id) == "openai_compat"


async def test_a_run_that_does_not_exist_has_no_provider(engine: AsyncEngine) -> None:
    assert await run_provider(engine, uuid.uuid4()) is None


class NotTheOne:
    """Stands in for whatever provider the process happened to build.

    A sentinel rather than a second real provider, because this build has only one and a
    test that started from `openai_compat` and asked for `openai_compat` would pass with
    `using()` replaced by `return self` — which is exactly what a mutation showed.
    """

    provider_name = "not-the-one"
    model = "none"

    def model_for(self, tier: str | None) -> str:
        return self.model


def deps_with(engine: AsyncEngine, provider: object) -> Deps:
    return Deps(
        settings=load_settings(env_file=None),
        provider=provider,  # type: ignore[arg-type]
        engine=engine,
        bus=None,  # type: ignore[arg-type]
        sandbox_factory=None,  # type: ignore[arg-type]
    )


async def test_using_builds_the_named_provider_rather_than_keeping_what_it_had(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direction is the whole point. A deployment configured for one provider must run
    a run that chose another as the one it chose, or the ledger records a thing that did
    not happen."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    deps = deps_with(engine, NotTheOne())
    assert deps.provider.provider_name == "not-the-one"
    assert deps.using("openai_compat").provider.provider_name == "openai_compat"


async def test_using_the_same_provider_changes_nothing(engine: AsyncEngine) -> None:
    """A run that wants what the process already has should not pay for a second client,
    and — more to the point — should not end up with a different object than the one the
    rest of `Deps` was built around."""
    settings = load_settings(env_file=None)
    deps = deps_with(engine, providers.build(settings, "openai_compat"))
    assert deps.using("openai_compat") is deps
    assert deps.using(None) is deps


async def test_a_run_naming_an_unavailable_provider_is_failed_not_retried(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through `run_job` itself, not by calling `fail_run` directly.

    Calling the helper only proves the helper works: a mutation that deleted the call from
    `run_job` survived that version of this test. What matters is that the job takes this
    path at all — a row can outlive the deployment that wrote it, retrying will not make
    the provider appear, and arq burning three attempts leaves the run `queued` with
    nothing said to whoever started it.
    """
    monkeypatch.setenv("LLM_API_KEY", "not-used-on-this-path")
    monkeypatch.setenv("GITHUB_TOKEN", "not-used-on-this-path")
    get_settings.cache_clear()
    run_id = await make_run(engine, "anthropic")

    assert await run_job({}, str(run_id)) == "failed"

    async with session(engine) as s:
        row = await db.get_run(s, run_id)
    assert row is not None
    assert row.status == "failed"
    assert row.error is not None and "openai_compat" in row.error
    assert row.finished_at is not None

    # And whoever started it reads it from the API, not from the worker's log.
    async with api_app(engine) as (client, _arq):
        body = (await client.get(f"/runs/{run_id}", headers={"X-API-Key": KEY})).json()
    assert body["status"] == "failed" and "openai_compat" in body["error"]


async def test_fail_run_records_what_the_api_will_show(engine: AsyncEngine) -> None:
    """The helper on its own, so a failure in the test above points at the wiring rather
    than at this."""
    run_id = await make_run(engine, "anthropic")
    await fail_run(engine, run_id, "provider 'anthropic' is not available in this build")
    async with session(engine) as s:
        row = await db.get_run(s, run_id)
    assert row is not None and row.status == "failed" and row.finished_at is not None


# ---- the budget --------------------------------------------------------------------------


async def test_the_budget_a_caller_posts_reaches_the_state_the_worker_builds(
    engine: AsyncEngine,
) -> None:
    """The whole wire, because each half was individually fine and the join was not.

    The API wrote `runs.budget` and `initial_state` built a fresh `Budget()`, so the
    column was decoration for five phases: a caller asking for $3 got $10. Both halves
    read correctly on their own, which is why nothing caught it — and why this test spans
    them rather than checking either.
    """
    from orchestrator.resume import initial_state

    body = {
        "repo_url": "https://github.com/acme/demo",
        "goal": "Implement subtract(a, b)",
        "budget": {"max_usd": 3.0, "max_debug_attempts": 0},
    }
    async with api_app(engine) as (client, _arq):
        res = await client.post("/runs", json=body, headers={"X-API-Key": KEY})
    assert res.status_code == 202
    run_id = uuid.UUID(res.json()["run_id"])

    state = await initial_state(engine, run_id)
    assert state.budget.max_usd == 3.0, "a $3 run must not start with a $10 ceiling"
    assert state.budget.max_debug_attempts == 0, "the no-debugger ablation arm"


async def test_a_run_that_named_no_budget_starts_on_the_defaults(engine: AsyncEngine) -> None:
    from contracts import Budget as BudgetContract
    from orchestrator.resume import initial_state

    body = {"repo_url": "https://github.com/acme/demo", "goal": "Implement subtract(a, b)"}
    async with api_app(engine) as (client, _arq):
        res = await client.post("/runs", json=body, headers={"X-API-Key": KEY})
    state = await initial_state(engine, uuid.UUID(res.json()["run_id"]))
    assert state.budget == BudgetContract()
