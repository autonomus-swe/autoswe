"""Which model a role gets, and the one condition that changes it.

Two things here are easy to get wrong in a way that leaves a feature looking implemented
while doing nothing, and both have already happened once:

- **A downgrade table keyed by a name that is not a role.** The phase document writes
  `"decompose"`; the role is `"decomposer"`. That entry matches nothing, so the decomposer
  stays on the expensive tier and every test of the *mechanism* still passes.
- **A tier nothing resolves to a model.** Before this step `Route.tier` was read by no
  production code at all — only `Route.effort` was — so routing a role to a cheaper tier
  changed precisely nothing about which model ran.

So the tests below check the table by role name, and check that a downgrade reaches the
request and the ledger rather than stopping at the router.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest

from contracts import Budget, Usage
from gateway.provider import NullHooks
from gateway.routing import ANTHROPIC_MODELS, DOWNGRADE, ROUTES, Route, route_for
from tests.fakes import FakeProviderBase

pytestmark = pytest.mark.unit


def budget(max_usd: float = 10.0) -> Budget:
    return Budget(max_usd=max_usd)


# ---- the table ---------------------------------------------------------------------------


def test_every_role_that_downgrades_is_a_role() -> None:
    """The bug the phase document has: `"decompose"` is not a key of `ROUTES`, so that
    entry would never fire and the decomposer would quietly stay on opus."""
    assert set(DOWNGRADE) <= set(ROUTES), sorted(set(DOWNGRADE) - set(ROUTES))


def test_a_downgrade_is_actually_cheaper_than_what_it_replaces() -> None:
    """A table entry that maps a role to the tier it already had is a no-op with the
    appearance of policy."""
    order = ["haiku", "sonnet", "opus"]
    for role, tier in DOWNGRADE.items():
        assert order.index(tier) < order.index(ROUTES[role].tier), role


def test_every_tier_names_a_model_the_anthropic_provider_knows() -> None:
    for role, route in ROUTES.items():
        assert route.tier in ANTHROPIC_MODELS, role


@pytest.mark.parametrize("role", ["coder", "debugger"])
def test_the_roles_that_do_the_work_never_downgrade(role: str) -> None:
    """A cheaper Coder that needs three attempts is not cheaper: it is the same money over
    three times the tool calls, plus a debug loop that would not otherwise have run."""
    assert role not in DOWNGRADE
    assert route_for(role, downgrade=True) == ROUTES[role]


@pytest.mark.parametrize("role", ["tester", "pr_writer", "review_pre", "security", "analyzer"])
def test_roles_already_near_the_floor_are_left_alone(role: str) -> None:
    assert route_for(role, downgrade=True) == ROUTES[role]


@pytest.mark.parametrize(("role", "tier"), sorted(DOWNGRADE.items()))
def test_the_roles_that_do_downgrade_do(role: str, tier: str) -> None:
    downgraded = route_for(role, downgrade=True)

    assert downgraded.tier == tier
    assert downgraded.effort == ROUTES[role].effort, "effort is not what got expensive"


def test_nothing_downgrades_while_there_is_money() -> None:
    assert all(route_for(role) == ROUTES[role] for role in ROUTES)


# ---- when it fires -------------------------------------------------------------------------


def test_the_line_is_the_budget_warning_fraction() -> None:
    b = budget(max_usd=10.0)  # warns at 0.9

    assert not b.past_cost_warning(Usage(cost_usd=8.99), elapsed_s=0)
    assert b.past_cost_warning(Usage(cost_usd=9.0), elapsed_s=0), "at the line, not past it"
    assert b.past_cost_warning(Usage(cost_usd=50.0), elapsed_s=0), "and over it"


def test_running_out_of_time_does_not_downgrade() -> None:
    """A cheaper model is not a faster one, and on a hard task it is slower. Downgrading
    on wall clock would spend the remaining minutes worse."""
    b = budget()

    assert not b.past_cost_warning(Usage(cost_usd=0.10), elapsed_s=b.wall_clock_s * 0.99)


def test_a_model_with_no_price_does_not_downgrade() -> None:
    """An unpriced model has not cost nothing — we cannot say. Downgrading on a number
    nobody has is worse than not downgrading."""
    b = budget()

    assert not b.past_cost_warning(Usage(cost_usd=0.0), elapsed_s=0, cost_measurable=False)


def test_a_refund_puts_the_role_back_on_the_better_model() -> None:
    """The condition is read fresh at every step rather than latched. `state.usage` is
    reconciled from `llm_calls`, so it moves in both directions — a provider correcting a
    charge should not leave the run punished for the rest of its life."""
    b = budget()
    over = Usage(cost_usd=9.5)
    corrected = Usage(cost_usd=4.0)

    assert route_for("planner", downgrade=b.past_cost_warning(over, 0)).tier == "sonnet"
    assert route_for("planner", downgrade=b.past_cost_warning(corrected, 0)).tier == "opus"


# ---- the tier has to reach the model -------------------------------------------------------


def provider(**models: str) -> Any:
    from gateway.openai_compat_provider import OpenAICompatProvider

    return OpenAICompatProvider(
        model="default/model", api_key="k", base_url="https://openrouter.ai/api/v1", models=models
    )


def test_a_tier_with_no_configured_model_falls_back_to_the_default() -> None:
    """A single-model endpoint is the normal case. Failing a run because the router asked
    for `sonnet` would be absurd."""
    p = provider()

    assert p.model_for("sonnet") == "default/model"
    assert p.model_for(None) == "default/model"
    assert p.model_for("a tier that does not exist") == "default/model"


def test_a_configured_tier_resolves_to_its_own_model() -> None:
    p = provider(opus="big/model", sonnet="medium/model")

    assert p.model_for("opus") == "big/model"
    assert p.model_for("sonnet") == "medium/model"
    assert p.model_for("haiku") == "default/model", "unset falls back rather than erroring"


def test_an_empty_setting_is_the_same_as_an_unset_one() -> None:
    """The settings are three optional strings threaded through as `"" if None`, and an
    empty model id would be sent to the API verbatim."""
    p = provider(opus="", sonnet="medium/model")

    assert p.model_for("opus") == "default/model"


async def test_the_request_is_sent_to_the_tier_that_was_routed() -> None:
    """The step that makes any of this real. Before it, `Route.tier` was read by no
    production code and a downgrade changed nothing about which model ran."""
    from gateway.provider import Request

    p = provider(sonnet="medium/model")
    sent: list[dict[str, Any]] = []

    async def fake_complete(**kwargs: Any) -> Any:
        sent.append(kwargs)
        raise RuntimeError("stop here; the model choice is what is under test")

    p._complete = fake_complete
    with pytest.raises(RuntimeError):
        await p.parse(Request(role="planner", system="s", tier="sonnet"), Budget)

    assert sent[0]["model"] == "medium/model"


def test_usage_is_priced_against_the_model_that_actually_answered() -> None:
    """Pricing a Sonnet answer at Opus rates would make the downgrade look like it saved
    nothing, which is the one number the whole feature is judged on. The provider passes
    the *request's* model to `usage_from` for exactly this reason."""
    from gateway.openai_compat_provider import usage_from

    resp = type(
        "R",
        (),
        {"usage": type("U", (), {"prompt_tokens": 1_000_000, "completion_tokens": 0})()},
    )()

    opus = usage_from(resp, ANTHROPIC_MODELS["opus"], None)
    sonnet = usage_from(resp, ANTHROPIC_MODELS["sonnet"], None)

    assert opus.cost_usd == pytest.approx(5.0)
    assert sonnet.cost_usd == pytest.approx(2.0)
    assert sonnet.cost_usd < opus.cost_usd, "the downgrade has to show up in the money"


def test_a_route_is_hashable_and_comparable() -> None:
    """`_route` compares the result against `ROUTES[role]` to decide whether to log a
    downgrade, which silently never fires if `Route` stops being a value."""
    assert Route("opus", "high") == Route("opus", "high")
    assert Route("sonnet", "high") != Route("opus", "high")


# ---- what the run says about it --------------------------------------------------------


class Deps:
    """Just enough of `Deps` for the two functions under test."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.engine = None
        self.bus = None
        self.events: list[tuple[str, dict[str, Any]]] = []


def test_a_single_model_deployment_reports_no_downgrade() -> None:
    """Real policy with no effect. Reporting the roles as downgraded would claim a saving
    that was never made, and the first person to check the event against the ledger would
    stop trusting both."""
    from orchestrator.nodes import _downgraded_roles

    assert _downgraded_roles(cast(Any, Deps(provider()))) == []


def test_a_multi_model_deployment_names_the_roles_that_moved() -> None:
    from orchestrator.nodes import _downgraded_roles

    deps = Deps(provider(opus="big/model", sonnet="medium/model"))

    assert _downgraded_roles(cast(Any, deps)) == sorted(DOWNGRADE)


def test_only_the_roles_whose_model_actually_changes_are_named() -> None:
    """A deployment that configures opus and leaves sonnet unset resolves both to
    different models; one that configures neither resolves both to the default."""
    from orchestrator.nodes import _downgraded_roles

    assert _downgraded_roles(cast(Any, Deps(provider(sonnet="medium/model")))) == sorted(DOWNGRADE)
    assert _downgraded_roles(cast(Any, Deps(provider(haiku="small/model")))) == []


async def test_the_budget_warning_says_which_roles_were_downgraded() -> None:
    """A reader who sees the next few steps cost less and read differently, with nothing
    saying why, reads it as the run degrading on its own."""
    from orchestrator import nodes
    from orchestrator.state import RunState

    deps = Deps(provider(opus="big/model", sonnet="medium/model"))
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(_deps: Any, _run_id: Any, type: str, payload: dict[str, Any]) -> None:
        emitted.append((type, payload))

    state = RunState(
        run_id=uuid4(),
        goal="g",
        repo_url="https://github.com/a/b",
        base_branch="main",
        work_branch="agent/x",
    )
    original = nodes._emit
    nodes._emit = emit  # type: ignore[assignment]
    try:
        gate = nodes._budget_gate(state, cast(Any, deps))
        assert gate.on_warning is not None
        await gate.on_warning("budget_usd", 0.95)
        await gate.on_warning("budget_wall_clock", 0.95)
    finally:
        nodes._emit = original

    kinds = {payload["kind"]: payload for _type, payload in emitted}
    assert kinds["budget_usd"]["downgraded"] == sorted(DOWNGRADE)
    assert "downgraded" not in kinds["budget_wall_clock"], "time is not helped by a cheaper model"


def test_the_models_a_run_could_reach_include_every_tier() -> None:
    """Checking only `provider.model` would miss the tier a downgrade sends work to."""
    from orchestrator.resume import reachable_models

    assert reachable_models(provider(opus="big/model", sonnet="medium/model")) == [
        "big/model",
        "default/model",
        "medium/model",
    ]


def test_an_unpriced_tier_makes_the_whole_run_unmeasurable() -> None:
    """The dollar figure has to be trustworthy or admitted to be absent. A deployment
    whose cheap tier is free would otherwise report a confident total that silently
    excludes every call that went there."""
    from orchestrator.resume import unpriced_models

    priced_everywhere = provider(
        opus=ANTHROPIC_MODELS["opus"],
        sonnet=ANTHROPIC_MODELS["sonnet"],
        haiku=ANTHROPIC_MODELS["haiku"],
    )
    priced_everywhere.model = ANTHROPIC_MODELS["opus"]
    mixed = provider(opus=ANTHROPIC_MODELS["opus"], sonnet="something-nobody-priced")
    mixed.model = ANTHROPIC_MODELS["opus"]

    assert unpriced_models(priced_everywhere, None) == []
    assert unpriced_models(mixed, None) == ["something-nobody-priced"]


def test_a_free_tier_is_a_measured_zero_rather_than_a_missing_price() -> None:
    """`:free` and local endpoints genuinely cost nothing, so they do not make a run
    unmeasurable — the distinction the pricing table already draws."""
    from orchestrator.resume import unpriced_models

    free = provider(sonnet="some-model:free")
    free.model = ANTHROPIC_MODELS["opus"]

    assert unpriced_models(free, None) == []


# ---- the join between the router and the request ---------------------------------------


class Recorder(FakeProviderBase):
    """Records the request rather than answering it."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def parse(self, req: Any, output: Any) -> Any:
        self.requests.append(req)
        return output(), Usage()

    async def run_tools(self, req: Any, tools: Any, ctx: Any, hooks: Any) -> Any:
        from gateway.provider import RunOutcome

        self.requests.append(req)
        return RunOutcome("", 1, Usage(), "end_turn")


async def test_the_tier_an_agent_was_built_with_reaches_its_request() -> None:
    """The join. `_route` can pick the right tier and the provider can resolve it
    correctly, and the feature still does nothing if the agent drops it in between."""
    from agents.base import Agent

    class Probe(Agent):
        role = "planner"
        prompt_file = "planner"

    provider = Recorder()
    await Probe(tier="sonnet").run_structured(cast(Any, provider), "go", Usage)
    await Probe(tier="sonnet").run_tools(
        cast(Any, provider), cast(Any, None), "go", cast(Any, NullHooks())
    )

    assert [r.tier for r in provider.requests] == ["sonnet", "sonnet"]


async def test_an_agent_built_without_a_tier_asks_for_the_default() -> None:
    """Every agent in the tests constructs one that way, and so does any caller written
    before routing could change it."""
    from agents.base import Agent

    class Probe(Agent):
        role = "planner"
        prompt_file = "planner"

    provider = Recorder()
    await Probe().run_structured(cast(Any, provider), "go", Usage)

    assert provider.requests[0].tier is None
    assert provider.model_for(provider.requests[0].tier) == "test/model"
