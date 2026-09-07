"""Thin async wrapper over the git binary. Credentials are passed per process, never stored."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from core.errors import RepoError

BOT_NAME = "autoswe[bot]"
BOT_EMAIL = "autoswe@users.noreply.github.com"


def git_auth_env(token: str) -> dict[str, str]:
    """Env that lets one git process authenticate to github.com without touching disk."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def identity_env() -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": BOT_NAME,
        "GIT_AUTHOR_EMAIL": BOT_EMAIL,
        "GIT_COMMITTER_NAME": BOT_NAME,
        "GIT_COMMITTER_EMAIL": BOT_EMAIL,
    }


async def git(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None, timeout_s: int = 300
) -> str:
    """Run ``git *args`` and return stdout. Raises :class:`RepoError` on non-zero exit."""
    import os

    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **identity_env(), **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError as e:
        proc.kill()
        raise RepoError(f"git {' '.join(args[:2])} timed out after {timeout_s}s") from e
    if proc.returncode != 0:
        detail = err.decode(errors="replace").strip() or out.decode(errors="replace").strip()
        raise RepoError(f"git {' '.join(args[:2])} failed ({proc.returncode}): {detail[:500]}")
    return out.decode(errors="replace")
