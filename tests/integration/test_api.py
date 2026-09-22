"""Control plane against a real Postgres and Redis; the job queue is stubbed."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import api_app

pytestmark = pytest.mark.integration
KEY = "test-key-123456"


async def test_healthz_reports_dependencies(engine: AsyncEngine) -> None:
    async with api_app(engine) as (client, _arq):
        res = await client.get("/healthz")
        assert res.status_code == 200 and res.json()["checks"] == {"database": "ok", "redis": "ok"}


async def test_create_run_requires_a_valid_key(engine: AsyncEngine) -> None:
    async with api_app(engine) as (client, arq):
        body = {"repo_url": "https://github.com/acme/demo", "goal": "Implement subtract(a, b)"}
        for headers in ({}, {"X-API-Key": "wrong-key-000000"}):
            res = await client.post("/runs", json=body, headers=headers)
            assert res.status_code == 401 and res.json() == {"detail": "unauthorized"}
        assert arq.jobs == []


async def test_create_run_enqueues_and_is_readable(engine: AsyncEngine) -> None:
    async with api_app(engine) as (client, arq):
        body = {"repo_url": "https://github.com/acme/demo", "goal": "Implement subtract(a, b)"}
        res = await client.post("/runs", json=body, headers={"X-API-Key": KEY})
        assert res.status_code == 202
        run_id = res.json()["run_id"]
        assert arq.jobs == [("run_job", run_id)]

        got = await client.get(f"/runs/{run_id}", headers={"X-API-Key": KEY})
        assert got.status_code == 200
        summary = got.json()
        assert summary["status"] == "queued" and summary["phase"] == "setup"
        assert summary["work_branch"] == f"agent/{run_id}" and summary["cost_usd"] == 0.0
        assert summary["pr_url"] is None and summary["goal"] == body["goal"]

        missing = await client.get(
            f"/runs/{'0' * 8}-0000-0000-0000-{'0' * 12}", headers={"X-API-Key": KEY}
        )
        assert missing.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"repo_url": "https://gitlab.com/acme/demo", "goal": "Implement subtract(a, b)"},
        {"repo_url": "https://github.com/acme", "goal": "Implement subtract(a, b)"},
        {"repo_url": "https://github.com/acme/demo", "goal": "short"},
        {
            "repo_url": "https://github.com/acme/demo",
            "goal": "Implement subtract(a, b)",
            "extra": 1,
        },
        {
            "repo_url": "https://github.com/acme/demo",
            "goal": "Implement subtract",
            "base_branch": "a b",
        },
    ],
)
async def test_invalid_bodies_are_rejected(engine: AsyncEngine, body: dict[str, Any]) -> None:
    async with api_app(engine) as (client, _arq):
        res = await client.post("/runs", json=body, headers={"X-API-Key": KEY})
        assert res.status_code == 422


MISSING = f"{'0' * 8}-0000-0000-0000-{'0' * 12}"


async def test_list_runs_is_newest_first(engine: AsyncEngine) -> None:
    async with api_app(engine) as (client, _arq):
        goals = ["the first goal", "the second goal", "the third goal"]
        made = []
        for goal in goals:
            res = await client.post(
                "/runs",
                json={"repo_url": "https://github.com/acme/demo", "goal": goal},
                headers={"X-API-Key": KEY},
            )
            assert res.status_code == 202, res.text
            made.append(res.json()["run_id"])

        res = await client.get("/runs?limit=3", headers={"X-API-Key": KEY})
        assert res.status_code == 200
        listed = res.json()
        assert [r["run_id"] for r in listed] == list(reversed(made))
        assert [r["goal"] for r in listed] == list(reversed(goals))
        # the rail needs an age to tell runs with the same goal apart
        assert all(r["created_at"] for r in listed)


async def test_list_runs_needs_a_key_and_clamps_the_limit(engine: AsyncEngine) -> None:
    async with api_app(engine) as (client, _arq):
        assert (await client.get("/runs")).status_code == 401
        for limit in (0, -5, 10_000):
            res = await client.get(f"/runs?limit={limit}", headers={"X-API-Key": KEY})
            assert res.status_code == 200, res.text


async def test_run_detail_carries_everything_the_console_draws(engine: AsyncEngine) -> None:
    async with api_app(engine) as (client, _arq):
        body = {"repo_url": "https://github.com/acme/demo", "goal": "Implement subtract(a, b)"}
        run_id = (await client.post("/runs", json=body, headers={"X-API-Key": KEY})).json()[
            "run_id"
        ]

        res = await client.get(f"/runs/{run_id}/detail", headers={"X-API-Key": KEY})
        assert res.status_code == 200, res.text
        detail = res.json()
        assert detail["run"]["run_id"] == run_id
        # a fresh run has nothing yet, and every list must still be present and empty
        assert detail["tasks"] == detail["steps"] == detail["events"] == []
        assert detail["tool_calls"] == detail["llm_calls"] == []
        # Pinned as a whole dict rather than key by key: the console and the eval
        # harness both read this block, and a key that quietly disappears is a chart that
        # renders as zero rather than an error anyone sees.
        assert detail["totals"] == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_hit_rate": 0,
            "cost_usd": 0,
            "tool_calls": 0,
            "llm_calls": 0,
        }


async def test_run_detail_replays_events_with_the_time_they_happened(engine: AsyncEngine) -> None:
    """An SSE frame carries no timestamp, so the console reads them from here."""
    async with api_app(engine) as (client, _arq):
        from storage import repo as db
        from storage.db import session

        body = {"repo_url": "https://github.com/acme/demo", "goal": "Implement subtract(a, b)"}
        run_id = (await client.post("/runs", json=body, headers={"X-API-Key": KEY})).json()[
            "run_id"
        ]

        async with session(engine) as s:
            await db.insert_event(s, UUID(run_id), "phase_changed", {"phase": "analyze"})
            await db.insert_event(s, UUID(run_id), "awaiting_input", {"questions": ["which?"]})

        events = (await client.get(f"/runs/{run_id}/detail", headers={"X-API-Key": KEY})).json()[
            "events"
        ]
        assert [e["type"] for e in events] == ["phase_changed", "awaiting_input"]
        assert events[1]["payload"] == {"questions": ["which?"]}
        assert events[0]["id"] < events[1]["id"]
        assert all(e["ts"] for e in events)


async def test_detail_of_a_missing_run_is_404(engine: AsyncEngine) -> None:
    async with api_app(engine) as (client, _arq):
        res = await client.get(f"/runs/{MISSING}/detail", headers={"X-API-Key": KEY})
        assert res.status_code == 404 and res.json() == {"detail": "run not found"}


async def test_rate_limit_after_the_burst(engine: AsyncEngine) -> None:
    """The write bucket is small: starting runs costs money."""
    async with api_app(engine) as (client, _arq):
        codes = [
            (await client.post(f"/runs/{MISSING}/cancel", headers={"X-API-Key": KEY})).status_code
            for _ in range(6)
        ]
        assert codes[:5] == [404] * 5 and codes[5] == 429


async def test_reads_do_not_spend_the_write_budget(engine: AsyncEngine) -> None:
    """A console re-reads a run on every phase change; that must never block starting one."""
    async with api_app(engine) as (client, _arq):
        for _ in range(20):
            res = await client.get(f"/runs/{MISSING}", headers={"X-API-Key": KEY})
            assert res.status_code == 404, res.text
        # the write bucket is untouched, so a real write still gets through
        assert (
            await client.post(f"/runs/{MISSING}/cancel", headers={"X-API-Key": KEY})
        ).status_code == 404


# ---- artifacts -------------------------------------------------------------------------


async def _run_with_artifacts(engine: AsyncEngine) -> UUID:
    """A run with one of each interesting artifact kind, written the way a real run does."""
    from contracts import Budget
    from storage import repo as db
    from storage.db import session

    async with session(engine) as s:
        run_id = await db.create_run(
            s,
            repo_url="https://github.com/acme/demo",
            base_branch="main",
            goal="Implement subtract",
            budget=Budget(),
            provider="scripted",
        )
        await db.save_artifact(s, run_id, "diff", None, {"text": "--- a\n+++ b\n+x = 1\n"})
        await db.save_artifact(s, run_id, "test_report", None, {"passed": False, "total": 1})
        # Twice, so "the latest of that kind" is a claim the test can actually check.
        await db.save_artifact(s, run_id, "test_report", None, {"passed": True, "total": 41})
        await db.save_artifact(s, run_id, "security", None, {"checklist": {"no_secrets": True}})
        # The two the exit criterion names that this fixture used to omit — which is why
        # the PR description's only HTTP assertion was a negative one (a 404 for 'pr').
        await db.save_artifact(
            s, run_id, "review", None, {"report": {"findings": []}, "dropped": []}
        )
        await db.save_artifact(
            s, run_id, "pr", "https://example/pull/1", {"description": {"title": "feat: x"}}
        )
    return run_id


async def test_artifacts_require_a_key(engine: AsyncEngine) -> None:
    async with api_app(engine) as (client, _arq):
        run_id = await _run_with_artifacts(engine)

        for path in (f"/runs/{run_id}/artifacts", f"/runs/{run_id}/artifacts/diff"):
            res = await client.get(path)
            assert res.status_code == 401, path


async def test_the_listing_carries_sizes_and_not_content(engine: AsyncEngine) -> None:
    """A `diff` can be megabytes. Deciding whether to fetch one should not require
    fetching it."""
    async with api_app(engine) as (client, _arq):
        run_id = await _run_with_artifacts(engine)

        res = await client.get(f"/runs/{run_id}/artifacts", headers={"X-API-Key": KEY})

        assert res.status_code == 200
        rows = res.json()
        assert [r["kind"] for r in rows] == [
            "diff",
            "test_report",
            "test_report",
            "security",
            "review",
            "pr",
        ], "in the order the run wrote them, both test reports kept"
        assert all(r["size"] > 0 for r in rows)
        assert all("content" not in r for r in rows)


async def test_fetching_a_kind_returns_the_latest_of_it(engine: AsyncEngine) -> None:
    """A run that went round the TEST loop twice has two reports, and "what did it end up
    with" is the question this answers. The listing is where the sequence is visible."""
    async with api_app(engine) as (client, _arq):
        run_id = await _run_with_artifacts(engine)

        res = await client.get(f"/runs/{run_id}/artifacts/test_report", headers={"X-API-Key": KEY})

        assert res.status_code == 200
        assert res.json() == {"passed": True, "total": 41}, "the second write, not the first"


async def test_the_diff_comes_back_as_text_so_it_can_be_applied(engine: AsyncEngine) -> None:
    """JSON-escaping every line of a patch helps nobody: the caller pipes this to
    `git apply` or reads it."""
    async with api_app(engine) as (client, _arq):
        run_id = await _run_with_artifacts(engine)

        res = await client.get(f"/runs/{run_id}/artifacts/diff", headers={"X-API-Key": KEY})

        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/plain")
        assert res.text.startswith("--- a\n+++ b\n")


async def test_a_missing_run_and_a_missing_kind_are_told_apart(engine: AsyncEngine) -> None:
    """ "No such run" and "that run produced no security report" are different answers, and
    the second one is a real state — a run can fail before SECURITY."""
    async with api_app(engine) as (client, _arq):
        run_id = await _run_with_artifacts(engine)
        missing = UUID("00000000-0000-0000-0000-000000000000")

        absent = await client.get(f"/runs/{missing}/artifacts", headers={"X-API-Key": KEY})
        assert absent.status_code == 404 and absent.json()["detail"] == "run not found"

        # A kind the pipeline never writes. Using 'pr' here made the PR description's only
        # HTTP assertion a 404, which is the opposite of what the criterion asks.
        no_kind = await client.get(
            f"/runs/{run_id}/artifacts/neverwritten", headers={"X-API-Key": KEY}
        )
        assert no_kind.status_code == 404
        assert "no 'neverwritten' artifact" in no_kind.json()["detail"]


async def test_a_run_that_wrote_nothing_lists_nothing_rather_than_failing(
    engine: AsyncEngine,
) -> None:
    async with api_app(engine) as (client, _arq):
        from contracts import Budget
        from storage import repo as db
        from storage.db import session

        async with session(engine) as s:
            run_id = await db.create_run(
                s,
                repo_url="https://github.com/acme/demo",
                base_branch="main",
                goal="g",
                budget=Budget(),
                provider="scripted",
            )

        res = await client.get(f"/runs/{run_id}/artifacts", headers={"X-API-Key": KEY})

        assert res.status_code == 200 and res.json() == []


async def test_all_five_named_kinds_come_back(engine: AsyncEngine) -> None:
    """The exit criterion names five: diff, test reports, review, security, PR description.

    Asserted together because the endpoint is kind-generic — nothing in it knows the list —
    so the only thing that can go wrong is a producer writing under a name no caller would
    guess. The PR description is stored under kind `pr` with the description nested at
    `content["description"]`, which is exactly the sort of thing this pins.
    """
    async with api_app(engine) as (client, _arq):
        run_id = await _run_with_artifacts(engine)

        for kind in ("test_report", "review", "security", "pr"):
            res = await client.get(f"/runs/{run_id}/artifacts/{kind}", headers={"X-API-Key": KEY})
            assert res.status_code == 200, kind
            assert res.json(), kind

        text = await client.get(f"/runs/{run_id}/artifacts/diff", headers={"X-API-Key": KEY})
        assert text.status_code == 200 and text.text.startswith("--- a")

        pr = await client.get(f"/runs/{run_id}/artifacts/pr", headers={"X-API-Key": KEY})
        assert pr.json()["description"]["title"] == "feat: x", "the description, where it lives"


# ---- the scrape endpoint -------------------------------------------------------------------


async def test_metrics_needs_no_key(engine: AsyncEngine) -> None:
    """A scraper is infrastructure and cannot hold an API key. Unauthenticated like
    `/healthz`, and defensible only because every label is a role or an outcome."""
    async with api_app(engine) as (client, _arq):
        res = await client.get("/metrics")

        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/plain")
        assert "autoswe_runs_total" in res.text


async def test_metrics_is_not_in_the_public_schema(engine: AsyncEngine) -> None:
    """It is an operational endpoint, not part of the API anyone codes against."""
    async with api_app(engine) as (client, _arq):
        schema = (await client.get("/openapi.json")).json()

        assert "/metrics" not in schema["paths"]


async def test_run_detail_carries_the_cache_totals_the_eval_harness_needs(
    engine: AsyncEngine,
) -> None:
    """A run-level cache hit rate is the number Phase 5 leads with, and until now it could
    not be read through the API at all — the eval harness would have had to reach past the
    control plane into the database to compute it, which is the one thing that harness
    must not do."""
    from contracts import Budget, Usage
    from storage import repo as db
    from storage.db import session

    async with session(engine) as s:
        run_id = await db.create_run(
            s,
            repo_url="https://github.com/acme/demo",
            base_branch="main",
            goal="do the thing",
            budget=Budget(),
        )
        step_id = await db.start_step(s, run_id=run_id, task_id=None, agent="coder", phase="code")
        await db.insert_llm_call(
            s,
            step_id=step_id,
            provider="openai_compat",
            model="m",
            effort=None,
            usage=Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=3000),
            latency_ms=10,
            stop_reason="end_turn",
        )

    async with api_app(engine) as (client, _arq):
        totals = (await client.get(f"/runs/{run_id}/detail", headers={"X-API-Key": KEY})).json()[
            "totals"
        ]

    assert totals["input_tokens"] == 1000
    assert totals["cache_read_tokens"] == 3000
    # 3000 / (1000 + 3000). Against input + cache_read rather than every token, because
    # output tokens were never candidates for a cache hit.
    assert totals["cache_hit_rate"] == 0.75
