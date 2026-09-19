"""The Tester: which failures are the agent's, and which are the repository's.

Two halves. The first is the decision logic — pure functions over a report, because
"did the tests pass" must never depend on a model. The second is the node that wires them,
where the thing worth asserting is *how many times the suite ran*: the short-circuit and
the flaky re-run are both claims about that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agents import tester
from contracts import Frame, TaskGraph, TestFailure, TestReport, ToolResult, Usage
from contracts.plan import Task, TaskSpec
from gateway.provider import LLMProvider
from orchestrator import nodes
from orchestrator.nodes import RunResources
from orchestrator.state import Phase, RunState
from tests.fakes import FakeProviderBase, FakeSandbox, make_ctx
from tools import test_report as tr

pytestmark = pytest.mark.unit

# Run ids whose diff the node collected. A list rather than an attribute bolted onto the
# module under test: the fixture clears it, so each test sees only its own calls.
DIFF_CALLS: list[Any] = []


def fail(
    test_id: str,
    message: str = "AssertionError: assert 3 == 2",
    frames: list[Frame] | None = None,
) -> TestFailure:
    """A failure with a real signature, since filtering is keyed on it."""
    return tr.failure(test_id, message, frames=frames)


def rep(failures: list[TestFailure], total: int = 5, duration_s: float = 0.1) -> TestReport:
    counted = sum(1 for f in failures if "::" in f.test_id)
    return TestReport(
        passed=not failures and total > 0,
        total=total,
        failed=counted,
        errors=len(failures) - counted,
        skipped=0,
        failures=failures,
        duration_s=duration_s,
        command="pytest -q",
        truncated_output="",
        signature=tr.report_signature(failures),
    )


def frame(file: str = "src/pages.py", line: int = 12, in_repo: bool = True) -> Frame:
    return Frame(file=file, line=line, function="paginate", code="x = 1", in_repo=in_repo)


# ---- baseline: failures the agent inherited ----------------------------------------


def test_a_baseline_failure_is_not_the_agents() -> None:
    mine, theirs = fail("tests/a.py::test_new"), fail("tests/b.py::test_old")
    report = rep([mine, theirs])
    kept, dropped = tester.filter_baseline(report, {theirs.signature})

    assert [f.test_id for f in kept.failures] == ["tests/a.py::test_new"]
    assert dropped == ["tests/b.py::test_old"]
    assert kept.failed == 1, "the count follows the failures out"
    assert not kept.passed


def test_a_report_becomes_a_pass_once_the_inherited_failures_are_out() -> None:
    theirs = fail("tests/b.py::test_old")
    kept, dropped = tester.filter_baseline(rep([theirs]), {theirs.signature})
    assert kept.passed and kept.failed == 0 and dropped == ["tests/b.py::test_old"]


def test_the_signature_is_recomputed_so_progress_is_visible() -> None:
    """ "The same failure as last time" has to mean the same *remaining* failures."""
    a, b = fail("tests/a.py::test_one"), fail("tests/b.py::test_two")
    before = rep([a, b])
    kept, _ = tester.filter_baseline(before, {b.signature})
    assert kept.signature != before.signature
    assert kept.signature == tr.report_signature([a])


def test_a_collection_error_leaves_the_error_count_not_the_failed_count() -> None:
    """A node id without `::` was never a running test, so it came out of `errors`."""
    collection = fail("tests/broken.py", "ImportError: No module named 'x'")
    running = fail("tests/a.py::test_one")
    report = rep([collection, running])
    assert (report.errors, report.failed) == (1, 1)

    kept, _ = tester.filter_baseline(report, {collection.signature})
    assert (kept.errors, kept.failed) == (0, 1)


def test_an_unfingerprintable_failure_is_kept_rather_than_forgiven() -> None:
    blank = fail("tests/a.py::test_one").model_copy(update={"signature": ""})
    kept, dropped = tester.filter_baseline(rep([blank]), {""})
    assert dropped == [] and not kept.passed


def test_a_suite_that_collected_nothing_is_never_a_pass() -> None:
    env = tr.failure("<environment>", "no tests were collected", "environment")
    empty = rep([env], total=0)
    kept, _ = tester.filter_baseline(empty, {env.signature})
    assert kept.failures == [] and not kept.passed, "zero tests proved nothing"


def test_filtering_nothing_returns_the_report_untouched() -> None:
    report = rep([fail("tests/a.py::test_one")])
    kept, dropped = tester.filter_baseline(report, set())
    assert kept is report and dropped == []


# ---- the selector: what the task claimed -------------------------------------------


@pytest.mark.parametrize(
    ("selector", "test_id", "covered"),
    [
        ("tests/a.py", "tests/a.py::test_one", True),
        ("tests/a.py::test_one", "tests/a.py::test_one", True),
        ("tests/unit", "tests/unit/test_b.py::test_two", True),
        ("tests/unit/", "tests/unit/test_b.py::test_two", True),
        ("tests/a.py tests/b.py", "tests/b.py::test_two", True),
        # a half-name is a coincidence, not a selection
        ("tests/a", "tests/a.py::test_one", False),
        ("tests/unit", "tests/unittest/test_b.py::x", False),
        ("tests/a.py::test_one", "tests/a.py::test_onerous", False),
        ("tests/b.py", "tests/a.py::test_one", False),
    ],
)
def test_the_selector_matches_on_a_boundary(selector: str, test_id: str, covered: bool) -> None:
    assert tester.covers(selector, test_id) is covered


def test_a_selector_with_flags_is_not_something_we_can_reason_about() -> None:
    assert tester.understood("tests/a.py")
    assert not tester.understood('-k "not slow"'), "matching -k means reimplementing pytest"
    assert not tester.understood("tests/a.py -m integration")


def test_outside_names_only_the_tests_the_task_never_claimed() -> None:
    report = rep(
        [
            fail("tests/a.py::test_mine"),
            fail("tests/z.py::test_theirs"),
            fail("tests/z.py::test_theirs"),  # a test can fail twice in one report
        ]
    )
    assert tester.outside(report, "tests/a.py") == ["tests/z.py::test_theirs"]


@pytest.mark.parametrize("selector", ["", "   ", '-k "not slow"'])
def test_without_a_usable_selector_nothing_is_excused(selector: str) -> None:
    """No claim means no re-roll: every failure is potentially the agent's."""
    report = rep([fail("tests/z.py::test_theirs")])
    assert tester.outside(report, selector) == []


def test_a_parametrised_id_survives_being_put_back_into_a_command() -> None:
    quoted = tester.selector_for(["tests/a.py::test_pages[a b]", "tests/b.py::test_two"])
    assert quoted == "'tests/a.py::test_pages[a b]' tests/b.py::test_two"


def test_flaky_tests_are_dropped_by_id_not_signature() -> None:
    """A flaky test is identified by re-running it, and a re-run has its own frames."""
    a, b = fail("tests/a.py::test_one"), fail("tests/b.py::test_two")
    kept, dropped = tester.without_flaky(rep([a, b]), ["tests/b.py::test_two"])
    assert [f.test_id for f in kept.failures] == ["tests/a.py::test_one"]
    assert dropped == ["tests/b.py::test_two"]


# ---- what the Debugger gets to read ------------------------------------------------


def test_source_context_reads_the_innermost_repository_frames(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pages.py").write_text("\n".join(f"line {i}" for i in range(1, 41)))
    failure = fail(
        "tests/a.py::test_one",
        frames=[
            frame("src/pages.py", 5),
            frame(".venv/lib/thing.py", 9, in_repo=False),
            frame("src/pages.py", 30),
        ],
    )
    context = tester.source_context(rep([failure]), tmp_path, radius=2)
    body = context["tests/a.py::test_one"]

    assert body.startswith("src/pages.py:30"), "innermost first: that is the line that raised"
    assert "> 30 | line 30" in body, "the failing line is marked so nobody has to count"
    assert ".venv" not in body, "library code is not the agent's to read or fix"
    assert body.index("src/pages.py:30") < body.index("src/pages.py:5")


def test_source_context_is_capped_per_failure(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n" * 50)
    failure = fail("tests/a.py::test_one", frames=[frame("a.py", n) for n in range(1, 9)])
    body = tester.source_context(rep([failure]), tmp_path)["tests/a.py::test_one"]
    assert body.count("in paginate") == tester.MAX_CONTEXT_FRAMES


def test_a_failure_with_no_readable_frame_gets_no_entry(tmp_path: Path) -> None:
    failure = fail("tests/a.py::test_one", frames=[frame("gone.py", 3)])
    assert tester.source_context(rep([failure]), tmp_path) == {}


# ---- triage: the one place a model is asked anything --------------------------------


def provider(fake: Any) -> LLMProvider:
    """Only ``parse`` is ever reached, so a stand-in is enough."""
    return cast("LLMProvider", fake)


def test_triage_is_only_for_failures_the_parser_could_not_describe() -> None:
    useless = tr.failure("tests/a.py::test_one", "", "exception")
    described = tr.failure("tests/b.py::test_two", "ValueError: bad width", "exception")
    classified = tr.failure("tests/c.py::test_three", "", "environment")
    picked = tester.needs_triage([useless, described, classified])
    assert [f.test_id for f in picked] == ["tests/a.py::test_one"]


def test_triage_is_capped_so_one_broken_commit_is_not_a_giant_prompt() -> None:
    failures = [tr.failure(f"tests/a.py::test_{i}", "", "exception") for i in range(40)]
    assert len(tester.needs_triage(failures)) == tester.MAX_TRIAGE


async def test_triage_with_nothing_to_classify_does_not_call_a_model() -> None:
    class Provider(FakeProviderBase):
        async def parse(self, req: Any, output: Any) -> Any:
            raise AssertionError("no model call should happen")

    kinds = await tester.TesterAgent().classify_unknown(provider(Provider()), [])
    assert kinds == {}


async def test_a_triage_failure_does_not_fail_the_run() -> None:
    class Provider(FakeProviderBase):
        async def parse(self, req: Any, output: Any) -> Any:
            raise TimeoutError("the model did not answer")

    kinds = await tester.TesterAgent().classify_unknown(
        provider(Provider()), [tr.failure("tests/a.py::test_one", "", "exception")]
    )
    assert kinds == {}, "advice is not worth a failed run"


async def test_triage_ignores_classifications_for_tests_it_did_not_ask_about() -> None:
    from contracts import FailureClassification, Triage

    class Provider(FakeProviderBase):
        async def parse(self, req: Any, output: Any) -> Any:
            return (
                Triage(
                    classifications=[
                        FailureClassification(test_id="tests/a.py::test_one", kind="environment"),
                        FailureClassification(test_id="tests/invented.py::test_x", kind="import"),
                    ]
                ),
                Usage(),
            )

    kinds = await tester.TesterAgent().classify_unknown(
        provider(Provider()), [tr.failure("tests/a.py::test_one", "", "exception")]
    )
    assert kinds == {"tests/a.py::test_one": "environment"}


def test_a_triage_result_is_a_note_and_never_a_kind() -> None:
    """``kind`` feeds the signature, and a label that can flap would make the transition
    table read three identical attempts as three different ones."""
    notes = tester.render_triage({"tests/a.py::test_one": "environment"})
    assert "environment" in notes["tests/a.py::test_one"]
    assert set(notes) == {"tests/a.py::test_one"}


# ---- the node that wires it together ------------------------------------------------


class FakeTool:
    def __init__(self, owner: ScriptedTool) -> None:
        self.owner = owner

    async def __call__(self, ctx: Any, selector: str = "", **kw: Any) -> ToolResult:
        self.owner.selectors.append(selector)
        report = self.owner.by_selector[selector]
        return ToolResult(
            content=tr.summarize(report),
            is_error=not report.passed,
            artifact=report.model_dump(mode="json"),
        )


class ScriptedTool:
    """``RunTestsTool`` replaced: one scripted report per selector, calls recorded."""

    def __init__(self, by_selector: dict[str, TestReport]) -> None:
        self.by_selector = by_selector
        self.selectors: list[str] = []

    def __call__(self) -> FakeTool:
        return FakeTool(self)


class FakeDeps:
    def __init__(self, provider: Any = None) -> None:
        self.engine = None
        self.bus = None
        self.provider = provider or FakeProviderBase()
        self.events: list[tuple[str, dict[str, Any]]] = []


@pytest.fixture
def node_io(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """The node's I/O, stubbed. What it decides is what is under test."""

    async def emit(deps: Any, run_id: Any, type: str, payload: dict[str, Any]) -> None:
        deps.events.append((type, payload))

    class NullSession:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def start_step(*a: Any, **k: Any) -> Any:
        return uuid4()

    async def noop(*a: Any, **k: Any) -> None:
        return None

    async def run_cost(*a: Any, **k: Any) -> Usage:
        return Usage(cost_usd=0.001)

    DIFF_CALLS.clear()

    async def store_diff(state: Any, deps: Any, res: Any) -> None:
        DIFF_CALLS.append(state.run_id)

    monkeypatch.setattr(nodes, "_store_diff", store_diff)
    monkeypatch.setattr(nodes, "_emit", emit)
    monkeypatch.setattr(nodes, "session", lambda _engine: NullSession())
    # the triage is a model call, so it goes through the hooks and the ledger like any
    # other; both are redirected here, since neither is what these tests are about
    monkeypatch.setattr("orchestrator.hooks.session", lambda _engine: NullSession())
    monkeypatch.setattr("gateway.budget.record_llm_call", noop)
    monkeypatch.setattr("storage.repo.start_step", start_step)
    monkeypatch.setattr("storage.repo.run_cost", run_cost)
    for name in (
        "insert_tool_call",
        "save_artifact",
        "finish_step",
        "upsert_tasks",
        "add_run_cost",
    ):
        monkeypatch.setattr(f"storage.repo.{name}", noop)
    monkeypatch.setattr(
        nodes,
        "_run_context",
        lambda state, res, step_id, role, engine=None: make_ctx(
            tmp_path, FakeSandbox(tmp_path), role=role, engine=engine
        ),
    )
    return monkeypatch


def state_for(selector: str, **kw: Any) -> RunState:
    spec = TaskSpec(
        id="t1",
        title="t",
        description="d",
        depends_on=[],
        files=["a.py"],
        acceptance_criteria=["works"],
        test_selector=selector,
    )
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "goal": "g",
        "repo_url": "https://github.com/a/b",
        "base_branch": "main",
        "work_branch": "agent/x",
        "phase": Phase.TEST,
        "tasks": TaskGraph(tasks=[Task(spec=spec, status="in_progress")]),
        "current_task_id": "t1",
    }
    return RunState(**{**base, **kw})


async def run_node(
    monkeypatch: pytest.MonkeyPatch, tool: ScriptedTool, state: RunState, deps: Any = None
) -> tuple[RunState, Any]:
    deps = deps or FakeDeps()
    monkeypatch.setattr("orchestrator.nodes.RunTestsTool", tool)
    return await nodes.test_node(state, deps, RunResources()), deps


async def test_a_failing_selector_does_not_bother_running_the_suite(
    node_io: pytest.MonkeyPatch,
) -> None:
    """The suite is not informative while the tests the task was written against fail."""
    tool = ScriptedTool({"tests/a.py": rep([fail("tests/a.py::test_mine")])})
    out, _ = await run_node(node_io, tool, state_for("tests/a.py"))

    assert tool.selectors == ["tests/a.py"], "one run, not two"
    assert out.last_test_report is not None and not out.last_test_report.passed
    assert out.tasks is not None and out.tasks.by_id("t1").status == "in_progress"


async def test_the_task_is_done_when_its_tests_and_then_the_suite_pass(
    node_io: pytest.MonkeyPatch,
) -> None:
    tool = ScriptedTool({"tests/a.py": rep([]), "": rep([])})
    out, deps = await run_node(node_io, tool, state_for("tests/a.py"))

    assert tool.selectors == ["tests/a.py", ""], "targeted first, then the whole suite"
    assert out.last_test_report is not None and out.last_test_report.passed
    assert out.tasks is not None and out.tasks.by_id("t1").status == "done"
    assert deps.events[-1][1]["passed"] is True


async def test_a_failure_the_repository_already_had_does_not_fail_the_run(
    node_io: pytest.MonkeyPatch,
) -> None:
    theirs = fail("tests/legacy.py::test_old")
    tool = ScriptedTool({"tests/a.py": rep([]), "": rep([theirs])})
    out, deps = await run_node(
        node_io, tool, state_for("tests/a.py", baseline_failures={theirs.signature})
    )

    assert out.last_test_report is not None and out.last_test_report.passed
    assert out.preexisting_failures == {"tests/legacy.py::test_old"}
    assert deps.events[-1][1]["pre_existing"] == ["tests/legacy.py::test_old"]
    assert tool.selectors == ["tests/a.py", ""], "no re-run: nothing was in doubt"


async def test_a_test_that_passes_alone_is_recorded_rather_than_ignored(
    node_io: pytest.MonkeyPatch,
) -> None:
    loose = fail("tests/z.py::test_unrelated")
    tool = ScriptedTool(
        {
            "tests/a.py": rep([]),
            "": rep([loose]),
            "tests/z.py::test_unrelated": rep([]),  # passes on its own
        }
    )
    out, deps = await run_node(node_io, tool, state_for("tests/a.py"))

    assert tool.selectors == ["tests/a.py", "", "tests/z.py::test_unrelated"]
    assert out.last_test_report is not None and out.last_test_report.passed
    assert out.flaky_tests == {"tests/z.py::test_unrelated"}
    assert deps.events[-1][1]["flaky"] == ["tests/z.py::test_unrelated"]
    assert out.tasks is not None and out.tasks.by_id("t1").status == "done"


async def test_a_test_that_fails_alone_too_is_a_real_failure(node_io: pytest.MonkeyPatch) -> None:
    loose = fail("tests/z.py::test_unrelated")
    tool = ScriptedTool(
        {"tests/a.py": rep([]), "": rep([loose]), "tests/z.py::test_unrelated": rep([loose])}
    )
    out, _ = await run_node(node_io, tool, state_for("tests/a.py"))

    assert out.last_test_report is not None and not out.last_test_report.passed
    assert out.flaky_tests == set()


async def test_a_failure_inside_the_selector_is_never_excused_as_flaky(
    node_io: pytest.MonkeyPatch,
) -> None:
    """The selector is the specification. Re-rolling it would be marking your own work."""
    mine = fail("tests/a.py::test_mine")
    tool = ScriptedTool({"tests/a.py": rep([]), "": rep([mine])})
    out, _ = await run_node(node_io, tool, state_for("tests/a.py"))

    assert tool.selectors == ["tests/a.py", ""], "no third run"
    assert out.last_test_report is not None and not out.last_test_report.passed


async def test_a_task_with_no_selector_runs_the_suite_once(node_io: pytest.MonkeyPatch) -> None:
    tool = ScriptedTool({"": rep([])})
    out, _ = await run_node(node_io, tool, state_for(""))
    assert tool.selectors == [""]
    assert out.last_test_report is not None and out.last_test_report.passed


async def test_without_a_selector_the_baseline_excuses_nothing(
    node_io: pytest.MonkeyPatch,
) -> None:
    """Otherwise a run reports success having done nothing.

    The tests a goal names are failing *before* it starts — that is what makes it a goal.
    With no selector there is no line between "already broken" and "what I was asked to
    fix", so the baseline is not applied at all.
    """
    target = fail("tests/goal.py::test_the_thing_i_was_asked_for")
    tool = ScriptedTool({"": rep([target])})
    out, _ = await run_node(node_io, tool, state_for("", baseline_failures={target.signature}))

    assert out.last_test_report is not None and not out.last_test_report.passed
    assert out.preexisting_failures == set()
    assert out.tasks is not None and out.tasks.by_id("t1").status == "in_progress"


async def test_the_debugger_gets_source_context_for_what_still_fails(
    node_io: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "pages.py").write_text("\n".join(f"line {i}" for i in range(1, 21)))
    mine = fail("tests/a.py::test_mine", frames=[frame("src/pages.py", 10)])
    tool = ScriptedTool({"tests/a.py": rep([mine])})
    out, _ = await run_node(node_io, tool, state_for("tests/a.py"))

    assert "src/pages.py:10" in out.test_context["tests/a.py::test_mine"]


async def test_an_unclassifiable_failure_is_triaged_into_a_note(
    node_io: pytest.MonkeyPatch,
) -> None:
    from contracts import FailureClassification, Triage

    class Provider(FakeProviderBase):
        async def parse(self, req: Any, output: Any) -> Any:
            return (
                Triage(
                    classifications=[
                        FailureClassification(test_id="tests/a.py::test_mine", kind="environment")
                    ]
                ),
                Usage(input_tokens=10, output_tokens=2, cost_usd=0.001),
            )

    blank = tr.failure("tests/a.py::test_mine", "", "exception")
    tool = ScriptedTool({"tests/a.py": rep([blank])})
    out, _ = await run_node(node_io, tool, state_for("tests/a.py"), FakeDeps(Provider()))

    note = out.test_context["tests/a.py::test_mine"]
    assert "environment" in note
    assert out.last_test_report is not None
    assert out.last_test_report.failures[0].kind == "exception", "the report is not relabelled"
    # read back from the ledger, which is where the hooks wrote it
    assert out.usage.cost_usd == pytest.approx(0.001), "a triage call is still spend"


async def test_the_diff_is_collected_once_the_last_task_passes(
    node_io: pytest.MonkeyPatch,
) -> None:
    """The change is final at that point, and a resumed REVIEW should read the diff it was
    reviewed against rather than recompute one from a worktree that has moved."""
    tool = ScriptedTool({"tests/a.py": rep([]), "": rep([])})
    out, _ = await run_node(node_io, tool, state_for("tests/a.py"))

    assert out.tasks is not None and out.tasks.by_id("t1").status == "done"
    assert DIFF_CALLS == [out.run_id], "no diff collected when the run is about to be reviewed"


async def test_the_diff_is_not_collected_while_tasks_remain(
    node_io: pytest.MonkeyPatch,
) -> None:
    """Mid-run the diff is a moving target; collecting it would only cost time."""
    spec_two = TaskSpec(
        id="t2",
        title="t2",
        description="d",
        depends_on=[],
        files=["b.py"],
        acceptance_criteria=["works"],
        test_selector="tests/b.py",
    )
    s = state_for("tests/a.py")
    assert s.tasks is not None
    s.tasks.tasks.append(Task(spec=spec_two))

    tool = ScriptedTool({"tests/a.py": rep([]), "": rep([])})
    await run_node(node_io, tool, s)

    assert DIFF_CALLS == [], "t2 is still waiting"
