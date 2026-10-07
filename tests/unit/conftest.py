"""Unit tests run as though no `.env` existed, because CI has none.

Three tests in `test_repo_map_ablation.py` built `Settings(...)` without the three fields it
requires — `database_url`, `redis_url`, `API_KEYS` — and passed, because
`SettingsConfigDict(env_file=".env")` quietly supplied them from the developer's own file.
They failed in CI, which has no `.env`, with validation errors naming settings those tests do
not care about.

The failure mattered more than the three tests. **`main`'s CI had been red for that reason,
and a local `pytest -m unit` said nothing**, so the local suite and the one that gates merges
disagreed about whether the project was green — and the local one was the more flattering of
the two. A test suite whose result depends on an untracked file is not a suite, it is a
reading of one machine.

So the fixture below removes `.env` from `Settings`' view for every unit test. Measured
before adding it: exactly those three tests depended on it, so nothing else loses anything.

**A unit test that needs a setting should say which one**, by passing it. That is the whole
point of the marker: `unit` means no I/O, and reading a file off disk to decide what a test
asserts is I/O with an opinion.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.settings import Settings


@pytest.fixture(autouse=True)
def _no_dotenv_for_unit_tests() -> Iterator[None]:
    """Make `Settings` behave as it does in CI: no `.env` to fall back on.

    `model_config` is a plain dict at runtime, so this is a key swap rather than anything
    clever, and it is restored afterwards — the integration tier does want the real file.

    Autouse rather than opt-in on purpose. The bug this prevents is not a wrong decision, it
    is an absent one: nobody chose to read `.env` in a unit test, and the three that did
    looked exactly like the ones that do not.
    """
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original
