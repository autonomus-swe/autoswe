"""Rendering the pull request body.

Two inputs, and the split between them is the whole design. `PullRequestDescription` is
what a model wrote — prose, and nothing else. `BodyFacts` is what the harness knows:
counts, severities, statuses, rounds, money, time. Every number in the rendered body comes
from the second, so a model that felt good about its work cannot report that a test passed.

That is the same rule as `ReviewReport.blocking` and `SecurityReport.critical`, applied to
the one artifact a human actually reads.

## What is deliberately *not* rendered

**`SecurityFinding.rationale`.** It is the model's free text, written after it opened files
with `read_file`, so it is the one field that can quote a source line — and a source line
can hold a credential. The rationale stays in the `security` artifact, behind the API key,
and never reaches a forge. A run cannot reach this renderer at all with a secret in its own
commits, because `gitleaks_gate` refuses the push first; what this guards is the narrower
case of a model quoting from a file the run did not touch.

**Anything unescaped.** The diff is untrusted, so a finding's message can contain whatever
an attacker put in a comment. `_cell` strips newlines and escapes pipes, because a `|` in a
message silently breaks a Markdown table and a newline ends it — and a body that renders
differently from what the scanner said is worse than no table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from contracts import (
    DebugHypothesis,
    ImplementationPlan,
    PullRequestDescription,
    ReviewReport,
    SecurityReport,
    TaskGraph,
    TestReport,
)

# GitHub truncates long titles in notification email and in the PR list. 70 characters is
# the phase document's limit; enforced here rather than trusted to the prompt, because a
# model asked for "under 70 characters" produces 74 often enough to matter.
MAX_TITLE = 70
MAX_CELL = 160
# Past this, a table stops being readable and the artifact is the right place to look.
MAX_ROWS = 25

# Explicit, because sorting by the severity *string* is wrong and wrong quietly. The
# review severities happen to sort correctly by accident (blocking < major < minor < nit),
# but the security ones do not: alphabetically `info` lands between `high` and `low`, so a
# table sorted that way would put advisory rows above real ones.
REVIEW_RANK = {"blocking": 0, "major": 1, "minor": 2, "nit": 3}
SECURITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class BodyFacts:
    """What the harness knows. Every number in the body comes from here, not from a model.

    A dataclass rather than `RunState` because `repo/` sits below `orchestrator/` and must
    not import it — and because listing the facts explicitly is what makes it obvious when
    a new section is quietly sourcing a number from the model instead.
    """

    run_id: str
    goal: str
    diff_stat: str
    plan: ImplementationPlan | None = None
    tasks: TaskGraph | None = None
    review: ReviewReport | None = None
    security: SecurityReport | None = None
    test_report: TestReport | None = None
    flaky_tests: set[str] = field(default_factory=set)
    preexisting_failures: set[str] = field(default_factory=set)
    known_issues: list[str] = field(default_factory=list)
    fix_rounds: dict[str, int] = field(default_factory=dict)
    debug_attempts: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    escalation_reason: str | None = None
    # task id -> what the Debugger believed, in the order it believed it. Only populated
    # for a run that escalated: on a run that succeeded these are noise, and on one that
    # did not they are the most useful thing in the body — somebody picking the work up
    # needs to know which four theories have already been tried and failed.
    hypotheses: dict[str, list[DebugHypothesis]] = field(default_factory=dict)


def _cell(text: str, limit: int = MAX_CELL) -> str:
    """One Markdown table cell from untrusted text.

    A newline ends a table and an unescaped pipe adds a column, so a finding message
    written by a model that just read an attacker-controlled diff could rearrange the body
    around itself. Both are neutralised rather than trusted.
    """
    flat = " ".join(text.split())
    flat = flat.replace("|", "\\|")
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _per_task(facts: BodyFacts) -> str:
    """Cost per *completed* task, when there were any.

    The number to compare two runs with, and the reason a cheaper model is not
    automatically a saving: a run that spent half as much and finished one task instead
    of three cost more per unit of work, and a total alone hides that entirely.

    Per task *done*, not per task planned. A run that planned four and finished one spent
    all of it on the one, and dividing by four would flatter it.

    Derived here rather than stored on `runs`: it is two numbers already recorded, and a
    stored ratio is a number that can disagree with the rows it came from.
    """
    done = sum(1 for t in facts.tasks.tasks if t.status == "done") if facts.tasks else 0
    if not done or facts.cost_usd <= 0:
        return ""
    return f" (${facts.cost_usd / done:.2f}/task)"


def title(description: PullRequestDescription, facts: BodyFacts) -> str:
    """The title, capped host-side and prefixed when the run did not complete.

    `[WIP]` is not decoration: the title is what lands in notification email and in a
    reviewer's list view, and a run that escalated must not read there as a finished one.
    """
    text = " ".join(description.title.split()) or facts.goal
    if facts.escalation_reason:
        return f"[WIP] {text}"[:MAX_TITLE]
    return text[:MAX_TITLE]


def is_draft(facts: BodyFacts) -> bool:
    """Whether to open as a draft, decided from the run rather than from the description.

    A draft is this project's way of saying "a human has to look at this before it can
    merge", so every condition here is a way the run fell short of its own standard:

    - `known_issues` — a gate blocked, the fix budget ran out, and the finding is unresolved
    - `escalation_reason` — the run did not finish the work it set out to do
    - no test report, or a failing one — nothing shows the change works
    - excused tests — "all tests pass" is true only because some were not counted

    The model's own text has no vote. It wrote the prose; this reads the state.
    """
    report = facts.test_report
    return bool(
        facts.known_issues
        or facts.escalation_reason
        or report is None
        or not report.passed
        or facts.flaky_tests
        or facts.preexisting_failures
    )


def _review_section(review: ReviewReport | None, rounds: int) -> str:
    if review is None:
        return _details("Review report", "The review did not run.")
    if not review.findings:
        return _details("Review report (nothing found)", "No findings.")
    rows = ["| severity | where | finding |", "|---|---|---|"]
    ordered = sorted(
        review.findings, key=lambda x: (REVIEW_RANK.get(x.severity, 9), x.file, x.line)
    )
    for f in ordered[:MAX_ROWS]:
        rows.append(f"| {f.severity} | `{_cell(f.file, 60)}:{f.line}` | {_cell(f.summary)} |")
    extra = len(review.findings) - MAX_ROWS
    if extra > 0:
        rows.append(f"\n…and {extra} more in the `review` artifact.")
    spent = f", {rounds} fix round{'s' if rounds != 1 else ''}" if rounds else ""
    return _details(f"Review report ({len(review.findings)} findings{spent})", "\n".join(rows))


def _security_section(security: SecurityReport | None, rounds: int) -> str:
    """The findings, with `rationale` withheld — see the module docstring for why."""
    if security is None:
        return _details("Security report", "The scan did not run.")
    lines = []
    if security.checklist:
        ticks = " · ".join(
            f"{'✓' if ok else '✗'} {key}" for key, ok in sorted(security.checklist.items())
        )
        lines.append(f"{ticks}\n")
    real = [f for f in security.findings if not f.false_positive and f.rule != "scan-failed"]
    mine = [f for f in real if f.in_diff]
    inherited = [f for f in real if not f.in_diff]
    for label, group in (("In this change", mine), ("Pre-existing", inherited)):
        if not group:
            continue
        lines.append(f"\n**{label}**\n")
        lines.append("| severity | check | where | finding |")
        lines.append("|---|---|---|---|")
        ordered = sorted(group, key=lambda x: (SECURITY_RANK.get(x.severity, 9), x.tool, x.file))
        for f in ordered[:MAX_ROWS]:
            where = f"`{_cell(f.file, 60)}:{f.line}`" if f.file else "—"
            # Tool *and* rule: "bandit" alone says which program complained, and the
            # rule id is the part a reviewer can look up, suppress, or search for.
            lines.append(
                f"| {f.severity} | {_cell(f.tool, 20)} `{_cell(f.rule, 60)}` | {where} "
                f"| {_cell(f.message)} |"
            )
        if len(group) > MAX_ROWS:
            lines.append(f"\n…and {len(group) - MAX_ROWS} more in the `security` artifact.")
    # A scanner that did not run is worth saying: it means a class of problem went
    # unlooked-for, which reads nothing like "no problems found".
    failed = [f for f in security.findings if f.rule == "scan-failed"]
    if failed:
        lines.append("\n**Did not run**\n")
        lines.extend(f"- {_cell(f.message, 200)}" for f in failed)
    if not lines:
        lines.append("No findings.")
    spent = f", {rounds} fix round{'s' if rounds != 1 else ''}" if rounds else ""
    heading = f"Security report (critical: {'yes' if security.critical else 'no'}{spent})"
    return _details(heading, "\n".join(lines))


def _tests_section(facts: BodyFacts) -> str:
    report = facts.test_report
    if report is None:
        return _details("Test results", "No test report was produced.")
    passed = report.total - report.failed - report.errors - report.skipped
    lines = [
        f"`{_cell(report.command, 120)}`\n",
        f"- {passed} passed, {report.failed} failed, {report.errors} errors, "
        f"{report.skipped} skipped, of {report.total} in {report.duration_s:.2f}s",
    ]
    # Excused tests are the reason this section exists. A reviewer told "all tests pass"
    # deserves to know which ones were not made to pass here.
    for label, names in (
        ("excused as flaky", sorted(facts.flaky_tests)),
        ("failing before this run", sorted(facts.preexisting_failures)),
    ):
        if names:
            lines.append(f"\n**{len(names)} {label}**\n")
            lines.extend(f"- `{_cell(n, 120)}`" for n in names[:MAX_ROWS])
    return _details("Test results", "\n".join(lines))


def _plan_section(plan: ImplementationPlan | None, tasks: TaskGraph | None) -> str:
    if plan is None:
        return ""
    lines = [plan.approach, ""]
    if plan.risks:
        lines.append("**Risks the plan named**\n")
        lines.extend(f"- {_cell(r, 200)}" for r in plan.risks)
        lines.append("")
    if tasks and tasks.tasks:
        lines.append("**Tasks**\n")
        lines.extend(
            f"- `{t.spec.id}` [{t.status}] {_cell(t.spec.title, 100)}" for t in tasks.tasks
        )
    return _details("Plan", "\n".join(lines))


def _hypotheses_section(facts: BodyFacts) -> str:
    """What the Debugger tried, per task, for whoever picks this up.

    Not collapsed behind a summary count like the other sections: on an escalated run this
    is the point of the pull request. A reader's first question is "what has already been
    ruled out", and the answer should not require a click.
    """
    if not facts.hypotheses:
        return ""
    lines = []
    for task_id in sorted(facts.hypotheses):
        tried = facts.hypotheses[task_id]
        if not tried:
            continue
        lines.append(f"\n**`{task_id}`** — {len(tried)} attempt{'s' if len(tried) != 1 else ''}\n")
        for i, h in enumerate(tried, 1):
            lines.append(
                f"{i}. [{h.failure_class}, confidence {h.confidence:.2f}] "
                f"{_cell(h.root_cause, 300)}\n"
                f"   tried: {_cell(h.plan, 300)}"
            )
    if not lines:
        return ""
    return "## What was already tried\n" + "\n".join(lines)


def _details(summary: str, body: str) -> str:
    """Collapsed by default. A body that opens to four screens of tables gets skimmed, and
    the point of these sections is that somebody reads the two lines above them."""
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def render(description: PullRequestDescription, facts: BodyFacts) -> str:
    """The body. Prose from `description`, every number from `facts`."""
    review_rounds = facts.fix_rounds.get("review", 0)
    security_rounds = facts.fix_rounds.get("security", 0)

    parts: list[str] = []
    if facts.escalation_reason:
        # First line, before anything a model wrote. A reader must not have to reach the
        # footer to learn the run gave up.
        parts.append(
            f"> **This run did not complete: {_cell(facts.escalation_reason, 200)}**\n> \n"
            "> The commits below are what it managed before stopping. Treat the summary as "
            "a description of an unfinished change."
        )
    parts.append(description.summary.strip())

    parts.append("## Changes\n")
    changes = [c for c in description.changes if c.strip()] or ["(none described)"]
    parts.append("\n".join(f"- {c.strip()}" for c in changes))
    parts.append(f"\n```\n{facts.diff_stat.strip() or '(no diff)'}\n```")

    parts.append("## Testing\n")
    parts.append(description.testing.strip() or "(not described)")

    hypotheses = _hypotheses_section(facts)
    if hypotheses:
        parts.append(hypotheses)

    sections = [
        _plan_section(facts.plan, facts.tasks),
        _review_section(facts.review, review_rounds),
        _security_section(facts.security, security_rounds),
        _tests_section(facts),
    ]
    parts.extend(s for s in sections if s)

    parts.append("## Known issues\n")
    # Copied from the state, not from the description: the model is asked to repeat these
    # verbatim, and this is where that stops being a matter of trust.
    if facts.known_issues:
        parts.append("\n".join(f"- {_cell(k, 300)}" for k in facts.known_issues))
    else:
        parts.append("None.")

    parts.append("## Rollback\n")
    parts.append(description.rollback.strip() or "Revert the merge commit.")

    task_count = len(facts.tasks.tasks) if facts.tasks else 0
    footer = " · ".join(
        [
            f"autoswe run `{facts.run_id}`",
            f"{task_count} task{'s' if task_count != 1 else ''}",
            f"{facts.debug_attempts} debug attempt{'s' if facts.debug_attempts != 1 else ''}",
            f"{review_rounds + security_rounds} fix round"
            f"{'s' if review_rounds + security_rounds != 1 else ''}",
            f"${facts.cost_usd:.2f}{_per_task(facts)}",
            _elapsed(facts.elapsed_s),
        ]
    )
    parts.append(f"---\n{footer}")
    return "\n\n".join(parts) + "\n"
