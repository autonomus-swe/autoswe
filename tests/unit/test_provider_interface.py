"""The boundary Phase 6 has to come through.

Phase 5's exit checklist asks that "the provider interface has not grown Anthropic-specific
parameters; Phase 6's provider must implement `parse()` and `run_tools()` only". That is an
architectural claim, and the way it is normally checked — a person reading the file — stops
happening the moment someone is mid-way through adding a provider and needs one more field.

It is worth pinning because the failure is silent and expensive. `thinking`, `betas` or a
`cache_control` list on `Request` would each work fine while there is one provider, and
would each be a thing the *next* provider has to emulate or ignore. The interface stops
being provider-agnostic long before anyone notices, and by then Phase 6 is built on it.

The tests below are deliberately written as "this exact set", not "does not contain
`thinking`". A denylist only catches the Anthropic parameters I thought of today; an
allowlist fails on whatever actually gets added, which is the point. When a field genuinely
belongs here, the fix is to add it to the list and say why in the commit — thirty seconds,
and it leaves a record of a decision that would otherwise be invisible.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from gateway.provider import LLMProvider, Request, RunOutcome

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# Every SDK this repository could plausibly grow a dependency on. The rule is not "no SDKs"
# — `openai_compat_provider` obviously needs one — it is that nothing *above* the gateway
# boundary may import one.
SDKS = frozenset({"openai", "anthropic", "google", "cohere", "mistralai", "litellm"})

# The directories that must stay SDK-free. `gateway/` is excluded on purpose: it is the
# layer whose job is to know about SDKs.
SDK_FREE = ("agents", "orchestrator", "tools", "repo", "contracts")


def modules(package: str) -> list[Path]:
    return sorted(p for p in (ROOT / package).rglob("*.py") if "__pycache__" not in p.parts)


def imported_roots(path: Path) -> set[str]:
    """Top-level package names imported by a module, including inside functions.

    Walks the whole tree rather than reading only module-level statements, because a lazy
    `import openai` inside a method is exactly how this boundary would be crossed by
    someone trying not to pay an import cost.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


# ---- the request shape ----------------------------------------------------------------


def test_the_request_carries_only_provider_agnostic_fields() -> None:
    """Each of these is a question any chat completion API can answer.

    `tier` is the interesting one: it is deliberately a *tier* and not a model id, so the
    router never has to know that one deployment calls the middle tier `claude-sonnet-5`
    and another calls it `qwen2.5:3b`. That indirection is what keeps this list short.
    """
    assert {f.name for f in dataclasses.fields(Request)} == {
        "role",
        "system",
        "tier",
        "run_block",
        "messages",
        "max_tokens",
        "max_iterations",
        "must_call",
        "task_budget_tokens",
    }


def test_the_outcome_says_why_it_stopped_in_words_every_provider_can_produce() -> None:
    """`stop_reason` is a plain string rather than an SDK enum for the same reason the rest
    of this file exists: an enum imported from one vendor's package makes every other
    provider translate into that vendor's vocabulary."""
    assert {f.name for f in dataclasses.fields(RunOutcome)} == {
        "final_text",
        "turns",
        "usage",
        "stop_reason",
    }
    assert RunOutcome.__annotations__["stop_reason"] == "str"


def test_no_anthropic_concept_has_leaked_into_the_request() -> None:
    """The specific shapes this checklist item was written about.

    Kept alongside the allowlist above rather than instead of it: the allowlist catches
    anything new, and this one names the things that would be tempting, so a failure here
    reads as "you are re-introducing the coupling" rather than "update the list".
    """
    fields = {f.name for f in dataclasses.fields(Request)}

    for leak in ("thinking", "betas", "cache_control", "system_blocks", "extra_headers"):
        assert leak not in fields, f"{leak} is an Anthropic SDK concept, not a provider one"


# ---- the protocol surface ----------------------------------------------------------------


def test_a_phase_6_provider_implements_three_things() -> None:
    """The checklist says `parse()` and `run_tools()`. There is a third, `model_for`, added
    when budget-aware routing landed, and it belongs here: only the provider knows which
    model ids its endpoint will accept.

    It is asserted rather than glossed over because the checklist's number is now wrong,
    and a stale checklist that nobody reconciles is how an interface grows a fourth method
    without discussion.
    """
    required = {
        name
        for name in LLMProvider.__protocol_attrs__  # type: ignore[attr-defined]
        if not name.startswith("_")
    }

    assert required == {"provider_name", "model", "model_for", "parse", "run_tools"}


# ---- the layering ---------------------------------------------------------------------


@pytest.mark.parametrize("package", SDK_FREE)
def test_nothing_above_the_gateway_imports_a_model_sdk(package: str) -> None:
    """`gateway/provider.py` opens by saying agents depend on it and never on an SDK. This
    is that sentence, enforced.

    The cost of losing it is not abstract: an agent that imports `openai` to type a
    parameter pins the whole system to one vendor's response objects, and the Phase 6
    provider then has to produce them.
    """
    offenders = {
        str(path.relative_to(ROOT)): sorted(imported_roots(path) & SDKS)
        for path in modules(package)
        if imported_roots(path) & SDKS
    }

    assert offenders == {}, f"{package} must reach models through gateway.provider only"


def test_the_gateway_is_where_the_sdk_is_allowed_to_be() -> None:
    """The control for the test above.

    Without it, that test passes just as well if `SDKS` is misspelled, if `imported_roots`
    silently returns nothing, or if someone deletes the provider — all of which look
    identical to "the layering is clean". Something, somewhere, must import an SDK.
    """
    users = {
        str(path.relative_to(ROOT)) for path in modules("gateway") if imported_roots(path) & SDKS
    }

    assert users == {"gateway/openai_compat_provider.py"}, users
