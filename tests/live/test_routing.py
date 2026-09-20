"""A budget downgrade reaching a different model, against a server that says which it used.

The unit tests prove the table is right and that the routed tier is carried into the
request. What they cannot prove is the last hop: that a deployment with two tiers
configured actually runs two different models, rather than resolving both to the same one
and reporting a saving nobody made.

Two local models stand in for two price tiers. They cost nothing and are the wrong *kind*
of difference — a 7B and a 3B rather than an expensive and a cheap hosted model — but the
mechanism under test is the resolution, not the price, and Ollama echoes the model it
actually served in every response.

This is why `_downgraded_roles` reports an empty list on a single-model deployment: the
policy is real and the effect is nil, and saying otherwise would be claiming a saving that
was never made.
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from gateway.openai_compat_provider import OpenAICompatProvider
from gateway.routing import DOWNGRADE, ROUTES, route_for

pytestmark = pytest.mark.live

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
BIG = "qwen2.5:7b"
SMALL = "qwen2.5:3b"


def _present() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
            names = {m.get("name", "") for m in json.load(r).get("models", [])}
        return {BIG, SMALL} <= names
    except Exception:
        return False


requires_two_models = pytest.mark.skipif(
    not _present(), reason=f"need both {BIG} and {SMALL} at {OLLAMA}"
)


def served_by(model: str) -> str:
    """The model the server says answered, which is the only witness that matters here."""
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 3}
    ).encode()
    request = urllib.request.Request(
        f"{OLLAMA}/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=180) as r:
        return str(json.load(r).get("model", ""))


@requires_two_models
def test_a_downgraded_role_resolves_to_a_different_model_that_really_answers() -> None:
    """The last hop. `model_for` could return two names and the server could serve one."""
    provider = OpenAICompatProvider(
        model=BIG,
        api_key=None,
        base_url=f"{OLLAMA}/v1",
        models={"opus": BIG, "sonnet": SMALL},
    )
    role = "planner"
    assert role in DOWNGRADE, "the fixture role stopped being one that downgrades"

    normal = provider.model_for(route_for(role).tier)
    downgraded = provider.model_for(route_for(role, downgrade=True).tier)

    assert normal == BIG and downgraded == SMALL
    assert served_by(normal) == BIG
    assert served_by(downgraded) == SMALL, "the server served something else"


@requires_two_models
def test_the_roles_that_do_the_work_keep_the_bigger_model_under_the_same_pressure() -> None:
    """A cheaper Coder that needs three attempts is not cheaper. The downgrade is a policy
    about which roles can afford to be worse, and this is the half that must not move."""
    provider = OpenAICompatProvider(
        model=BIG, api_key=None, base_url=f"{OLLAMA}/v1", models={"opus": BIG, "sonnet": SMALL}
    )

    for role in ("coder", "debugger"):
        assert provider.model_for(route_for(role, downgrade=True).tier) == BIG, role


@requires_two_models
def test_a_single_model_deployment_reports_no_downgrade() -> None:
    """The honesty check, against a real server rather than a stub: with one model
    configured every tier resolves to it, so there is no saving to claim."""
    provider = OpenAICompatProvider(model=BIG, api_key=None, base_url=f"{OLLAMA}/v1")

    resolved = {provider.model_for(ROUTES[role].tier) for role in DOWNGRADE}
    downgraded = {provider.model_for(tier) for tier in DOWNGRADE.values()}

    assert resolved == downgraded == {BIG}
    assert served_by(BIG) == BIG
