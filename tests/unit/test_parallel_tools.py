"""Running several tool calls at once, and the cases where that would be wrong.

Three `search_code` calls against a large tree are three independent reads, and doing them
one after another is most of a Coder turn spent waiting. Doing them at once is worth real
time — and is wrong the moment one of them writes something.

**Parallelism is proved with a barrier, not with a stopwatch.** A timing assertion says
"this was faster than that", which is true on an idle laptop and false on a loaded CI box;
`asyncio.Barrier` says "all three were inside the tool at the same moment", which is the
actual claim and cannot pass by luck. Serial execution is proved the same way, by the
barrier timing out — three calls that cannot all arrive never release it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest

from contracts import ToolResult, Usage
from core.errors import BudgetExhausted, RunCancelled
from gateway import openai_compat_provider as provider_module
from gateway.openai_compat_provider import ChatTurn, OpenAICompatProvider, ToolCallReq
from gateway.provider import NullHooks, Request
from tests.fakes import make_ctx
from tools.base import BaseTool, RunContext

pytestmark = pytest.mark.unit

BARRIER_TIMEOUT_S = 1.0


class Reader(BaseTool):
    """A read-only tool that waits for its siblings before returning.

    Releases only when `parties` calls are inside it at once, which is exactly the
    property under test.
    """

    name = "slow_read"
    description = "read something, slowly"
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    mutating = False
    parallel_safe = True

    def __init__(self, barrier: asyncio.Barrier, order: list[str]) -> None:
        self.barrier = barrier
        self.order = order

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        which = str(kwargs.get("which", "?"))
        self.order.append(f"start:{which}")
        await asyncio.wait_for(self.barrier.wait(), BARRIER_TIMEOUT_S)
        self.order.append(f"end:{which}")
        return ToolResult(content=f"read {which}")


class Writer(BaseTool):
    name = "write_something"
    description = "change something"
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    mutating = True
    parallel_safe = False

    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        which = str(kwargs.get("which", "?"))
        self.order.append(f"start:{which}")
        await asyncio.sleep(0)
        self.order.append(f"end:{which}")
        return ToolResult(content=f"wrote {which}")


class Approver(BaseTool):
    name = "needs_a_human"
    description = "asks first"
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    mutating = False
    parallel_safe = True
    requires_approval = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        return ToolResult(content="asked")


class Exploder(BaseTool):
    name = "explodes"
    description = "raises rather than returning"
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    mutating = False
    parallel_safe = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        raise ValueError("the tool itself is broken")


def provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        model="test/model", api_key="k", base_url="https://openrouter.ai/api/v1"
    )


def calls(name: str, count: int) -> list[ToolCallReq]:
    return [
        ToolCallReq(id=f"c{i}", name=name, arguments=f'{{"which": "{i}"}}') for i in range(count)
    ]


# ---- when the calls may run at once ------------------------------------------------------


async def test_three_reads_are_all_inside_the_tool_at_once(tmp_path: Path) -> None:
    """The barrier releases only if all three arrived, so this cannot pass serially."""
    order: list[str] = []
    tool = Reader(asyncio.Barrier(3), order)

    results = await provider()._run_calls(
        calls("slow_read", 3), {"slow_read": tool}, make_ctx(tmp_path), NullHooks()
    )

    assert results == ["read 0", "read 1", "read 2"]
    assert order[:3] == ["start:0", "start:1", "start:2"], order


async def test_results_come_back_in_the_order_the_model_asked(tmp_path: Path) -> None:
    """The transcript is what the model reads next. Results reordered by completion time
    would attribute one question's answer to another."""

    class Uneven(Reader):
        async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
            which = int(kwargs.get("which", 0))
            await asyncio.sleep((3 - which) * 0.01)  # finishes in reverse
            return ToolResult(content=f"read {which}")

    tool = Uneven(asyncio.Barrier(1), [])

    results = await provider()._run_calls(
        calls("slow_read", 3), {"slow_read": tool}, make_ctx(tmp_path), NullHooks()
    )

    assert results == ["read 0", "read 1", "read 2"]


async def test_a_single_call_does_not_take_the_parallel_path(tmp_path: Path) -> None:
    """One call has nothing to be concurrent with, and `gather` around it is a layer of
    exception rewrapping bought for nothing."""
    order: list[str] = []
    tool = Reader(asyncio.Barrier(1), order)

    results = await provider()._run_calls(
        calls("slow_read", 1), {"slow_read": tool}, make_ctx(tmp_path), NullHooks()
    )

    assert results == ["read 0"] and order == ["start:0", "end:0"]


# ---- when they may not -------------------------------------------------------------------


async def test_one_writer_makes_the_whole_batch_serial(tmp_path: Path) -> None:
    """Every call has to qualify. A batch that is mostly reads still runs in order once
    something in it writes, because 'mostly safe' is not safe.

    Strict `start, end, start, end` is the discriminator: both tools yield to the loop
    part-way through, so run concurrently the second would start before the first ended.
    """
    order: list[str] = []
    reader, writer = Reader(asyncio.Barrier(1), order), Writer(order)
    batch = [
        ToolCallReq(id="c0", name="slow_read", arguments='{"which": "0"}'),
        ToolCallReq(id="c1", name="write_something", arguments='{"which": "1"}'),
    ]

    results = await provider()._run_calls(
        batch, {"slow_read": reader, "write_something": writer}, make_ctx(tmp_path), NullHooks()
    )

    assert order == ["start:0", "end:0", "start:1", "end:1"], order
    assert results == ["read 0", "wrote 1"]


async def test_the_same_batch_without_the_writer_does_overlap(tmp_path: Path) -> None:
    """The control for the test above: it has to be the writer that forces the order, not
    the shape of the fakes."""
    order: list[str] = []
    reader = Reader(asyncio.Barrier(2), order)

    await provider()._run_calls(
        calls("slow_read", 2), {"slow_read": reader}, make_ctx(tmp_path), NullHooks()
    )

    assert order[:2] == ["start:0", "start:1"], order
    # Which of the two the barrier releases first is asyncio's business. That both were
    # inside at once is the claim, and the two starts preceding both ends is what says so.
    assert set(order[2:]) == {"end:0", "end:1"}, order


async def test_two_writers_run_in_order(tmp_path: Path) -> None:
    order: list[str] = []
    writer = Writer(order)

    await provider()._run_calls(
        calls("write_something", 2), {"write_something": writer}, make_ctx(tmp_path), NullHooks()
    )

    assert order == ["start:0", "end:0", "start:1", "end:1"], order


async def test_a_tool_that_asks_a_human_is_never_run_alongside_another(tmp_path: Path) -> None:
    """Approval is a question put through a channel keyed by a per-call counter. Two in
    flight at once is a race for one answer."""
    batch = [
        ToolCallReq(id="c0", name="needs_a_human", arguments="{}"),
        ToolCallReq(id="c1", name="needs_a_human", arguments="{}"),
    ]

    assert not OpenAICompatProvider._parallelisable(batch, {"needs_a_human": Approver()})


async def test_a_tool_the_model_invented_forces_the_serial_path(tmp_path: Path) -> None:
    """Nothing can be assumed about a name that is not in the registry — including that it
    is safe to run beside something else. It becomes an error result on the serial path."""
    batch = [
        ToolCallReq(id="c0", name="slow_read", arguments='{"which": "0"}'),
        ToolCallReq(id="c1", name="no_such_tool", arguments="{}"),
    ]
    order: list[str] = []

    assert not OpenAICompatProvider._parallelisable(
        batch, {"slow_read": Reader(asyncio.Barrier(1), order)}
    )

    results = await provider()._run_calls(
        batch, {"slow_read": Reader(asyncio.Barrier(1), order)}, make_ctx(tmp_path), NullHooks()
    )

    assert results[0] == "read 0"
    assert "unknown tool" in results[1]


# ---- failures and control flow -----------------------------------------------------------


async def test_a_tool_that_raises_becomes_an_error_result_and_the_others_still_answer(
    tmp_path: Path,
) -> None:
    """A tool bug must not cost the run the two calls that worked."""
    batch = [
        ToolCallReq(id="c0", name="slow_read", arguments='{"which": "0"}'),
        ToolCallReq(id="c1", name="explodes", arguments="{}"),
    ]

    results = await provider()._run_calls(
        batch,
        {"slow_read": Reader(asyncio.Barrier(1), []), "explodes": Exploder()},
        make_ctx(tmp_path),
        NullHooks(),
    )

    assert results[0] == "read 0"
    assert results[1].startswith("ERROR: tool error: ValueError")


class Cancelling(NullHooks):
    """`before_tool` raising is how a cancel and an exhausted budget reach the loop."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.seen: list[str] = []

    async def before_tool(self, name: str, input: dict[str, Any]) -> str | None:
        self.seen.append(name)
        raise self.error


@pytest.mark.parametrize(
    "error", [RunCancelled("cancelled"), BudgetExhausted("budget_usd")], ids=["cancel", "budget"]
)
async def test_a_hook_raising_stops_the_batch_and_leaves_nothing_running(
    tmp_path: Path, error: BaseException
) -> None:
    """A bare `gather` would propagate the first of these and leave its siblings running
    against a run that is over — writing ledger rows for a cancelled run and logging
    'never retrieved' for whatever they raised.

    `hooks.seen` is the proof that nothing was orphaned: all three calls reached
    `before_tool` and all three had settled before the exception was re-raised.
    """
    hooks = Cancelling(error)

    with pytest.raises(type(error)):
        await provider()._run_calls(
            calls("slow_read", 3),
            {"slow_read": Reader(asyncio.Barrier(3), [])},
            make_ctx(tmp_path),
            hooks,
        )

    assert hooks.seen == ["slow_read"] * 3, "every call was settled, not just the first"


async def test_a_denied_call_does_not_stop_the_others(tmp_path: Path) -> None:
    """A denial is an answer, not a failure: `before_tool` returning a reason is how the
    budget gate and the hypothesis gate talk to the model."""

    class DenyOne(NullHooks):
        async def before_tool(self, name: str, input: dict[str, Any]) -> str | None:
            return "not this one" if input.get("which") == "1" else None

    results = await provider()._run_calls(
        calls("slow_read", 3),
        {"slow_read": Reader(asyncio.Barrier(2), [])},
        make_ctx(tmp_path),
        DenyOne(),
    )

    assert results[0] == "read 0" and results[2] == "read 2"
    assert results[1] == "ERROR: denied: not this one"


# ---- through the loop --------------------------------------------------------------------


async def test_the_loop_appends_one_tool_message_per_call_in_order(tmp_path: Path) -> None:
    """The OpenAI wire format has one `tool` message per `tool_call_id`; there is no
    single user message to put them all in, as there is with Anthropic's blocks. What has
    to hold is that every call is answered exactly once, in order."""

    class Scripted(OpenAICompatProvider):
        def __init__(self, script: list[ChatTurn]) -> None:
            super().__init__(
                model="test/model", api_key="k", base_url="https://openrouter.ai/api/v1"
            )
            self.script = list(script)
            self.requests: list[dict[str, Any]] = []

        async def _complete(self, **kwargs: Any) -> ChatTurn:
            import copy

            self.requests.append(copy.deepcopy(kwargs))
            return self.script.pop(0)

    turn_with_calls = ChatTurn(
        content=None,
        tool_calls=calls("slow_read", 3),
        finish_reason="tool_calls",
        usage=Usage(),
        raw_message={"role": "assistant", "content": ""},
    )
    done = ChatTurn(
        content="done",
        tool_calls=[],
        finish_reason="stop",
        usage=Usage(),
        raw_message={"role": "assistant", "content": "done"},
    )
    p = Scripted([turn_with_calls, done])

    await p.run_tools(
        Request(role="coder", system="s"),
        [Reader(asyncio.Barrier(3), [])],
        make_ctx(tmp_path),
        NullHooks(),
    )

    tool_messages = [m for m in p.requests[-1]["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c0", "c1", "c2"]
    assert [m["content"] for m in tool_messages] == ["read 0", "read 1", "read 2"]


async def test_a_large_batch_is_bounded_rather_than_all_at_once(tmp_path: Path) -> None:
    """Not a throughput knob. Every call ends in `after_tool`, which opens a session to
    write its ledger row, and a model asking for twenty reads in one turn would take
    twenty connections from a pool sized for five — deadlocking the step this was meant
    to speed up."""
    peak = 0
    live = 0

    class Counting(BaseTool):
        name = "slow_read"
        description = "read"
        input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
        mutating = False
        parallel_safe = True

        async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
            nonlocal peak, live
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return ToolResult(content="read")

    count = provider_module.MAX_PARALLEL_TOOLS * 2
    results = await provider()._run_calls(
        calls("slow_read", count), {"slow_read": Counting()}, make_ctx(tmp_path), NullHooks()
    )

    assert len(results) == count, "every call is still answered"
    assert peak == provider_module.MAX_PARALLEL_TOOLS, f"peak concurrency was {peak}"
