from __future__ import annotations

import pytest

from core.settings import EXIT_CONFIG, get_settings, load_settings

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
    set_env(monkeypatch, ANTHROPIC_API_KEY="sk-ant-unit-test-key-000000")
    s = load_settings(env_file=None)
    with pytest.raises(SystemExit) as exc:
        s.require_worker()
    assert exc.value.code == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err and "ANTHROPIC_API_KEY" not in err


def test_require_worker_passes_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(
        monkeypatch,
        ANTHROPIC_API_KEY="sk-ant-unit-test-key-000000",
        GITHUB_TOKEN="ghp_unittest000000000000",
    )
    load_settings(env_file=None).require_worker()


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
