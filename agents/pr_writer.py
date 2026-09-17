"""The PR Writer: prose, and only prose.

The one agent in this project whose output a human reads directly, and the one with the
least authority. It writes six strings — title, summary, changes, testing, known issues,
rollback — and every number in the rendered body comes from `repo.pr_body.BodyFacts`
instead. It cannot report that a test passed, cannot decide the pull request is ready, and
cannot soften a finding: `render` copies `known_issues` from the state and ignores the
model's copy.

No tools, deliberately. It is given the facts rather than sent to find them, because a
writer that can read files is a writer that can quote one — and this output is published to
a forge. `run_structured` rather than a submit tool for the same reason: one request, one
object, no loop in which a prompt-injected string could steer anything.

The input is assembled here rather than in the node so that what the writer is told is
reviewable in one place — which is also where it is visible that it is told the *summaries*
of reports and not the reports themselves.
"""

from __future__ import annotations

from typing import ClassVar

from agents.base import Agent
from contracts import PullRequestDescription
from gateway.provider import Hooks, LLMProvider
from observability.logging import get_logger
from repo.pr_body import BodyFacts

log = get_logger(__name__)

# Enough for the reviewer to see what the run did, not so much that the writer starts
# summarising a diff it was not given.
MAX_COMMITS = 30
MAX_TASKS = 20


def _tasks(facts: BodyFacts, results: dict[str, str]) -> str:
    if facts.tasks is None or not facts.tasks.tasks:
        return "(no task graph: the goal was attempted as one task)"
    out = []
    for task in facts.tasks.tasks[:MAX_TASKS]:
        summary = results.get(task.spec.id, "")
        out.append(f"- `{task.spec.id}` [{task.status}] {task.spec.title}")
        if summary:
            out.append(f"    what the coder said it did: {summary}")
    return "\n".join(out)


def _tests(facts: BodyFacts) -> str:
    """The numbers, spelled out, so the writer never has to compute one.

    The excused tests are named rather than counted, because "copy this, do not summarise"
    is only enforceable if the thing to copy is in front of it.
    """
    report = facts.test_report
    if report is None:
        return "No test report was produced. Say so plainly; do not claim tests passed."
    passed = report.total - report.failed - report.errors - report.skipped
    lines = [
        f"Command: {report.command}",
        f"Result: {'PASSED' if report.passed else 'FAILED'} — {passed} passed, "
        f"{report.failed} failed, {report.errors} errors, {report.skipped} skipped, "
        f"of {report.total} in {report.duration_s:.2f}s",
    ]
    if facts.flaky_tests:
        lines.append(f"Excused as flaky ({len(facts.flaky_tests)}): {sorted(facts.flaky_tests)}")
    if facts.preexisting_failures:
        lines.append(
            f"Already failing before this run ({len(facts.preexisting_failures)}): "
            f"{sorted(facts.preexisting_failures)}"
        )
    if facts.flaky_tests or facts.preexisting_failures:
        lines.append("These were NOT made to pass by this change. The `testing` line must say so.")
    return "\n".join(lines)


def _reports(facts: BodyFacts) -> str:
    """Counts and severities only.

    The writer is given the *shape* of the review and the scan rather than their findings.
    A body needs to say "two blocking findings were fixed"; it does not need the finding
    text, and handing over scanner messages derived from an untrusted diff is how prose
    picks up a string somebody planted.
    """
    lines = []
    review = facts.review
    if review is None:
        lines.append("Review: did not run.")
    else:
        counts = {
            sev: sum(1 for f in review.findings if f.severity == sev)
            for sev in ("blocking", "major", "minor", "nit")
        }
        named = ", ".join(f"{n} {sev}" for sev, n in counts.items() if n) or "nothing"
        lines.append(f"Review: {named}. Blocking: {'yes' if review.blocking else 'no'}.")
    security = facts.security
    if security is None:
        lines.append("Security scan: did not run.")
    else:
        counts = {
            sev: sum(1 for f in security.findings if f.severity == sev and not f.false_positive)
            for sev in ("critical", "high", "medium", "low", "info")
        }
        named = ", ".join(f"{n} {sev}" for sev, n in counts.items() if n) or "nothing"
        in_diff = sum(1 for f in security.findings if f.in_diff and not f.false_positive)
        unticked = sorted(k for k, ok in security.checklist.items() if not ok)
        lines.append(
            f"Security scan: {named}; {in_diff} of them inside this change. "
            f"Critical: {'yes' if security.critical else 'no'}."
        )
        if unticked:
            lines.append(f"Security checklist items not satisfied: {unticked}")
    rounds = facts.fix_rounds
    if rounds:
        spent = ", ".join(f"{k}: {v}" for k, v in sorted(rounds.items()))
        lines.append(f"Fix rounds spent ({spent}) — findings were sent back and re-tested.")
    return "\n".join(lines)


def message(facts: BodyFacts, commits: str, results: dict[str, str]) -> str:
    """Everything the writer is given, and nothing else."""
    parts = [
        f"# Goal\n{facts.goal}",
        f"# Approach the plan took\n"
        f"{facts.plan.approach if facts.plan else '(no plan was recorded)'}",
        f"# Tasks\n{_tasks(facts, results)}",
        f"# Commits this run made\n{commits.strip() or '(none)'}",
        f"# Diff\n```\n{facts.diff_stat.strip() or '(no diff)'}\n```",
        f"# Test results\n{_tests(facts)}",
        f"# What the gates said\n{_reports(facts)}",
    ]
    if facts.known_issues:
        parts.append(
            "# Unresolved findings — copy these verbatim into `known_issues`\n"
            + "\n".join(f"- {k}" for k in facts.known_issues)
        )
    else:
        parts.append("# Unresolved findings\nNone. Leave `known_issues` empty.")
    if facts.escalation_reason:
        parts.append(
            f"# This run did not complete\nReason: {facts.escalation_reason}\n"
            "Describe an unfinished change. Do not write a summary that reads as finished."
        )
    return "\n\n".join(parts)


class PRWriterAgent(Agent):
    """One request, one description. No tools, and no authority over any number."""

    role: ClassVar[str] = "pr_writer"
    prompt_file: ClassVar[str] = "pr_writer"

    async def run(
        self,
        provider: LLMProvider,
        facts: BodyFacts,
        commits: str,
        results: dict[str, str],
        hooks: Hooks | None = None,
    ) -> PullRequestDescription:
        """The description. Raises on failure — `pr_node` has a fallback for that."""
        description = await self.run_structured(
            provider,
            message(facts, commits, results),
            PullRequestDescription,
            hooks=hooks,
        )
        log.info(
            "pr_description_written",
            title_len=len(description.title),
            changes=len(description.changes),
            # The writer is asked to copy these; the renderer uses the state's copy either
            # way. Logged so a writer that quietly drops them is visible rather than only
            # harmless.
            known_issues_written=len(description.known_issues),
            known_issues_actual=len(facts.known_issues),
        )
        return description


def fallback(facts: BodyFacts) -> PullRequestDescription:
    """A description written by the harness, for when the writer fails.

    A run that did the work, passed its tests and pushed a branch must not lose its pull
    request because a prose model was unavailable. Every field here is a fact the harness
    already had, which is the argument for how little the writer is trusted with: the body
    is still true without it, only duller.
    """
    report = facts.test_report
    if report is None:
        testing = "No test report was produced."
    else:
        passed = report.total - report.failed - report.errors - report.skipped
        testing = (
            f"`{report.command}` — {'passed' if report.passed else 'FAILED'}: {passed} passed, "
            f"{report.failed} failed, of {report.total}."
        )
    tasks = facts.tasks.tasks if facts.tasks else []
    return PullRequestDescription(
        title=facts.goal[:70],
        summary=(
            "_The PR Writer did not run, so this description is generated from the run's "
            "records rather than written. The reports below are unaffected._\n\n"
            f"Goal: {facts.goal}"
        ),
        changes=[f"{t.spec.id}: {t.spec.title} [{t.status}]" for t in tasks] or ["See the diff."],
        testing=testing,
        known_issues=list(facts.known_issues),
        rollback="Revert the merge commit.",
    )
