"""Which providers exist, which this build can actually construct, and how to build one.

Two lists rather than one, and the difference between them is the point. `KNOWN` is every
name the system has ever written into `runs.provider`; `AVAILABLE` is what this build can
turn into a working client today. A run row outlives a deployment, so the first list has
to keep accepting names the second one has dropped — otherwise a resumed run from last
month fails on a column value nobody can explain.

Its own module because both sides need it and neither should import the other: the API
validates a request against `AVAILABLE` before accepting it, and the worker builds from
the row. `orchestrator/deps.py` is worker-side and `api/` must not import it.

## Anthropic is known and unavailable

The Phase 6 plan is written for a build whose default is the Anthropic SDK. This one has
no Anthropic provider and is not getting one — the project runs on whatever is cheap or
free, and Phase 5 rewrote its own compaction criterion rather than take the dependency.
So `anthropic` stays a name this module recognises and refuses, with a message that says
which providers do work. A 422 at the API beats a `NotImplementedError` in a worker
thirty seconds after the caller was told the run had started.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.errors import ConfigError

if TYPE_CHECKING:
    from core.settings import Settings
    from gateway.provider import LLMProvider

KNOWN = ("openai_compat", "anthropic")
AVAILABLE = ("openai_compat",)


def unavailable(name: str) -> str | None:
    """Why this provider cannot be used here, or None when it can."""
    if name in AVAILABLE:
        return None
    if name in KNOWN:
        return (
            f"provider {name!r} is not available in this build; available: {', '.join(AVAILABLE)}"
        )
    return f"unknown provider {name!r}; available: {', '.join(AVAILABLE)}"


def open_model_preamble() -> str:
    """Tool-discipline rules prepended to every role prompt.

    Phase 5's scale run is the argument for it: on a free model the Debugger needed six
    attempts across two escalations, and four of the nine defects that phase found were
    malformed tool calls or a forgotten `submit_result`. Those are protocol failures, not
    reasoning ones, and the plan is explicit that the fix belongs in a preamble rather
    than in the role prompts — "do not tune Anthropic prompts to fix open-model
    behaviour".
    """
    from agents.base import load_prompt

    return load_prompt("_open_model_preamble")


def build(settings: Settings, name: str | None = None) -> LLMProvider:
    """A provider by name, or the deployment's default when none is given.

    `name` is the run's, read from its row. It wins over `LLM_PROVIDER` on purpose: the
    setting says what a run gets when it does not choose, and a run that chose must not
    quietly get something else — `llm_calls.provider` is supposed to be evidence.
    """
    chosen = name or settings.llm_provider
    if (why := unavailable(chosen)) is not None:
        raise ConfigError(why)

    from gateway.openai_compat_provider import OpenAICompatProvider

    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    return OpenAICompatProvider(
        model=settings.llm_model,
        api_key=key,
        base_url=settings.llm_base_url,
        timeout_s=settings.llm_timeout_s,
        preamble=open_model_preamble() if settings.open_model_preamble else "",
        models={
            "opus": settings.llm_model_opus or "",
            "sonnet": settings.llm_model_sonnet or "",
            "haiku": settings.llm_model_haiku or "",
        },
    )
