"""The Reviewer: two passes, because enumeration and scepticism reward opposite instincts.

The cheap pass (`review_pre`) lists everything that could be wrong and is allowed to be
wrong. The expensive pass (`review`) opens each file and decides `confirmed` or
`false_positive` with a reason. One pass doing both either misses findings or reports
noise, and noise is worse: a reviewer that cries wolf is one a human learns to skip.

Two rules hold this together, and both are about not trusting the model with the gate.

**`blocking` is recomputed from the severities.** `ReviewReport.blocking` is a field the
model fills in, and a model that has just written four findings has every incentive to
call the run blocked or not blocked according to how it feels about its own work. The
harness reads the severities and decides. The model judges findings; the code judges the
gate.

**A dropped candidate keeps its reason.** Everything the pre-pass raised and the
verification pass rejected is stored in the artifact with the one-line reason it was
rejected, so "why was this not reported" has an answer that is not "the model changed its
mind".
"""

from __future__ import annotations

from typing import Any, ClassVar

from agents.base import Agent, fence
from agents.submit import submit_tool
from contracts import (
    ImplementationPlan,
    ReviewCandidates,
    ReviewFinding,
    ReviewReport,
    TaskGraph,
    TaskSpec,
)
from core.errors import AgentError
from gateway.provider import Hooks, LLMProvider, RunOutcome
from observability.logging import get_logger
from repo import diff as diffmod
from tools.base import RunContext
from tools.registry import ROLE_TOOLS

log = get_logger(__name__)

REVIEW_KEY = "review"
MAX_ITERATIONS = 40
# A finding the pre-pass produced past this count is noise by volume: a pass that raises
# ninety concerns has stopped discriminating, and the verification pass would spend its
# budget rejecting them one at a time.
MAX_CANDIDATES = 40
# Rough characters-per-token. Used only to decide when to split the pre-pass by file, so
# being off by a third costs one extra request rather than a wrong answer.
# Characters per token, calibrated against a BPE tokenizer on real code — see
# `repo/repomap.CHARS_PER_TOKEN` for the measurement. It was 4, which is the prose figure
# and overestimates code by a third.
CHARS_PER_TOKEN = 2.6
PRE_PASS_TOKEN_BUDGET = 60_000


def finding_key(f: ReviewFinding) -> tuple[str, int, str]:
    """Identity for dedupe. Two passes over one bug should not report it twice."""
    return (f.file, f.line, f.category.strip().lower())


def is_blocking(findings: list[ReviewFinding]) -> bool:
    """The gate, in code. Never read from the model's own boolean."""
    return any(f.severity == "blocking" for f in findings)


def dedupe(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """First of each `(file, line, category)`, in the order they arrived.

    Severity is not part of the key on purpose: the same concern reported twice with two
    severities is one concern, and taking the first keeps the verification pass's judgement
    over the pre-pass's guess.
    """
    seen: set[tuple[str, int, str]] = set()
    out = []
    for f in findings:
        key = finding_key(f)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def render_criteria(tasks: TaskGraph | None) -> str:
    """What the run was asked to achieve — the only way to notice a criterion unmet."""
    if tasks is None or not tasks.tasks:
        return "(no task graph: judge against the goal alone)"
    out = []
    for t in tasks.tasks:
        spec = t.spec
        out.append(f"- **{spec.id} {spec.title}** [{t.status}]")
        for c in spec.acceptance_criteria:
            out.append(f"    - {c}")
    return "\n".join(out)


def group_for_budget(files: list[diffmod.FileDiff]) -> list[list[diffmod.FileDiff]]:
    """Split the diff into groups a single pre-pass request can hold.

    A diff can be larger than any context, and the pre-pass has to see the text to enumerate
    anything. Grouped by file so no file is ever cut in half — a half-read function invites
    exactly the confident wrong finding this design is trying to avoid.
    """
    budget = int(PRE_PASS_TOKEN_BUDGET * CHARS_PER_TOKEN)
    groups: list[list[diffmod.FileDiff]] = [[]]
    size = 0
    for f in files:
        text = f.summary() if diffmod.too_large(f) else f.text
        if groups[-1] and size + len(text) > budget:
            groups.append([])
            size = 0
        groups[-1].append(f)
        size += len(text)
    return [g for g in groups if g]


def pre_message(
    goal: str,
    plan: ImplementationPlan | None,
    tasks: TaskGraph | None,
    files: list[diffmod.FileDiff],
) -> str:
    """Input to the cheap pass: what was asked, what was planned, and the change itself."""
    parts = [
        f"# Goal\n{goal}",
        f"# Approach the plan took\n{plan.approach if plan else '(no plan recorded)'}",
        f"# Tasks and their acceptance criteria\n{render_criteria(tasks)}",
        f"# Change\n{diffmod.diff_stat(files)}",
        fence("the diff under review", diffmod.for_model(files)),
        "List every concern. One finding per concern.",
    ]
    return "\n\n".join(parts)


def verify_message(
    goal: str, candidates: list[ReviewFinding], files: list[diffmod.FileDiff]
) -> str:
    """Input to the expensive pass: the candidates, and the tools to check them with.

    The diff stat rather than the diff: this pass opens files itself with `read_file`, and
    a candidate is only worth confirming against what is actually on disk.
    """
    lines = [f"# Goal\n{goal}", f"# Change\n{diffmod.diff_stat(files)}", "# Candidate findings"]
    if not candidates:
        lines.append("(none — the pre-pass found nothing. Look at the diff yourself.)")
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"\n## {i}. {c.file}:{c.line} [{c.severity}] {c.category}\n"
            f"{c.summary}\n**claimed failure:** {c.failure_scenario}"
        )
    lines.append(
        "\nOpen each file. Confirm or reject every candidate with a one-line reason, then "
        "call submit_review once with the findings you confirmed."
    )
    return "\n\n".join(lines)


class ReviewPreAgent(Agent):
    """Pass one. No tools: it reads the diff it is given and enumerates."""

    role: ClassVar[str] = "review_pre"
    prompt_file: ClassVar[str] = "review_pre"

    async def run(
        self,
        provider: LLMProvider,
        goal: str,
        plan: ImplementationPlan | None,
        tasks: TaskGraph | None,
        files: list[diffmod.FileDiff],
        hooks: Hooks | None = None,
    ) -> list[ReviewFinding]:
        """Candidates from every group, concatenated. Never raises.

        A pre-pass that fails leaves the verification pass with nothing to check, which is
        a worse review rather than no review — it still reads the diff itself. Losing the
        whole run over the cheap half of it would be the wrong trade.
        """
        groups = group_for_budget(files)
        found: list[ReviewFinding] = []
        for i, group in enumerate(groups, 1):
            try:
                candidates = await self.run_structured(
                    provider,
                    pre_message(goal, plan, tasks, group),
                    ReviewCandidates,
                    hooks=hooks,
                )
            except Exception as e:
                log.warning(
                    "review_pre_failed", group=i, of=len(groups), error=f"{type(e).__name__}: {e}"
                )
                continue
            found.extend(candidates.findings)
        if len(groups) > 1:
            log.info("review_pre_split", groups=len(groups), candidates=len(found))
        return dedupe(found)[:MAX_CANDIDATES]


class ReviewAgent(Agent):
    """Pass two. Tools, because a candidate is confirmed against the file, not the diff."""

    role: ClassVar[str] = "review"
    prompt_file: ClassVar[str] = "review"
    tool_names: ClassVar[list[str]] = ROLE_TOOLS["review"]
    max_iterations: ClassVar[int] = MAX_ITERATIONS

    async def run(
        self,
        provider: LLMProvider,
        ctx: RunContext,
        goal: str,
        candidates: list[ReviewFinding],
        files: list[diffmod.FileDiff],
        hooks: Hooks,
    ) -> tuple[ReviewReport, list[ReviewFinding], RunOutcome]:
        """``(report, dropped, outcome)``. ``blocking`` is recomputed here, not trusted."""
        submit = submit_tool("submit_review", ReviewReport, REVIEW_KEY)
        outcome = await self.run_tools(
            provider,
            ctx,
            verify_message(goal, candidates, files),
            hooks,
            extra_tools=[submit],
            must_call=submit.name,
            rubric=RUBRIC,
        )
        report = ctx.submitted.get(REVIEW_KEY)
        if not isinstance(report, ReviewReport):
            raise AgentError(
                f"reviewer did not submit a report (stop_reason={outcome.stop_reason}, "
                f"turns={outcome.turns})"
            )
        confirmed = dedupe(report.findings)
        kept = {finding_key(f) for f in confirmed}
        dropped = [c for c in candidates if finding_key(c) not in kept]
        # The model filled in `blocking`; the harness decides it. A model that has just
        # written four findings is the last thing that should rule on whether they block.
        final = report.model_copy(
            update={"findings": confirmed, "blocking": is_blocking(confirmed)}
        )
        log.info(
            "review_done",
            candidates=len(candidates),
            confirmed=len(confirmed),
            dropped=len(dropped),
            blocking=final.blocking,
            model_said_blocking=report.blocking,
        )
        return final, dropped, outcome


# Rendered into agents/prompts/review.md at `{rubric}`. Kept beside the code rather than
# only in the prompt file so the severities and docs/review-rubric.md can be compared.
RUBRIC = "\n".join(
    [
        "| Severity | Meaning |",
        "|---|---|",
        "| blocking | wrong behaviour for a plausible input; a security hole; "
        "an acceptance criterion unmet; a test that cannot fail |",
        "| major | likely bug, missing error handling, data loss on an edge case |",
        "| minor | misleading naming, dead code, a missing docstring on a public API |",
        "| nit | formatting or preference |",
    ]
)


def render_findings(findings: list[ReviewFinding]) -> str:
    """The findings as a task description the Coder can work from.

    Grouped by file, because a fix round opens files rather than findings, and a reviewer's
    three notes on one function are one edit.
    """
    by_file: dict[str, list[ReviewFinding]] = {}
    for f in findings:
        by_file.setdefault(f.file, []).append(f)
    out = ["A review of your change raised these. Fix each one, or say in "]
    out[0] += "`notes_for_reviewer` why it is not a problem — a rebuttal is a valid answer."
    for path in sorted(by_file):
        out.append(f"\n## {path}")
        for f in sorted(by_file[path], key=lambda x: x.line):
            out.append(
                f"- **line {f.line}** [{f.severity}] {f.summary}\n"
                f"  fails when: {f.failure_scenario}"
            )
    return "\n".join(out)


def fix_task(findings: list[ReviewFinding], round_n: int, kind: str = "review") -> TaskSpec:
    """A task from the findings, for the Coder to take through CODE and TEST like any other.

    Deliberately an ordinary task: it gets the debug attempts, the test gate and the
    escalation path every task gets. A fix round that could not fail would be a fix round
    worth nothing.
    """
    return TaskSpec(
        id=f"fix-{kind}-{round_n}",
        title=f"Address {kind} findings (round {round_n})",
        description=render_findings(findings),
        depends_on=[],
        files=sorted({f.file for f in findings}),
        acceptance_criteria=[
            "Each listed finding is fixed, or rebutted in notes_for_reviewer with a reason",
            "Existing tests still pass, and new behaviour has a test that would catch it",
        ],
        test_selector="",
    )


def unresolved(findings: list[ReviewFinding]) -> list[str]:
    """Findings as `known_issues` lines, for when the fix budget ran out.

    One line each, carrying the severity and the failure — a pull request that says
    "2 known issues" without saying what they are is worse than one that says nothing.
    """
    return [
        f"[{f.severity}] {f.file}:{f.line} — {f.summary} (fails when: {f.failure_scenario})"
        for f in findings
        if f.severity in ("blocking", "major")
    ]


NO_REASON = "(dropped without a stated reason)"


def rejection_reasons(report: ReviewReport) -> dict[str, str]:
    """`file:line` -> the reason the verification pass gave for throwing a candidate out."""
    return {f"{r.file}:{r.line}": r.reason.strip() for r in report.rejections if r.reason.strip()}


def artifact(
    report: ReviewReport, dropped: list[ReviewFinding], reasons: dict[str, str] | None = None
) -> dict[str, Any]:
    """What gets stored: the report, and everything rejected with why.

    `reasons` used to be a parameter nobody passed, so every dropped candidate was stored
    with `"reason": ""` — and the test asserted only that the *key* was present, which an
    empty string satisfies. "Why was this not reported" had no answer, which is the thing
    this artifact exists to answer.

    A candidate with no matching rejection is marked `NO_REASON` rather than left blank:
    "the model rejected this and explained why" and "the model dropped this without
    saying anything" are different facts about how much the review can be trusted, and a
    blank string made them look identical.
    """
    stated = reasons if reasons is not None else rejection_reasons(report)
    return {
        "report": report.model_dump(mode="json"),
        "dropped": [
            {**c.model_dump(mode="json"), "reason": stated.get(f"{c.file}:{c.line}", NO_REASON)}
            for c in dropped
        ],
        # How many the model bothered to explain, against how many it threw out. A pass
        # that rejects nine candidates and explains none is one a reader should discount.
        "rejections_explained": sum(1 for c in dropped if f"{c.file}:{c.line}" in stated),
    }
