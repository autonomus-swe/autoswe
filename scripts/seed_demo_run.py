"""Drive a believable run through every phase, with no model and no quota.

The console is hard to exercise otherwise: a real run costs requests from a small free
allowance, and the interesting states (a live stream, a task part-finished, a run parked
on a question) are exactly the ones that are awkward to reach on demand. This writes the
same rows and publishes the same events the orchestrator does, at a pace you can watch.

    uv run python scripts/seed_demo_run.py              # runs to done, ~40s
    uv run python scripts/seed_demo_run.py --ask         # parks on a question and waits
    uv run python scripts/seed_demo_run.py --speed 3     # 3x slower, easier to watch
    uv run python scripts/seed_demo_run.py --fail        # ends failed, for the red path

It is a fixture, not a simulator: nothing here proves the orchestrator works. The tests
do that. This exists so the interface can be looked at.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import AsyncEngine

from contracts import Budget, TaskGraph, TaskGraphSpec, Usage
from core.settings import get_settings
from orchestrator.events import emit
from storage import repo as db
from storage.db import make_engine, session
from storage.redis import RedisBus

GOAL = (
    "DEMO — Add a Stats class in fixture/stats.py with mean(), median() and mode(), "
    "a CLI entry point, and tests for each."
)
# stdout is not a tty when piped or backgrounded, and a progress log that only
# appears at exit is useless for watching alongside the console
say = partial(print, flush=True)

REPO_URL = "https://github.com/Vatsalya001/autoswe-fixture-python"
MODEL = "demo/seeded"

TASKS = TaskGraphSpec.model_validate(
    {
        "tasks": [
            {
                "id": "t1",
                "title": "Add the Stats class with mean, median and mode",
                "description": "Implement the three statistics in fixture/stats.py.",
                "depends_on": [],
                "files": ["fixture/stats.py"],
                "acceptance_criteria": ["mean, median and mode return correct values"],
                "test_selector": "tests/test_stats.py",
            },
            {
                "id": "t2",
                "title": "Add the stdin CLI entry point",
                "description": "fixture/__main__.py reads numbers and prints all three.",
                "depends_on": ["t1"],
                "files": ["fixture/__main__.py"],
                "acceptance_criteria": ["python -m fixture prints all three statistics"],
                "test_selector": "tests/test_cli.py",
            },
            {
                "id": "t3",
                "title": "Cover both with tests",
                "description": "Tests for the class and the entry point.",
                "depends_on": ["t1", "t2"],
                "files": ["tests/test_stats.py", "tests/test_cli.py"],
                "acceptance_criteria": ["the suite passes"],
                "test_selector": "tests/",
            },
        ]
    }
)

# (tool, input, output, ms) per coding task — enough to fill the tool-call table
TOOL_SCRIPT: list[tuple[str, dict[str, Any], str, int]] = [
    ("search_code", {"pattern": "def mean", "path": "."}, "no matches", 41),
    ("read_file", {"path": "fixture/ops.py"}, "def add(a, b):\n    return a + b\n", 7),
    (
        "str_replace_based_edit_tool",
        {"command": "create", "path": "fixture/stats.py"},
        "created",
        12,
    ),
    ("run_tests", {"selector": "tests/test_stats.py"}, "3 passed", 2140),
    ("git_status", {}, "M fixture/stats.py", 9),
    ("git_commit", {"message": "feat(stats): add the Stats class"}, "committed 1 file", 23),
    ("submit_result", {"summary": "Added the Stats class"}, "accepted", 1),
]


class Seeder:
    def __init__(self, engine: AsyncEngine, bus: RedisBus, run_id: UUID, speed: float) -> None:
        self.engine, self.bus, self.run_id, self.speed = engine, bus, run_id, speed
        self.seq = 0

    async def beat(self, seconds: float = 0.6) -> None:
        await asyncio.sleep(seconds * self.speed)

    async def event(self, type_: str, payload: dict[str, Any]) -> None:
        await emit(self.bus, self.engine, self.run_id, type_, payload)

    async def phase(self, name: str, status: str = "running", **extra: Any) -> None:
        async with session(self.engine) as s:
            await db.set_run_phase(s, self.run_id, name, status)
        await self.event("phase_changed", {"phase": name, **extra})
        say(f"  phase -> {name}")
        await self.beat()

    async def agent(
        self,
        name: str,
        phase: str,
        tools: list[tuple[str, dict[str, Any], str, int]],
        task_id: str | None = None,
        turns: int = 3,
    ) -> None:
        """One agent step, with its tool calls and model turns recorded as rows."""
        async with session(self.engine) as s:
            step_id = await db.start_step(
                s,
                run_id=self.run_id,
                task_id=task_id,
                agent=name,
                phase=phase,
                input={"goal": GOAL},
                attempt=0,
            )
        await self.event(
            "agent_started", {"agent": name, **({"task_id": task_id} if task_id else {})}
        )

        total = Usage()
        for i, (tool, payload, output, ms) in enumerate(tools):
            self.seq += 1
            async with session(self.engine) as s:
                await db.insert_tool_call(
                    s,
                    step_id=step_id,
                    name=tool,
                    input=payload,
                    output_preview=output,
                    exit_code=0,
                    duration_ms=ms,
                )
            await self.event("tool_call", {"name": tool, "is_error": False, "duration_ms": ms})
            use = Usage(input_tokens=1200 + i * 700, output_tokens=90 + i * 20, cost_usd=0.0)
            total = total.add(use)
            async with session(self.engine) as s:
                await db.insert_llm_call(
                    s,
                    step_id=step_id,
                    provider="demo",
                    model=MODEL,
                    effort="medium",
                    usage=use,
                    latency_ms=900 + i * 250,
                    stop_reason="tool_calls",
                )
            await self.beat(0.35)

        for _ in range(max(0, turns - len(tools))):
            use = Usage(input_tokens=1500, output_tokens=120, cost_usd=0.0)
            total = total.add(use)
            async with session(self.engine) as s:
                await db.insert_llm_call(
                    s,
                    step_id=step_id,
                    provider="demo",
                    model=MODEL,
                    effort="medium",
                    usage=use,
                    latency_ms=1100,
                    stop_reason="stop",
                )
        async with session(self.engine) as s:
            await db.finish_step(s, step_id, output={"ok": True}, error=None, usage=total)
            await db.set_run_cost(s, self.run_id, 0.0)
        await self.event(
            "agent_finished", {"agent": name, **({"task_id": task_id} if task_id else {})}
        )
        await self.beat()

    async def ask(self, questions: list[str]) -> str:
        """Park the run and wait for a real answer, from the UI or the CLI."""
        async with session(self.engine) as s:
            await db.set_run_phase(s, self.run_id, "plan", "awaiting_input")
        await self.event("awaiting_input", {"kind": "open_questions", "questions": questions})
        say("\n  parked on a question. Answer it in the console, or:")
        say(f"    uv run autoswe answer {self.run_id} 'your answer'\n")
        while True:
            message = await self.bus.pop_inbox(self.run_id, timeout_s=5)
            if message and message.get("type") == "answer":
                text = str(message.get("text", ""))
                say(f"  answer received: {text[:70]}")
                await self.event("log", {"message": "answer received"})
                return text
            if await self.bus.is_cancelled(self.run_id):
                return ""

    async def cancelled(self) -> bool:
        return await self.bus.is_cancelled(self.run_id)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ask", action="store_true", help="park on an open question and wait")
    ap.add_argument("--fail", action="store_true", help="end the run failed instead of done")
    ap.add_argument("--speed", type=float, default=1.0, help="multiply every pause")
    args = ap.parse_args()

    st = get_settings()
    engine = make_engine(st.database_url)
    bus = RedisBus(st.redis_url)

    async with session(engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=REPO_URL,
            base_branch="main",
            goal=GOAL,
            budget=Budget(),
            provider="demo",
            unattended=not args.ask,
        )
        await db.mark_run_started(s, run_id)
    say(f"\nrun {run_id}\nwatch it at http://127.0.0.1:8000/\n")

    d = Seeder(engine, bus, run_id, args.speed)
    try:
        await d.phase("setup")
        await d.phase("analyze")
        await d.agent("analyzer", "analyze", TOOL_SCRIPT[:2], turns=2)
        await d.phase("plan")
        await d.agent("planner", "plan", [], turns=3)

        if args.ask:
            await d.ask(
                [
                    "Should mode() return every modal value, or just the first?",
                    "Should the CLI read whitespace-separated numbers or one per line?",
                ]
            )
            await d.phase("plan")
            await d.agent("planner", "plan", [], turns=2)

        await d.phase("decompose")
        await d.agent("decomposer", "decompose", [], turns=2)
        graph = TaskGraph.from_spec(TASKS)
        async with session(engine) as s:
            await db.upsert_tasks(s, run_id, graph)
        await d.phase("code", tasks=len(graph.tasks))

        for i, task in enumerate(graph.tasks):
            if await d.cancelled():
                break
            task.status = "in_progress"
            async with session(engine) as s:
                await db.upsert_tasks(s, run_id, graph)
            await d.agent("coder", "code", TOOL_SCRIPT, task_id=task.spec.id, turns=8)

            await d.phase("test")
            failed = args.fail and i == len(graph.tasks) - 1
            await d.event(
                "test_report",
                {"total": 3 + i * 2, "failed": 1 if failed else 0, "passed": not failed},
            )
            if failed:
                task.status = "failed"
                async with session(engine) as s:
                    await db.upsert_tasks(s, run_id, graph)
                break
            task.status = "done"
            async with session(engine) as s:
                await db.upsert_tasks(s, run_id, graph)
            if i < len(graph.tasks) - 1:
                await d.phase("code")

        if await d.cancelled():
            async with session(engine) as s:
                await db.finish_run(s, run_id, status="cancelled", error="cancelled")
            await d.event("run_finished", {"status": "cancelled", "cost_usd": 0.0})
            say("  cancelled")
        elif args.fail:
            async with session(engine) as s:
                await db.finish_run(s, run_id, status="failed", error="tests still failing on t3")
            await d.event(
                "run_finished",
                {"status": "failed", "error": "tests still failing on t3", "cost_usd": 0.0},
            )
            say("  failed (as asked)")
        else:
            await d.phase("pr")
            pr = f"{REPO_URL}/pull/42"
            await d.event("pr_opened", {"pr_url": pr})
            async with session(engine) as s:
                await db.finish_run(s, run_id, status="done", error=None, pr_url=pr)
            await d.phase("done", status="done")
            await d.event("run_finished", {"status": "done", "pr_url": pr, "cost_usd": 0.0})
            say("  done")
    finally:
        await bus.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
