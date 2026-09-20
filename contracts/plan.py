from __future__ import annotations

from typing import Literal

from pydantic import Field

from contracts.common import LLMModel, StateModel


class ImplementationPlan(LLMModel):
    """Produced by the Planner. Non-empty ``open_questions`` pauses the run in AWAITING_INPUT."""

    approach: str
    affected_files: list[str]
    new_files: list[str]
    risks: list[str]
    test_strategy: str
    open_questions: list[str]


class TaskSpec(LLMModel):
    """One unit of work as emitted by the Decomposer."""

    id: str = Field(min_length=1, max_length=64)
    title: str
    description: str
    depends_on: list[str]
    files: list[str]
    acceptance_criteria: list[str]
    # Empty means "run the whole suite", which is what the rest of the system already
    # assumes: `Stack.test_command` falls back to the stack's default selector on an empty
    # string, the Coder prompt renders it as "(full suite)", and four call sites construct
    # a TaskSpec with `test_selector=""`. It was nevertheless a required field, so the
    # Decomposer had to invent a selector for work that does not narrow to one — and a
    # model that answered honestly with `null` failed the whole graph. The default makes
    # the schema say what the code already meant.
    test_selector: str = ""


class TaskGraphSpec(LLMModel):
    """The Decomposer's whole answer: a DAG in topological order."""

    tasks: list[TaskSpec] = Field(min_length=1, max_length=8)


TaskStatus = Literal["pending", "in_progress", "done", "failed"]
# Where a task came from. Runtime metadata on `Task` rather than a field on `TaskSpec`,
# because `TaskSpec` is what the Decomposer writes: putting `kind` there would put it in
# the schema the model fills in, and where a task came from is not the model's to decide.
TaskKind = Literal["feature", "fix"]


class Task(StateModel):
    """Runtime wrapper around a TaskSpec."""

    spec: TaskSpec
    status: TaskStatus = "pending"
    kind: TaskKind = "feature"
    attempts: int = 0
    replanned: bool = False
    # HEAD before the Coder started, so an exhausted task can be rewound to a clean
    # base instead of a replan inheriting three attempts' worth of half-edits.
    task_start_sha: str | None = None
    # What a human said when the run escalated to them. Carried into the Debugger prompt.
    human_hint: str | None = None

    @property
    def id(self) -> str:
        return self.spec.id


class TaskGraph(StateModel):
    tasks: list[Task]

    @classmethod
    def from_spec(cls, spec: TaskGraphSpec) -> TaskGraph:
        return cls(tasks=[Task(spec=t) for t in spec.tasks])

    def by_id(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def next_ready(self) -> Task | None:
        """First pending task whose dependencies are all done, in list order."""
        done = {t.id for t in self.tasks if t.status == "done"}
        for t in self.tasks:
            if t.status == "pending" and all(d in done for d in t.spec.depends_on):
                return t
        return None

    def validate_dag(self) -> list[str]:
        """Human-readable problems: duplicate ids, unknown or self dependencies, cycles."""
        problems: list[str] = []
        ids = [t.id for t in self.tasks]
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            problems.append(f"duplicate task id: {dup}")
        known = set(ids)
        for t in self.tasks:
            for d in t.spec.depends_on:
                if d == t.id:
                    problems.append(f"task {t.id} depends on itself")
                elif d not in known:
                    problems.append(f"task {t.id} depends on unknown task {d}")
        problems.extend(self._cycles(known))
        return problems

    def _cycles(self, known: set[str]) -> list[str]:
        edges = {
            t.id: [d for d in t.spec.depends_on if d in known and d != t.id] for t in self.tasks
        }
        white, grey, black = 0, 1, 2
        colour = dict.fromkeys(edges, white)
        found: list[str] = []

        def visit(node: str, path: list[str]) -> None:
            colour[node] = grey
            for nxt in edges[node]:
                if colour[nxt] == grey:
                    cycle = [*path[path.index(nxt) :], nxt]
                    found.append("cycle: " + " -> ".join(cycle))
                elif colour[nxt] == white:
                    visit(nxt, [*path, nxt])
            colour[node] = black

        for start in edges:
            if colour[start] == white:
                visit(start, [start])
        return found
