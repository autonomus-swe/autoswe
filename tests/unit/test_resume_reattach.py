"""Taking back the lock, worktree and container a crashed run was using.

`reattach` is the whole value of checkpointing: a worker dies mid-run and another picks the
run up where it stopped instead of starting over. It runs only on the resume path, which no
unit test drove, and four mutations survived the whole suite:

| mutation | consequence |
|---|---|
| `base_sha or base_branch` → `base_branch` | **the run resumes against different code** |
| drop the `has_network()` guard | the sandbox keeps network for the rest of the run |
| lock conflict no longer refuses | two workers drive one run and one worktree |
| drop the CPU raise/restore | the install crawls, or keeps its allowance forever |

The first is the one that matters and it is silent. A run pinned to a commit — every
SWE-bench instance, and every task whose YAML declares `base_commit` — resumes from the
*head of the branch* instead. The worktree is created, the install succeeds, the tests run,
and the patch is produced against a tree the instance never specified. Nothing errors, and
the number that comes out is attributed to the agent.

Fakes rather than Docker: every assertion here is about which argument `reattach` passes and
which branch it takes, and a test that needed a container to check an argument is a test
nobody runs on every commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from core.errors import AutosweError, SandboxError
from orchestrator import resume as resume_mod

pytestmark = pytest.mark.unit

RUN_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


@dataclass
class FakeSandbox:
    """Records the order of what it was told to do, which is what most of this is about."""

    image: str = "agent-sandbox:python-3.12"
    events: list[Any] = field(default_factory=list)
    network_after_disconnect: bool = False

    async def stop(self, *, remove: bool = True) -> None:
        self.events.append("stop")

    async def start(self) -> None:
        self.events.append("start")

    async def connect_install_network(self) -> None:
        self.events.append("connect")

    async def set_cpus(self, cpus: float) -> None:
        self.events.append(("set_cpus", cpus))

    async def exec(self, cmd: str, *, timeout_s: int = 120, env: Any = None) -> Any:
        self.events.append("exec")
        return type("R", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

    async def disconnect_network(self) -> None:
        self.events.append("disconnect")

    async def has_network(self) -> bool:
        return self.network_after_disconnect


@dataclass
class FakeBus:
    acquired: bool = True
    holder: str = "another-worker"
    calls: list[Any] = field(default_factory=list)

    async def acquire_lock(self, key: str, owner: str, ttl: int) -> bool:
        self.calls.append(("acquire", key, owner))
        return self.acquired

    async def lock_owner(self, key: str) -> str:
        return self.holder


@dataclass
class FakeLimits:
    raises_cpus_to_install: bool = True
    install_cpus: float = 4.0
    cpus: float = 1.0


@dataclass
class FakeSettings:
    github_token: Any = None

    def proxy_env(self) -> dict[str, str]:
        return {}


@dataclass
class FakeDeps:
    bus: FakeBus
    settings: FakeSettings
    sandbox: FakeSandbox
    worktrees: Path
    repos: Path

    def repos_dir(self) -> Path:
        return self.repos

    def worktrees_dir(self) -> Path:
        return self.worktrees

    def sandbox_factory(self, run_id: Any, path: Path, facts: Any, image: Any) -> FakeSandbox:
        return self.sandbox


@dataclass
class FakeState:
    run_id: uuid.UUID = RUN_ID
    repo_url: str = "https://github.com/acme/demo"
    base_branch: str = "main"
    base_sha: str | None = "a" * 40
    work_branch: str = f"agent/{RUN_ID}"
    facts: Any = None
    sandbox_image: str | None = "agent-sandbox:python-3.12"
    phase: Any = field(default_factory=lambda: type("P", (), {"value": "code"})())


@dataclass
class FakeResources:
    lock_key: str | None = None
    lock_owner: str | None = None
    worktree: Any = None
    sandbox: Any = None


def wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    bus: FakeBus | None = None,
    sandbox: FakeSandbox | None = None,
    limits: FakeLimits | None = None,
    worktree_exists: bool = False,
) -> tuple[FakeDeps, FakeState, FakeResources, dict[str, Any]]:
    """Everything `reattach` reaches for, replaced with something that records."""
    seen: dict[str, Any] = {}
    the_sandbox = sandbox or FakeSandbox()

    async def fake_bare(url: str, repos: Path, token: Any) -> Any:
        return object()

    async def fake_create(bare: Any, worktrees: Path, run_id: Any, committish: Any) -> Any:
        seen["committish"] = committish
        return type("WT", (), {"path": tmp_path / "wt"})()

    monkeypatch.setattr(resume_mod, "ensure_bare_clone", fake_bare)
    monkeypatch.setattr("orchestrator.resume.wt.create", fake_create)
    monkeypatch.setattr(
        "orchestrator.resume.select.limits_for", lambda facts, image: limits or FakeLimits()
    )
    monkeypatch.setattr(resume_mod, "install_command", lambda path, facts: "pip install -e .")

    worktrees = tmp_path / "worktrees"
    if worktree_exists:
        (worktrees / str(RUN_ID)).mkdir(parents=True)
    else:
        worktrees.mkdir(parents=True, exist_ok=True)

    deps = FakeDeps(
        bus=bus or FakeBus(),
        settings=FakeSettings(),
        sandbox=the_sandbox,
        worktrees=worktrees,
        repos=tmp_path / "repos",
    )
    return deps, FakeState(), FakeResources(), seen


# ---- the committish ------------------------------------------------------------------------


async def test_a_recreated_worktree_starts_from_the_pinned_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The silent one.

    A run pinned to a commit — every SWE-bench instance — resumed from `base_branch` instead
    continues against whatever the branch head is now. Everything downstream succeeds: the
    worktree is created, the install runs, the tests pass or fail on their merits, and the
    patch is produced against a tree the instance never named.
    """
    deps, state, res, seen = wire(monkeypatch, tmp_path)

    await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    assert seen["committish"] == state.base_sha, (
        "a resumed run must continue from the commit it started on, not from the branch head"
    )


async def test_a_run_with_no_pinned_commit_falls_back_to_its_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """So the assertion above cannot be met by hard-coding `base_sha`. An ordinary run has
    no pin, and the branch is the only thing to resume from."""
    deps, state, res, seen = wire(monkeypatch, tmp_path)
    state.base_sha = None

    await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    assert seen["committish"] == "main"


async def test_an_existing_worktree_is_reused_rather_than_recreated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The point of resuming: the work already done is still on disk. Recreating would
    discard the edits the run is being resumed to continue."""
    deps, state, res, seen = wire(monkeypatch, tmp_path, worktree_exists=True)

    await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    assert "committish" not in seen, "an existing worktree must not be rebuilt from scratch"
    assert res.worktree.path == deps.worktrees_dir() / str(RUN_ID)


# ---- the lock ------------------------------------------------------------------------------


async def test_a_run_already_held_by_another_worker_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two workers on one run share a worktree and a container, and both write to them.

    The failure that produces is not a crash — it is a branch with two agents' commits
    interleaved on it. So this refuses rather than proceeding, and clears `lock_key` so the
    teardown does not release a lock this worker never held.
    """
    bus = FakeBus(acquired=False, holder="worker-2")
    deps, state, res, _ = wire(monkeypatch, tmp_path, bus=bus)

    with pytest.raises(AutosweError, match="held by worker-2"):
        await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    assert res.lock_key is None, "releasing a lock held by somebody else would be worse"


async def test_reacquiring_a_lock_this_worker_already_holds_is_not_a_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordinary resume: the same worker restarting after a crash still owns the lock,
    and must not refuse to resume its own run."""
    bus = FakeBus(acquired=False, holder="worker-1")
    deps, state, res, _ = wire(monkeypatch, tmp_path, bus=bus)

    await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    assert res.lock_key is not None and res.lock_owner == "worker-1"


async def test_the_lock_key_names_the_repository_and_the_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two runs against different branches of one repository are not in conflict, and a key
    that omitted the branch would serialise them for no reason."""
    deps, state, res, _ = wire(monkeypatch, tmp_path)

    await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    assert res.lock_key is not None
    assert "main" in res.lock_key


# ---- the network guard ----------------------------------------------------------------------


async def test_a_sandbox_that_still_has_network_after_resume_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guarantee `docs/security.md` makes: the agent never sees the repository while
    the container can reach the network.

    On the resume path the container is started and connected for the install, so the
    disconnect is the only thing standing between "installing dependencies" and "an agent
    with a network". Removing the check leaves a run that works perfectly and has quietly
    lost the sandbox's central property.
    """
    sandbox = FakeSandbox(network_after_disconnect=True)
    deps, state, res, _ = wire(monkeypatch, tmp_path, sandbox=sandbox)

    with pytest.raises(SandboxError, match="still has network"):
        await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    assert "disconnect" in sandbox.events, "and it did try to disconnect first"


async def test_the_network_is_disconnected_before_the_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Order matters: connect, install, disconnect, verify. A check before the disconnect
    would pass trivially and prove nothing."""
    sandbox = FakeSandbox()
    deps, state, res, _ = wire(monkeypatch, tmp_path, sandbox=sandbox)

    await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    names = [e if isinstance(e, str) else e[0] for e in sandbox.events]
    assert names.index("connect") < names.index("exec") < names.index("disconnect")


async def test_the_dead_attempts_container_is_removed_before_a_new_one_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "A container from the dead attempt cannot be trusted" — it may hold half an install
    and a network the previous attempt never disconnected."""
    sandbox = FakeSandbox()
    deps, state, res, _ = wire(monkeypatch, tmp_path, sandbox=sandbox)

    await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    names = [e if isinstance(e, str) else e[0] for e in sandbox.events]
    assert names.index("stop") < names.index("start")


# ---- the install CPU allowance --------------------------------------------------------------


async def test_the_cpu_allowance_is_raised_for_the_install_and_put_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dependency installs are the one CPU-hungry part of a run, so they get more cores and
    then give them back.

    Dropped, either the install crawls at the run's ordinary allowance, or — if only the
    restore is dropped — the run keeps the *install* allowance for its whole life, which on
    a box running several runs is one of them taking the others' cores.
    """
    sandbox = FakeSandbox()
    limits = FakeLimits(raises_cpus_to_install=True, install_cpus=4.0, cpus=1.0)
    deps, state, res, _ = wire(monkeypatch, tmp_path, sandbox=sandbox, limits=limits)

    await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    cpu_calls = [e for e in sandbox.events if not isinstance(e, str) and e[0] == "set_cpus"]
    assert [c[1] for c in cpu_calls] == [4.0, 1.0], "raised for the install, then restored"

    names = [e if isinstance(e, str) else e[0] for e in sandbox.events]
    first, last = names.index("set_cpus"), len(names) - 1 - names[::-1].index("set_cpus")
    assert first < names.index("exec") < last, "raised before the install, restored after it"


async def test_the_allowance_is_restored_even_when_the_install_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `finally` is the point. An install that raises must not leave the container
    holding four cores for a run that is about to be retried."""
    sandbox = FakeSandbox()

    async def failing_exec(cmd: str, *, timeout_s: int = 120, env: Any = None) -> Any:
        sandbox.events.append("exec")
        raise SandboxError("install died")

    sandbox.exec = failing_exec  # type: ignore[method-assign]
    limits = FakeLimits(raises_cpus_to_install=True, install_cpus=4.0, cpus=1.0)
    deps, state, res, _ = wire(monkeypatch, tmp_path, sandbox=sandbox, limits=limits)

    with pytest.raises(SandboxError, match="install died"):
        await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    cpu_calls = [e for e in sandbox.events if not isinstance(e, str) and e[0] == "set_cpus"]
    assert [c[1] for c in cpu_calls] == [4.0, 1.0], "restored on the way out of a failure"


async def test_an_image_that_does_not_raise_cpus_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """So the raise cannot become unconditional, which would hand every image the install
    allowance whether its limits asked for it or not."""
    sandbox = FakeSandbox()
    limits = FakeLimits(raises_cpus_to_install=False)
    deps, state, res, _ = wire(monkeypatch, tmp_path, sandbox=sandbox, limits=limits)

    await resume_mod.reattach(state, deps, res, "worker-1")  # type: ignore[arg-type]

    assert not [e for e in sandbox.events if not isinstance(e, str) and e[0] == "set_cpus"]
