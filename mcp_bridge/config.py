"""`mcp_servers.yaml`: which external MCP servers to mount, and on what terms.

Every field here is a policy decision the harness enforces, not a hint to the model. A
server declares which tools may be called at all (`allow`), which roles may call them
(`roles`), and which of them change something (`mutating`). The last one drives approval,
so getting it wrong is how a run comments on somebody's pull request without asking.

## Validated before anything connects

Mounting is a network operation against a subprocess that inherits secrets. A
configuration that could never be safe should fail while it is still a file on disk, so
every check that can run without a connection runs here: a mutating tool routed to a
read-only role, a `mutating` entry missing from `allow`, a role that does not exist.

`tools/registry.py` asserts the read-only rule again at registration. That is deliberate
duplication — this one gives a good message early, that one cannot be bypassed by any
route that reaches the registry.

## Secrets are named, never inherited

`env` is everything the child gets beyond the SDK's own safe-list (`HOME`, `LOGNAME`,
`PATH`, `SHELL`, `TERM`, `USER`). An MCP stdio server is a subprocess, and one started with
`os.environ` would get `ANTHROPIC_API_KEY`, `DATABASE_URL` and the GitHub token whether it
had any business with them or not. Values written `${VAR}` are resolved from the worker's
environment at mount time; a missing one is an error rather than an empty string, because a
server that silently starts unauthenticated fails later and further away.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.envsubst import expand
from core.errors import ConfigError

DEFAULT_PATH = Path("mcp_servers.yaml")
TIMEOUT_S = 60
# `${VAR}` (see `core/envsubst.py`) is expanded in `env` values only. Not in `command`: an
# argument is not a place to put a secret — it is visible in `ps` to every user on the box
# — and the one place the plan wanted it (`${TARGET_DATABASE_URL}` as a Postgres server's
# argv) is exactly that mistake. Such a server reads its DSN from the environment instead.
NAME = re.compile(r"^[a-z][a-z0-9_]*$")
TRANSPORTS = ("stdio",)


@dataclass(frozen=True)
class ServerConfig:
    """One external MCP server and the terms it is mounted on."""

    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    roles: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    mutating: list[str] = field(default_factory=list)
    read_only: bool = False
    timeout_s: int = TIMEOUT_S

    def tool_name(self, remote: str) -> str:
        """The local name for one of this server's tools.

        Prefixed because a server exposing `read_file` would otherwise shadow ours, and a
        model that called it would be reading the wrong machine's disk with no sign that
        anything had changed.
        """
        return f"mcp_{self.name}_{remote}"


def load(
    path: Path | str = DEFAULT_PATH, *, environ: dict[str, str] | None = None
) -> list[ServerConfig]:
    """Parse and validate the file. Missing file means no servers, which is the default.

    Absence is not an error: mounting external servers is opt-in, and a deployment that
    wants none should not have to write a file saying so.
    """
    p = Path(path)
    if not p.is_file():
        return []
    import yaml

    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{p} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{p} must be a mapping with a `servers:` key")
    servers = raw.get("servers") or {}
    if not isinstance(servers, dict):
        raise ConfigError(f"{p}: `servers` must be a mapping of name to configuration")
    return [
        _one(name, body, p, environ if environ is not None else dict(os.environ))
        for name, body in servers.items()
    ]


def _one(name: Any, body: Any, path: Path, environ: dict[str, str]) -> ServerConfig:
    where = f"{path}: server {name!r}"
    if not isinstance(name, str) or not NAME.match(name):
        raise ConfigError(f"{where}: name must be lower-case letters, digits and underscores")
    if not isinstance(body, dict):
        raise ConfigError(f"{where}: configuration must be a mapping")

    transport = body.get("transport", "stdio")
    if transport not in TRANSPORTS:
        raise ConfigError(
            f"{where}: transport {transport!r} is not supported; this build mounts "
            f"{' or '.join(TRANSPORTS)} servers only"
        )

    command = body.get("command")
    if not isinstance(command, list) or not command:
        raise ConfigError(f"{where}: `command` must be a non-empty list of strings")
    if not all(isinstance(c, str) for c in command):
        raise ConfigError(f"{where}: `command` must be a non-empty list of strings")

    allow = _str_list(body.get("allow"), f"{where}: `allow`")
    if not allow:
        # A server with nothing allowed mounts nothing, which is almost certainly a typo
        # rather than an intention — and the intention has a spelling: delete the entry.
        raise ConfigError(f"{where}: `allow` is empty, so this server would mount no tools")
    mutating = _str_list(body.get("mutating"), f"{where}: `mutating`")
    roles = _str_list(body.get("roles"), f"{where}: `roles`")
    read_only = bool(body.get("read_only", False))

    if unknown := [m for m in mutating if m not in allow]:
        raise ConfigError(
            f"{where}: {unknown} are listed as mutating but not allowed. A mutating name "
            "that is not in `allow` guards nothing — it is a typo that silently disarms "
            "the approval gate for the tool it was meant to name."
        )
    if read_only and mutating:
        raise ConfigError(f"{where}: read_only is set but {mutating} are listed as mutating")

    _check_roles(roles, mutating, where)
    return ServerConfig(
        name=name,
        command=[str(c) for c in command],
        env=_env(body.get("env"), where, environ),
        roles=roles,
        allow=allow,
        mutating=mutating,
        read_only=read_only,
        timeout_s=int(body.get("timeout_s", TIMEOUT_S)),
    )


def _check_roles(roles: list[str], mutating: list[str], where: str) -> None:
    """Roles exist, and a mutating tool is never routed to a read-only one."""
    from tools.registry import READ_ONLY_ROLES, ROLE_TOOLS

    if not roles:
        raise ConfigError(f"{where}: `roles` is empty, so no agent could call these tools")
    if unknown := [r for r in roles if r not in ROLE_TOOLS]:
        raise ConfigError(f"{where}: unknown roles {unknown}; known roles are {sorted(ROLE_TOOLS)}")
    if mutating and (bad := [r for r in roles if r in READ_ONLY_ROLES]):
        raise ConfigError(
            f"{where}: {bad} are read-only roles and this server exposes mutating tools "
            f"{mutating}. Split the server into two entries, or drop those roles."
        )


def _env(raw: Any, where: str, environ: dict[str, str]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: `env` must be a mapping")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError(f"{where}: `env` keys and values must be strings")
        # A server started with an empty token comes up, answers `list_tools`, and fails
        # on its first real call — somewhere that names neither this file nor the
        # variable. `expand` refuses instead.
        out[key] = expand(value, environ, where=f"{where}: `env.{key}`")
    return out


def _str_list(raw: Any, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"{where} must be a list of strings")
    return list(raw)
