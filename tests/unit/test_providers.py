"""Which provider a run gets, and the preamble every role prompt starts with.

`runs.provider` has been a column since Phase 1 and nothing read it: the worker built its
provider from `LLM_PROVIDER` whatever the row said. A recorded value that did not decide
anything is not evidence, and `llm_calls.provider` is supposed to be exactly that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from core.errors import ConfigError
from core.settings import Settings, load_settings
from gateway import providers
from gateway.openai_compat_provider import OpenAICompatProvider
from gateway.provider import Request

pytestmark = pytest.mark.unit


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Settings]:
    """A settings builder whose overrides are undone with the fixture.

    Through `monkeypatch` rather than `os.environ` directly: `Settings` reads the
    environment, and a bare assignment here would leak `LLM_PROVIDER` into whatever test
    ran next — which is the sort of failure that looks like a real one somewhere else.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("API_KEYS", "test-key-123456")  # the value the rest of the suite uses

    def build(**overrides: str) -> Settings:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
        return load_settings(env_file=None)

    return build


# ---- which providers exist ---------------------------------------------------------------


def test_the_available_provider_builds(settings: Callable[..., Settings]) -> None:
    built = providers.build(settings(), "openai_compat")
    assert built.provider_name == "openai_compat"


def test_a_known_but_unavailable_provider_says_which_ones_work(
    settings: Callable[..., Settings],
) -> None:
    """The plan is written for a build whose default is the Anthropic SDK. This one has no
    Anthropic provider, so the name is recognised and refused — with the list, because
    "not available" on its own leaves the caller guessing."""
    why = providers.unavailable("anthropic")
    assert why is not None and "openai_compat" in why
    with pytest.raises(ConfigError, match="openai_compat"):
        providers.build(settings(), "anthropic")


def test_an_unknown_provider_is_refused_too() -> None:
    why = providers.unavailable("gpt5")
    assert why is not None and "unknown provider" in why


def test_available_is_a_subset_of_known() -> None:
    """A row can outlive a deployment. `KNOWN` is every name that has ever been written
    into the column, so a provider dropped from a build is still a name this module can
    recognise and explain rather than one it calls a typo."""
    assert set(providers.AVAILABLE) <= set(providers.KNOWN)


def test_no_name_takes_the_deployments_default(settings: Callable[..., Settings]) -> None:
    assert providers.build(settings(), None).provider_name == "openai_compat"


def test_the_runs_choice_beats_the_process_setting(settings: Callable[..., Settings]) -> None:
    """The direction matters. The setting says what a run gets when it does not choose;
    a run that chose must not quietly get something else, or the ledger is fiction."""
    assert providers.unavailable("openai_compat") is None
    built = providers.build(settings(LLM_PROVIDER="anthropic"), "openai_compat")
    assert built.provider_name == "openai_compat"


# ---- the open-model preamble --------------------------------------------------------------


def make(preamble: str = "") -> OpenAICompatProvider:
    return OpenAICompatProvider(
        model="m", api_key=None, base_url="https://example.invalid/v1", preamble=preamble
    )


def system_text(provider: OpenAICompatProvider, req: Request) -> str:
    content = provider._system(req)["content"]
    if isinstance(content, str):
        return content
    return "\n".join(str(block["text"]) for block in content)


def test_the_preamble_comes_before_the_role_prompt() -> None:
    """Before, not after: it is about how to use tools at all, and instructions that
    arrive after the role's are read as exceptions to them."""
    text = system_text(make("RULES HERE"), Request(role="coder", system="ROLE PROMPT"))
    assert text.index("RULES HERE") < text.index("ROLE PROMPT")


def test_without_a_preamble_the_system_message_is_what_it_always_was() -> None:
    assert system_text(make(), Request(role="coder", system="ROLE PROMPT")) == "ROLE PROMPT"


def test_the_preamble_shares_the_role_prompts_block() -> None:
    """Not a block of its own. The system field allows four cache breakpoints and the run
    block wants one; a third stable block would spend a breakpoint on text that never
    changes independently of the prompt it is glued to."""
    content = make("RULES")._system(Request(role="coder", system="ROLE", run_block="FACTS"))[
        "content"
    ]
    assert isinstance(content, list) and len(content) == 2
    assert "RULES" in content[0]["text"] and "ROLE" in content[0]["text"]
    assert content[1]["text"] == "FACTS"


def test_the_shipped_preamble_is_about_tool_protocol() -> None:
    """It exists because of what Phase 5 measured — malformed calls and forgotten
    `submit_result` on free models — not because open models need encouragement. A
    preamble that drifted into role advice would be tuning eleven prompts from one file."""
    text = providers.open_model_preamble()
    assert "schema" in text and "submit" in text
    # Short on purpose: it rides in the cached prefix of every call of every step.
    assert len(text) < 2000


def test_the_setting_decides_whether_the_preamble_is_sent(
    settings: Callable[..., Settings],
) -> None:
    with_it = providers.build(settings(), "openai_compat")
    without = providers.build(settings(OPEN_MODEL_PREAMBLE="false"), "openai_compat")
    req = Request(role="coder", system="ROLE")
    assert "schema" in system_text(cast(OpenAICompatProvider, with_it), req)
    assert system_text(cast(OpenAICompatProvider, without), req) == "ROLE"
