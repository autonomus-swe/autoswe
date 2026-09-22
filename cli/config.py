"""Where the CLI gets its API address and key: flags, then environment, then a file.

Three sources in that order, which is the order of how specific the intent is. A flag was
typed for this invocation. An environment variable was exported for this shell. A file was
written once and forgotten — which is exactly why it must lose to both, and why a stale
one should never silently redirect a run at the wrong control plane.

## An unreadable file is not a missing file

A `config.toml` that cannot be parsed raises rather than falling through to the defaults.
Falling through would connect to `localhost` with no key and report "unauthorized", which
sends the reader to look at their key rather than at the file they just edited.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.errors import ConfigError

DEFAULT_API = "http://127.0.0.1:8000"
CONFIG_PATH = Path("~/.config/autoswe/config.toml")


@dataclass(frozen=True)
class Config:
    api: str = DEFAULT_API
    key: str = ""


def load(
    *,
    api: str | None = None,
    key: str | None = None,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Resolve the two settings the CLI needs.

    `api=None` means "no flag was given". A flag that happens to equal the default is
    still a flag and still wins — which matters when a config file points somewhere else
    and somebody passes `--api http://127.0.0.1:8000` to get back to local.
    """
    env = environ if environ is not None else os.environ
    file = _file(path if path is not None else CONFIG_PATH)
    return Config(
        api=(api or env.get("AUTOSWE_API") or file.get("api") or DEFAULT_API).rstrip("/"),
        key=key or env.get("AUTOSWE_API_KEY") or file.get("key") or "",
    )


def _file(path: Path) -> dict[str, str]:
    import tomllib

    expanded = path.expanduser()
    if not expanded.is_file():
        return {}
    try:
        raw = tomllib.loads(expanded.read_text())
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise ConfigError(f"{expanded} could not be read: {e}") from e
    return {k: str(v) for k, v in raw.items() if isinstance(v, str | int | float)}
