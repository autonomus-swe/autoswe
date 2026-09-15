"""Everything the nodes need, built once per run."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from core.settings import Settings
from gateway.provider import LLMProvider
from repo.github import GitHubClient, pygithub_client
from sandbox.base import Sandbox
from sandbox.docker import DockerSandbox
from storage.db import make_engine
from storage.redis import RedisBus


class SandboxFactory(Protocol):
    def __call__(self, run_id: UUID, workspace: Path) -> Sandbox: ...


def docker_sandbox_factory(settings: Settings) -> SandboxFactory:
    def build(run_id: UUID, workspace: Path) -> Sandbox:
        return DockerSandbox(
            run_id,
            workspace,
            image=settings.sandbox_image,
            network=settings.sandbox_network,
            user=f"{settings.sandbox_uid}:{settings.sandbox_gid}",
            runtime=settings.sandbox_runtime,
            no_new_privileges=settings.sandbox_no_new_privileges,
        )

    return build


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":  # Phase 6 ships the Anthropic provider
        raise NotImplementedError(
            "the anthropic provider arrives in Phase 6; set LLM_PROVIDER=openai_compat"
        )
    from gateway.openai_compat_provider import OpenAICompatProvider

    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    return OpenAICompatProvider(
        model=settings.llm_model,
        api_key=key,
        base_url=settings.llm_base_url,
        timeout_s=settings.llm_timeout_s,
    )


@dataclass
class Deps:
    settings: Settings
    provider: LLMProvider
    engine: Any
    bus: RedisBus
    sandbox_factory: SandboxFactory
    github: GitHubClient | None = None

    @classmethod
    def build(cls, settings: Settings) -> Deps:
        token = settings.github_token.get_secret_value() if settings.github_token else None
        return cls(
            settings=settings,
            provider=build_provider(settings),
            engine=make_engine(settings.database_url, pool_size=5, max_overflow=2),
            bus=RedisBus(settings.redis_url),
            sandbox_factory=docker_sandbox_factory(settings),
            github=pygithub_client(token) if token else None,
        )

    async def aclose(self) -> None:
        await self.bus.close()
        await self.engine.dispose()

    def git_token(self) -> str | None:
        """The GitHub token when one is configured. Local or already-authenticated
        remotes push fine without it; ``open_pr`` is what truly needs it."""
        if self.settings.github_token is None:
            return None
        return self.settings.github_token.get_secret_value()

    def worktrees_dir(self) -> Path:
        return Path(os.path.expanduser(str(self.settings.worktrees_dir)))

    def repos_dir(self) -> Path:
        return Path(os.path.expanduser(str(self.settings.repos_dir)))
