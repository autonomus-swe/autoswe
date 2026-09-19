"""Everything the nodes need, built once per run."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from contracts import RepoFacts
from core.settings import Settings
from gateway.provider import LLMProvider
from observability.logging import get_logger
from repo.github import GitHubClient, pygithub_client
from sandbox import select
from sandbox.base import Sandbox
from sandbox.docker import DockerSandbox, image_present
from storage.db import make_engine
from storage.redis import RedisBus

log = get_logger(__name__)


class SandboxFactory(Protocol):
    def __call__(
        self,
        run_id: UUID,
        workspace: Path,
        facts: RepoFacts | None = None,
        image: str | None = None,
    ) -> Sandbox: ...


def docker_sandbox_factory(settings: Settings) -> SandboxFactory:
    """Build a container sized and stocked for the repository it will hold.

    `facts` chooses the image and the limits; `image` overrides the choice, which is what a
    resumed run passes so that a change to the mapping between deploys cannot swap the
    toolchain under a half-finished run.

    `SANDBOX_IMAGE` is the fallback, used for a stack nothing recognises *and* for one whose
    image this host has not built. It no longer pins every run: a repository with a
    `package.json` gets the Node image if that image exists here, whatever `SANDBOX_IMAGE`
    says. A deployment that wants one image for everything should build only that one —
    which is then what every run falls back to.
    """

    def build(
        run_id: UUID,
        workspace: Path,
        facts: RepoFacts | None = None,
        image: str | None = None,
    ) -> Sandbox:
        chosen = image or select.image_for(facts, default=settings.sandbox_image)
        # An image this host has not built is worse than the wrong image: `containers.run`
        # on a missing tag tries to PULL it, and these tags exist in no registry, so the
        # run dies in SETUP with "pull access denied ... may require 'docker login'" — a
        # message about authentication for a problem that is a missing local build.
        #
        # Falling back keeps the previous behaviour for anyone who built only the Python
        # image, which is what the README's `make sandbox-image` tells them to do: the
        # agent still reads, searches and edits, and the install fails non-fatally exactly
        # as it did before this became a choice at all.
        if chosen != settings.sandbox_image and not image_present(chosen):
            log.warning(
                "sandbox_image_not_built",
                wanted=chosen,
                using=settings.sandbox_image,
                note="run `make sandbox-images`; this repository's toolchain is missing",
            )
            chosen = settings.sandbox_image
        limits = select.limits_for(facts, chosen)
        return DockerSandbox(
            run_id,
            workspace,
            image=chosen,
            network=settings.install_network(),
            network_internal=settings.egress_enforced,
            user=f"{settings.sandbox_uid}:{settings.sandbox_gid}",
            runtime=settings.sandbox_runtime,
            no_new_privileges=settings.sandbox_no_new_privileges,
            mem_limit=limits.mem_limit,
            tmpfs_size=limits.tmpfs_size,
            cpus=limits.cpus,
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
        models={
            "opus": settings.llm_model_opus or "",
            "sonnet": settings.llm_model_sonnet or "",
            "haiku": settings.llm_model_haiku or "",
        },
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
