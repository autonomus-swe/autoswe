"""Where the CLI gets its API address and key.

Precedence is the kind of thing that is obviously right until somebody has a stale config
file and cannot work out why their key is being ignored. Each rule is pinned here so the
answer is in the repository rather than in whoever remembers writing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli import config
from core.errors import ConfigError

pytestmark = pytest.mark.unit


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_nothing_set_gives_the_local_default_and_no_key() -> None:
    resolved = config.load(path=Path("/nonexistent/config.toml"), environ={})
    assert resolved.api == config.DEFAULT_API and resolved.key == ""


def test_a_flag_beats_the_environment_and_the_file(tmp_path: Path) -> None:
    resolved = config.load(
        api="http://flag",
        key="from-flag",
        path=write(tmp_path, 'api = "http://file"\nkey = "from-file"\n'),
        environ={"AUTOSWE_API": "http://env", "AUTOSWE_API_KEY": "from-env"},
    )
    assert resolved.api == "http://flag" and resolved.key == "from-flag"


def test_the_environment_beats_the_file(tmp_path: Path) -> None:
    resolved = config.load(
        path=write(tmp_path, 'api = "http://file"\nkey = "from-file"\n'),
        environ={"AUTOSWE_API": "http://env", "AUTOSWE_API_KEY": "from-env"},
    )
    assert resolved.api == "http://env" and resolved.key == "from-env"


def test_the_file_is_used_when_nothing_else_is(tmp_path: Path) -> None:
    resolved = config.load(
        path=write(tmp_path, 'api = "http://file"\nkey = "from-file"\n'), environ={}
    )
    assert resolved.api == "http://file" and resolved.key == "from-file"


def test_a_flag_equal_to_the_default_still_wins(tmp_path: Path) -> None:
    """`--api http://127.0.0.1:8000` is how you get back to local when a config file
    points at staging. A resolver that treated "equals the default" as "not given" would
    ignore it and send the run to staging anyway."""
    resolved = config.load(
        api=config.DEFAULT_API,
        path=write(tmp_path, 'api = "http://staging"\n'),
        environ={"AUTOSWE_API": "http://env"},
    )
    assert resolved.api == config.DEFAULT_API


def test_the_sources_are_resolved_per_setting(tmp_path: Path) -> None:
    """A key in the environment and an address in the file is the ordinary case: one is a
    secret you export, the other is a machine you wrote down once."""
    resolved = config.load(
        path=write(tmp_path, 'api = "http://file"\n'), environ={"AUTOSWE_API_KEY": "from-env"}
    )
    assert resolved.api == "http://file" and resolved.key == "from-env"


def test_a_trailing_slash_is_removed(tmp_path: Path) -> None:
    """`http://host/` plus `/runs` is `http://host//runs`, which some proxies route
    somewhere else entirely."""
    assert config.load(api="http://host/", environ={}).api == "http://host"


def test_an_unreadable_file_is_an_error_rather_than_a_silent_default(tmp_path: Path) -> None:
    """Falling through would connect to localhost with no key and report "unauthorized",
    which sends the reader to look at their key rather than at the file they just edited."""
    with pytest.raises(ConfigError, match="could not be read"):
        config.load(path=write(tmp_path, "api = this is not toml\n"), environ={})


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """Most people never write one."""
    assert config.load(path=tmp_path / "absent.toml", environ={}).api == config.DEFAULT_API


def test_non_string_values_in_the_file_are_coerced_not_crashed_on(tmp_path: Path) -> None:
    """A port written as a bare number is a plausible mistake, and refusing to start over
    it would be a worse answer than reading it."""
    resolved = config.load(path=write(tmp_path, "api = 8000\n"), environ={})
    assert resolved.api == "8000"
