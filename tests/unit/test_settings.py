from __future__ import annotations

import pytest

from core.settings import EXIT_CONFIG, Settings, get_settings, load_settings

pytestmark = pytest.mark.unit

ALL_VARS = (
    "DATABASE_URL",
    "REDIS_URL",
    "API_KEYS",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "ENVIRONMENT",
    "LOG_LEVEL",
    "WORKTREES_DIR",
    "REPOS_DIR",
    "SANDBOX_IMAGE",
)
BASE = {
    "DATABASE_URL": "postgresql+asyncpg://user:pw@localhost:5432/autoswe",
    "REDIS_URL": "redis://localhost:6379/0",
    "API_KEYS": "alpha-key-1, beta-key-2,,",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()


def set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    env = {**BASE, **overrides}
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_loads_minimal_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    s = load_settings(env_file=None)
    assert s.api_keys == frozenset({"alpha-key-1", "beta-key-2"})
    assert s.anthropic_api_key is None
    assert s.environment == "dev"


def test_missing_database_url_exits_naming_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_env(monkeypatch, DATABASE_URL=None)
    with pytest.raises(SystemExit) as exc:
        load_settings(env_file=None)
    assert exc.value.code == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "FATAL" in err and "DATABASE_URL" in err


def test_wrong_database_driver_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_env(monkeypatch, DATABASE_URL="postgresql://user:pw@localhost/db")
    with pytest.raises(SystemExit):
        load_settings(env_file=None)
    assert "DATABASE_URL" in capsys.readouterr().err


def test_short_api_keys_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_env(monkeypatch, API_KEYS="short")
    with pytest.raises(SystemExit):
        load_settings(env_file=None)
    assert "API_KEYS" in capsys.readouterr().err


def test_require_worker_names_missing_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_env(monkeypatch, LLM_API_KEY="sk-or-unit-test-key-000000")
    s = load_settings(env_file=None)
    with pytest.raises(SystemExit) as exc:
        s.require_worker()
    assert exc.value.code == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err and "LLM_API_KEY" not in err and "ANTHROPIC" not in err


def test_require_worker_key_depends_on_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_env(monkeypatch, LLM_PROVIDER="anthropic", GITHUB_TOKEN="ghp_unittest000000000000")
    with pytest.raises(SystemExit):
        load_settings(env_file=None).require_worker()
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_require_worker_passes_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(
        monkeypatch,
        LLM_API_KEY="sk-or-unit-test-key-000000",
        GITHUB_TOKEN="ghp_unittest000000000000",
    )
    s = load_settings(env_file=None)
    s.require_worker()
    public = s.public_dict()
    assert public["llm_api_key"] == "set" and public["llm_provider"] == "openai_compat"
    assert public["sandbox_user"].count(":") == 1


def test_secrets_never_appear_in_repr_or_public_view(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(
        monkeypatch,
        ANTHROPIC_API_KEY="sk-ant-unit-test-key-000000",
        GITHUB_TOKEN="ghp_unittest000000000000",
    )
    s = load_settings(env_file=None)
    text = repr(s) + str(s) + str(s.public_dict())
    assert "sk-ant-unit" not in text and "ghp_unittest" not in text
    assert s.public_dict()["database_url"] == "postgresql+asyncpg://user:***@localhost:5432/autoswe"
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-unit-test-key-000000"


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    assert get_settings() is get_settings()


def test_blank_optional_secrets_are_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A .env written by `cp .env.example .env` has empty values; they must not read as set.
    set_env(monkeypatch, LLM_API_KEY="", GITHUB_TOKEN="   ")
    s = load_settings(env_file=None)
    assert s.llm_api_key is None and s.github_token is None
    assert s.public_dict()["llm_api_key"] == "unset"
    with pytest.raises(SystemExit):
        s.require_worker()
    err = capsys.readouterr().err
    assert "LLM_API_KEY" in err and "GITHUB_TOKEN" in err


# ---- egress ----------------------------------------------------------------------------


def test_egress_is_off_until_a_proxy_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default has to stay byte-identical to what every existing machine does. A
    half-configured egress setup degrades runs silently, because an install failure is
    only a warning."""
    set_env(monkeypatch)
    monkeypatch.delenv("EGRESS_PROXY_URL", raising=False)
    s = load_settings(env_file=None)

    assert not s.egress_enforced
    assert s.install_network() == s.sandbox_network
    assert s.proxy_env() == {}


def test_naming_a_proxy_moves_the_sandbox_to_the_internal_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second network rather than flipping `agent-install`: reusing an existing network
    without inspecting it would leave every machine that has run autoswe with unrestricted
    egress while the deny tests still passed."""
    set_env(monkeypatch)
    monkeypatch.setenv("EGRESS_PROXY_URL", "http://agent-egress-proxy:8888")
    s = load_settings(env_file=None)

    assert s.egress_enforced
    assert s.install_network() == s.sandbox_egress_network
    assert s.install_network() != s.sandbox_network


def test_the_proxy_variables_cover_both_cases_and_spare_the_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uv, pip, npm, pnpm and go between them read lower and upper case, and which a given
    version prefers is not worth tracking."""
    set_env(monkeypatch)
    monkeypatch.setenv("EGRESS_PROXY_URL", "http://p:8888")
    env = load_settings(env_file=None).proxy_env()

    assert env["HTTP_PROXY"] == env["http_proxy"] == "http://p:8888"
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://p:8888"
    assert "127.0.0.1" in env["NO_PROXY"] and "127.0.0.1" in env["no_proxy"]


def test_a_unit_test_cannot_see_the_dotenv_file() -> None:
    """The guard in `tests/unit/conftest.py` is in force, asserted rather than assumed.

    `Settings` declares `env_file=".env"`, so on a developer's machine `Settings()` with no
    arguments succeeds off a file CI does not have. That is how three tests in
    `test_repo_map_ablation.py` passed locally while **`main`'s CI was red** — the local
    suite and the one that gates merges disagreed, and the local one was the flattering one.

    The autouse fixture removes `.env` from `Settings`' view for the unit tier. Removing the
    fixture changes nothing for the tests that are now hermetic, so no ordinary test can
    catch its loss — this one can, because it asserts the condition the fixture creates.

    It only bites on a machine that *has* a `.env`, which is exactly the machine where the
    guard matters: CI fails without it either way, and a developer would not have noticed.
    """
    from pydantic import ValidationError

    assert Settings.model_config["env_file"] is None, (
        "the unit tier's no-dotenv fixture is not in force; a unit test can now pass off a "
        "file CI does not have"
    )

    with pytest.raises(ValidationError) as caught:
        Settings()

    missing = {e["loc"][0] for e in caught.value.errors()}
    assert {"database_url", "redis_url", "API_KEYS"} <= missing, (
        "a unit test that needs a setting must pass it explicitly; the required ones are "
        f"{sorted(missing)}"
    )
