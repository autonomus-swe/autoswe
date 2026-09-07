"""Process settings, validated at startup.

Rules:
- Secrets are ``SecretStr`` so they never appear in ``repr()`` or logs.
- The API process never needs ``ANTHROPIC_API_KEY`` / ``GITHUB_TOKEN``; only the worker
  calls :meth:`Settings.require_worker`.
- Any missing or invalid required variable exits the process with code 2 and prints the
  variable name. Never silently proceed.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EXIT_CONFIG = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- Phase 0: required everywhere ----
    database_url: str = Field(pattern=r"^postgresql\+asyncpg://")
    redis_url: str = Field(pattern=r"^rediss?://")
    api_keys_raw: str = Field(validation_alias="API_KEYS", min_length=8)

    # ---- Phase 1+: worker only ----
    # LLM provider. ``openai_compat`` talks to any OpenAI-compatible endpoint (OpenRouter by
    # default; also vLLM, Ollama, Groq). ``anthropic`` uses the Anthropic SDK (Phase 6).
    llm_provider: Literal["openai_compat", "anthropic"] = "openai_compat"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "minimax/minimax-m3:free"
    llm_max_tokens: int = Field(default=16_000, gt=0)
    anthropic_api_key: SecretStr | None = None
    github_token: SecretStr | None = None
    worktrees_dir: Path = Path("/var/agent/worktrees")
    repos_dir: Path = Path("/var/agent/repos")
    sandbox_image: str = "agent-sandbox:python-3.12"
    sandbox_network: str = "agent-install"
    # no_new_privs blocks setuid escalation. Some Docker builds (snap-packaged Docker on
    # Ubuntu Core) cannot start a container with it because their AppArmor profile
    # transition needs it off; the sandbox detects that and falls back with a warning.
    sandbox_no_new_privileges: bool = True
    sandbox_runtime: str | None = None  # "runsc" for gVisor in Phase 5
    # The container runs as this uid:gid so files it writes match the worker's files.
    sandbox_uid: int = Field(default_factory=os.getuid)
    sandbox_gid: int = Field(default_factory=os.getgid)
    keep_failed_sandbox: bool = False

    # ---- misc ----
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    @field_validator("anthropic_api_key", "github_token", "llm_api_key", mode="before")
    @classmethod
    def _blank_secret_is_absent(cls, v: object) -> object:
        """``LLM_API_KEY=`` in a .env file means "not set", not "the empty key"."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def api_keys(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys_raw.split(",") if k.strip())

    def require_worker(self) -> None:
        """Worker entrypoint only. Exits naming any missing worker secret."""
        key_field = "anthropic_api_key" if self.llm_provider == "anthropic" else "llm_api_key"
        missing = [name for name in (key_field, "github_token") if getattr(self, name) is None]
        if missing:
            _die(missing)

    def public_dict(self) -> dict[str, str]:
        """Non-secret view for ``autoswe config`` and debug logs."""
        return {
            "database_url": _mask_dsn(self.database_url),
            "redis_url": _mask_dsn(self.redis_url),
            "api_keys": f"{len(self.api_keys)} key(s)",
            "llm_provider": self.llm_provider,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_api_key": "set" if self.llm_api_key else "unset",
            "anthropic_api_key": "set" if self.anthropic_api_key else "unset",
            "github_token": "set" if self.github_token else "unset",
            "worktrees_dir": str(self.worktrees_dir),
            "repos_dir": str(self.repos_dir),
            "sandbox_image": self.sandbox_image,
            "sandbox_user": f"{self.sandbox_uid}:{self.sandbox_gid}",
            "sandbox_no_new_privileges": str(self.sandbox_no_new_privileges),
            "environment": self.environment,
            "log_level": self.log_level,
        }


def _mask_dsn(dsn: str) -> str:
    """postgresql+asyncpg://user:pass@host/db -> postgresql+asyncpg://user:***@host/db"""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, hostpart = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{hostpart}" if ":" in creds else dsn


def _die(missing: list[str]) -> None:
    names = ", ".join(m.upper() for m in missing)
    print(f"FATAL: missing or invalid settings: {names}", file=sys.stderr)
    raise SystemExit(EXIT_CONFIG)


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Build settings from the environment (and ``env_file`` if given). Exits on failure."""
    try:
        return Settings(_env_file=env_file)
    except ValidationError as e:
        _die([".".join(str(p) for p in err["loc"]) for err in e.errors()])
        raise AssertionError("unreachable") from None


@lru_cache
def get_settings() -> Settings:
    return load_settings(".env")
