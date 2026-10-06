"""SETUP's two promises about what a run spends and what it can reach.

`tests/unit/test_resume_reattach.py` covers the resume path's copy of the network guard —
it exists because exactly this mutation survived there once. The SETUP twin, which is the
path every run takes and the resume path is the exception to, had no equivalent. Two
mutations survived the whole suite:

| mutation | consequence |
|---|---|
| `if await sandbox.has_network():` → `if False:` | **the Coder works with a live network** |
| `state.waiting_s += seconds` → `pass` | a run parked on a human is killed for being slow |

The first falsifies the claim `docs/` §2 makes, that the container "is disconnected before
the agent sees the repository". Nothing fails when it goes: the disconnect is still
attempted, the run still proceeds, and a container that ignored the disconnect — a bad
network driver, a sandbox implementation whose disconnect is a no-op — carries its network
into CODE, where the agent can exfiltrate the repository or pull in a dependency nobody
reviewed. The guard is the only thing that converts "we asked" into "we checked".

The second is the single line joining `ApprovalGate`'s `on_wait` callback to the run's
`waiting_s`, and §9's promise that "waiting for a human does not consume the wall-clock
budget" rests entirely on it. It was invisible because nothing exercised *this* wiring:
`tests/integration/test_approvals.py` substitutes its own `on_wait`, and the two tests that
assert `waiting_s > 0` go through `awaiting_input_node`, which increments `waiting_s`
inline and never calls this callback at all. So the mid-tool-call approval wait — the long
one, four hours by default, against a 45-minute wall-clock budget — had nothing keeping it
out of the run's elapsed time.

Fakes rather than Docker and Redis: every assertion here is about which branch is taken and
which number moves, and a test that needed a container to find out would not run on every
commit. The sandbox fake is the one from `test_resume_reattach.py`, for the same reason it
was written there.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from contracts import ExecResult, RepoFacts
from core.errors import SandboxError
from orchestrator import approvals as approvals_mod
from orchestrator import nodes
from orchestrator.state import Phase, RunState
from sandbox.select import Limits

pytestmark = pytest.mark.unit

RUN_ID = uuid.UUID("6b1e4c90-0000-4000-8000-00000000abcd")
# Distinctive on purpose: `starting_commit` is stubbed, so a `base_sha` equal to anything
# the fakes would produce anyway could not tell us the real path had run.
SETUP_SHA = "5ed0facade" * 4
# The gate's own default timeout, which is what a wait that nobody answers actually costs.
FOUR_HOURS_S = 4 * 3600.0
# Not a round number and not zero: `waiting_s` must be *added to*, not assigned.
ALREADY_WAITED_S = 12.5

FACTS = RepoFacts(
    languages=["python"],
    package_manager="uv",
    install_command="uv sync --all-extras",
    file_count=37,
)
LIMITS = Limits(mem_limit="2g", tmpfs_size="512m", cpus=1.0, install_cpus=4.0)


# ---- the fakes -------------------------------------------------------------------------------


@dataclass
class FakeSandbox:
    """Records the order of what it was told to do, which is half of what this file asks.

    `network_after_disconnect` is the container that ignored the disconnect — the only
    thing the guard exists to catch, and the thing a real Docker daemon will not do on
    demand.
    """

    image: str = "agent-sandbox:python-3.12"
    events: list[str] = field(default_factory=list)
    network_after_disconnect: bool = False

    async def start(self) -> None:
        self.events.append("start")

    async def connect_install_network(self) -> None:
        self.events.append("connect")

    async def set_cpus(self, cpus: float) -> None:
        self.events.append("set_cpus")

    async def exec(
        self, cmd: str, *, timeout_s: int = 120, env: dict[str, str] | None = None
    ) -> ExecResult:
        self.events.append("exec")
        return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    async def disconnect_network(self) -> None:
        self.events.append("disconnect")

    async def has_network(self) -> bool:
        self.events.append("has_network")
        return self.network_after_disconnect


@dataclass
class FakeBus:
    """Both of the run's uses of Redis: the repository lock, and the approval inbox."""

    lock_held_by: str | None = None
    inbox: list[dict[str, Any]] = field(default_factory=list)
    parked_on: list[str] = field(default_factory=list)
    cleared: int = 0

    async def acquire_lock(self, key: str, owner: str, ttl_s: int) -> bool:
        return self.lock_held_by is None

    async def lock_owner(self, key: str) -> str | None:
        return self.lock_held_by

    async def renew_lock(self, key: str, owner: str, ttl_s: int) -> bool:
        return True

    async def set_pending(self, run_id: Any, tool_call_id: str) -> None:
        self.parked_on.append(tool_call_id)

    async def clear_pending(self, run_id: Any) -> None:
        self.cleared += 1

    async def pop_inbox(self, run_id: Any, timeout_s: int) -> dict[str, Any] | None:
        return self.inbox.pop(0) if self.inbox else None

    async def push_inbox(self, run_id: Any, message: dict[str, Any]) -> None:
        self.inbox.append(message)

    async def is_cancelled(self, run_id: Any) -> bool:
        return False


@dataclass
class FakeSettings:
    github_token: Any = None

    def proxy_env(self) -> dict[str, str]:
        return {"HTTPS_PROXY": "http://proxy:3128"}


@dataclass
class FakeDeps:
    bus: FakeBus
    settings: FakeSettings
    sandbox: FakeSandbox
    engine: Any = None
    worktrees: Path = Path("/nonexistent")
    repos: Path = Path("/nonexistent")

    def repos_dir(self) -> Path:
        return self.repos

    def worktrees_dir(self) -> Path:
        return self.worktrees

    def sandbox_factory(
        self, run_id: Any, workspace: Path, facts: Any = None, image: str | None = None
    ) -> FakeSandbox:
        return self.sandbox


def run_state(
    *,
    waiting_s: float = 0.0,
    started_at: datetime | None = None,
    unattended: bool = False,
) -> RunState:
    return RunState(
        run_id=RUN_ID,
        goal="Make the failing integration test pass without touching the public API.",
        repo_url="https://github.com/acme/isolated",
        base_branch="trunk",
        work_branch=f"agent/{RUN_ID}",
        waiting_s=waiting_s,
        started_at=started_at,
        unattended=unattended,
    )


def wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    sandbox: FakeSandbox,
) -> tuple[FakeDeps, RunState, nodes.RunResources, list[tuple[str, dict[str, Any]]]]:
    """Everything `setup_node` reaches for outside the container, replaced.

    The clone, the worktree, the symbol index and the embedding pass are all stubbed: none
    of them is what this file is about, and each would otherwise need a git repository, a
    database or both. What is left real is the sequence inside the node — connect, install,
    disconnect, check — which is the subject.
    """
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def fake_bare(url: str, repos: Path, token: str | None) -> Path:
        return tmp_path / "bare.git"

    async def fake_starting_commit(bare: Path, state: RunState) -> str:
        return SETUP_SHA

    async def fake_create(bare: Path, worktrees: Path, run_id: Any, committish: str) -> Any:
        return type("WT", (), {"path": tmp_path / "wt"})()

    async def fake_index_repo(engine: Any, worktree: Path, sha: str) -> None:
        return None

    async def fake_embeddings(deps: Any, worktree: Path, repo_sha: str) -> None:
        return None

    async def fake_renew_forever(deps: Any, key: str, owner: str) -> None:
        return None

    async def fake_emit(deps: Any, run_id: Any, event: str, payload: dict[str, Any]) -> None:
        emitted.append((event, payload))

    monkeypatch.setattr(nodes, "ensure_bare_clone", fake_bare)
    monkeypatch.setattr(nodes, "starting_commit", fake_starting_commit)
    monkeypatch.setattr("orchestrator.nodes.wt.create", fake_create)
    monkeypatch.setattr("orchestrator.nodes.repo_profile.collect", lambda path: FACTS)
    monkeypatch.setattr("orchestrator.nodes.select.limits_for", lambda facts, image: LIMITS)
    monkeypatch.setattr("orchestrator.nodes.symbols.index_repo", fake_index_repo)
    monkeypatch.setattr(nodes, "_index_embeddings", fake_embeddings)
    monkeypatch.setattr(nodes, "_renew_forever", fake_renew_forever)
    monkeypatch.setattr(nodes, "_emit", fake_emit)
    monkeypatch.setattr(nodes, "install_command", lambda worktree, facts: "uv sync --all-extras")

    deps = FakeDeps(
        bus=FakeBus(),
        settings=FakeSettings(),
        sandbox=sandbox,
        worktrees=tmp_path / "worktrees",
        repos=tmp_path / "repos",
    )
    return deps, run_state(), nodes.RunResources(), emitted


# ---- the network guard -----------------------------------------------------------------------


async def test_a_sandbox_that_still_has_network_at_the_end_of_setup_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent must never see the repository from a container that can reach the internet.

    SETUP deliberately opens a network for the dependency install, so for one window the
    container has egress. The check after the disconnect is the only thing that establishes
    the window actually closed. Without it a container whose disconnect silently failed goes
    on to CODE fully connected: the Coder can post the repository anywhere, and every
    dependency it installs mid-run arrives unreviewed. Nothing errors and no log says so.
    """
    sandbox = FakeSandbox(network_after_disconnect=True)
    deps, state, res, emitted = wire(monkeypatch, tmp_path, sandbox=sandbox)

    with pytest.raises(SandboxError, match="still has network"):
        await nodes.setup_node(state, deps, res)  # type: ignore[arg-type]

    assert "disconnect" in sandbox.events, "and it did ask the container to disconnect first"
    assert emitted == [], "a run that failed isolation must not announce itself into ANALYZE"


async def test_a_sandbox_that_is_offline_after_the_disconnect_finishes_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The counterweight: the ordinary run, where the disconnect worked.

    Without this, a guard that refused every sandbox — or a `has_network` read as `not
    has_network` — would satisfy the test above while making every run fail in SETUP.
    """
    sandbox = FakeSandbox(network_after_disconnect=False)
    deps, state, res, emitted = wire(monkeypatch, tmp_path, sandbox=sandbox)

    out = await nodes.setup_node(state, deps, res)  # type: ignore[arg-type]

    assert out.base_sha == SETUP_SHA, "the real path ran rather than failing early somewhere"
    assert emitted == [("phase_changed", {"phase": Phase.ANALYZE.value})], (
        "an isolated sandbox hands the run on to ANALYZE"
    )


async def test_the_install_network_is_disconnected_before_the_check_is_made(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Order is what makes the check mean anything.

    Asked before the disconnect, `has_network` would be answering about a window that is
    meant to be open, so it would pass on every run and prove nothing — a guard that is
    present, green, and worthless. The install must also happen inside that window: moved
    after the disconnect it fails on every repository with a dependency.
    """
    sandbox = FakeSandbox()
    deps, state, res, _ = wire(monkeypatch, tmp_path, sandbox=sandbox)

    await nodes.setup_node(state, deps, res)  # type: ignore[arg-type]

    order = sandbox.events
    assert order.index("connect") < order.index("exec") < order.index("disconnect"), (
        f"the install belongs inside the network window, got {order}"
    )
    assert "has_network" in order, (
        f"SETUP never asked whether the container was offline, got {order}"
    )
    assert order.index("disconnect") < order.index("has_network"), (
        f"the check must be made after the window is closed, got {order}"
    )


# ---- the approval wait and the wall clock ------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate writes a run status and an event row; neither is what is being asked here.

    Only `ApprovalGate.wait` touches these, so the SETUP tests above are unaffected.
    """

    class NullSession:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def noop(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(approvals_mod, "session", lambda _engine: NullSession())
    monkeypatch.setattr("storage.repo.set_run_status", noop)
    monkeypatch.setattr(approvals_mod, "emit", noop)


@dataclass
class ScriptedClock:
    """The only clock `ApprovalGate` reads, so a four-hour wait costs the test no time.

    Replaces the `time` module *inside* `orchestrator.approvals` rather than patching
    `time.monotonic` globally, which the event loop also reads.
    """

    readings: list[float]
    taken: int = 0

    def monotonic(self) -> float:
        assert self.taken < len(self.readings), (
            "the gate read the clock more often than this test scripted; the wait is no "
            "longer the shape this test assumes"
        )
        value = self.readings[self.taken]
        self.taken += 1
        return value


def gate_for(state: RunState, bus: FakeBus) -> Any:
    """The gate `nodes` builds for a run, with nothing about it substituted."""
    deps = FakeDeps(bus=bus, settings=FakeSettings(), sandbox=FakeSandbox())
    res = nodes.RunResources(lock_key="lock:repo:deadbeef:trunk", lock_owner="worker-1")
    return nodes._approval_gate(state, deps, res)  # type: ignore[arg-type]


async def test_four_hours_parked_on_a_human_do_not_count_against_the_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that waited overnight for a reviewer must not wake up over its time budget.

    The default approval timeout is four hours and the default wall clock is forty-five
    minutes, so a single unanswered approval is more than five times the whole budget. With
    the wait uncharged, the run is killed for being slow the moment the human answers — the
    reviewer's thinking time is billed to the agent, and the longer the review the more
    certain the run is to die because of it.
    """
    now = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    # Ninety seconds of actual work, then four hours of nobody looking at it.
    state = run_state(started_at=now - timedelta(seconds=FOUR_HOURS_S + 90.0))
    bus = FakeBus(inbox=[{"type": "approve", "tool_call_id": "toolu_setup_7"}])
    gate = gate_for(state, bus)
    monkeypatch.setattr(approvals_mod, "time", ScriptedClock([1_000.0, 1_000.0 + FOUR_HOURS_S]))

    decision = await gate.wait("approval", "bash", "toolu_setup_7", {"command": "rm -rf build"})

    assert decision.approved, "the human approved; this test is about the clock, not the answer"
    assert bus.parked_on == ["toolu_setup_7"], "the run really did park on this call"
    assert state.waiting_s == FOUR_HOURS_S, "the whole wait is charged to waiting time"
    assert state.elapsed_s(now) == 90.0, "the run has done ninety seconds of work"
    assert state.elapsed_s(now) < state.budget.wall_clock_s, (
        "and is comfortably inside a budget its raw wall clock exceeds five times over"
    )


async def test_a_second_wait_adds_to_the_first_rather_than_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs are asked for several approvals, and the budget has to survive all of them.

    Assigning rather than accumulating would leave a run that was stopped four times
    crediting only the last pause, so the earlier waits would silently return to the
    agent's wall clock and the run would be cut short mid-fix.
    """
    state = run_state(waiting_s=ALREADY_WAITED_S)
    gate = gate_for(state, FakeBus())

    gate.on_wait(FOUR_HOURS_S)

    assert state.waiting_s == ALREADY_WAITED_S + FOUR_HOURS_S, (
        "what this run had already waited is still part of what it has waited"
    )


async def test_a_run_that_never_parked_is_not_credited_with_waiting_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterweight, and it protects the budget from the other side.

    `waiting_s` is subtracted from elapsed time, so inventing a wait buys the agent wall
    clock it never earned — an unattended run, which asks nobody and waits for nothing,
    would quietly get four extra hours on every refused tool call. An unattended refusal is
    exactly the path that must leave the clock alone.
    """
    now = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    state = run_state(
        waiting_s=ALREADY_WAITED_S,
        started_at=now - timedelta(seconds=ALREADY_WAITED_S + 300.0),
        unattended=True,
    )
    bus = FakeBus()
    gate = gate_for(state, bus)

    decision = await gate.wait("approval", "bash", "toolu_setup_8", {"command": "rm -rf build"})

    assert not decision.approved and decision.reason == approvals_mod.UNATTENDED_REJECTION
    assert bus.parked_on == [], "nobody was asked, so nothing was waited for"
    assert state.waiting_s == ALREADY_WAITED_S, "and nothing new was deducted from the clock"
    assert state.elapsed_s(now) == 300.0, "the run is still spending its budget as it works"
