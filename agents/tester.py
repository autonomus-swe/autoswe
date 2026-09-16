"""The Tester: deciding which failures are the agent's to fix.

Almost everything here is deterministic and host-side, because "did the tests pass" is
not a judgement call and must not become one. A model is consulted for exactly one thing —
putting a name to a failure the parser could not classify — and even then its answer is
advice for the Debugger rather than an input to control flow. See ``classify_unknown``.

Three filters decide what a report means:

**Baseline.** Failures already present on the base branch are not the agent's. They are
recorded in SETUP and removed from the full-suite report here.

**Selector.** The task names the tests it is responsible for. Those are never filtered:
they are the specification the Coder was given, so a pre-existing failure among them is
the job, not an inheritance.

**Flaky.** A test that fails in the suite and passes alone is either flaky or
order-dependent, and one re-run cannot tell which. Treating it as passed is a deliberate
trade, which is why the ids are kept and the pull request lists them.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import ClassVar

from agents.base import Agent
from contracts import FailureKind, TestFailure, TestReport, Triage, Usage
from gateway.provider import LLMProvider, Request
from observability.logging import get_logger
from repo import source_context as src
from tools import test_report as tr
from tools.test_report import report_signature

log = get_logger(__name__)

# Per failure. The Debugger already gets every frame as a one-liner; the windows are for
# reading, and three is where a prompt stops being evidence and starts being a file dump.
MAX_CONTEXT_FRAMES = 3
CONTEXT_RADIUS = 10
# One broken commit can fail hundreds of tests. Triage is a courtesy, not a duty.
MAX_TRIAGE = 10
SHORT_MESSAGE_CHARS = 12


def is_collection(test_id: str) -> bool:
    """Did this failure happen while collecting rather than running?

    A collected pytest test always has ``::`` in its node id; a collection error names
    only the file. The runner counts the two differently, so removing a failure has to
    know which total it came out of.
    """
    return "::" not in test_id


def _without(
    report: TestReport, drop: Callable[[TestFailure], bool]
) -> tuple[TestReport, TestReport]:
    """Remove the failures ``drop`` selects. Returns ``(kept, dropped)`` reports.

    ``passed`` is recomputed rather than carried over — the whole point is that a report
    can become a pass once the failures nobody owns are taken out of it. So is the
    signature, because "the same failure as last time" has to mean the same *remaining*
    failures.
    """
    dropped = [f for f in report.failures if drop(f)]
    if not dropped:
        return report, report.model_copy(update={"failures": [], "failed": 0, "errors": 0})
    kept = [f for f in report.failures if not drop(f)]
    lost_errors = sum(1 for f in dropped if is_collection(f.test_id))
    failed = max(0, report.failed - (len(dropped) - lost_errors))
    errors = max(0, report.errors - lost_errors)
    return (
        report.model_copy(
            update={
                "failures": kept,
                "failed": failed,
                "errors": errors,
                # total == 0 is never a pass: a suite that collected nothing proved nothing
                "passed": report.total > 0 and failed == 0 and errors == 0,
                "signature": report_signature(kept),
            }
        ),
        report.model_copy(
            update={
                "failures": dropped,
                "failed": len(dropped) - lost_errors,
                "errors": lost_errors,
            }
        ),
    )


def filter_baseline(report: TestReport, baseline: Iterable[str]) -> tuple[TestReport, list[str]]:
    """Drop failures that were already failing before the run started.

    Keyed on signature, not test id: the same test can fail a new way, and that way is the
    agent's. An empty signature never matches, so a failure the parser could not
    fingerprint is kept rather than silently forgiven.
    """
    known = {s for s in baseline if s}
    kept, dropped = _without(report, lambda f: f.signature in known)
    return kept, [f.test_id for f in dropped.failures]


def without_flaky(report: TestReport, flaky: Iterable[str]) -> tuple[TestReport, list[str]]:
    """Drop failures in tests that passed when re-run on their own. Keyed on test id."""
    ids = set(flaky)
    kept, dropped = _without(report, lambda f: f.test_id in ids)
    return kept, [f.test_id for f in dropped.failures]


# ---- the selector: what this task claimed -------------------------------------------


def understood(selector: str) -> bool:
    """Can we reason about what this selector covers?

    A selector carrying flags (``-k``, ``-m``) selects by expression, and working out
    whether a given test id was included would mean reimplementing pytest's matcher. When
    the answer is no, callers treat coverage as unknown rather than guessing.
    """
    return not any(token.startswith("-") for token in shlex.split(selector or ""))


def covers(selector: str, test_id: str) -> bool:
    """Does ``selector`` include ``test_id``?

    Prefix matching with a boundary, so ``tests/unit`` covers ``tests/unit/test_a.py::x``
    and ``tests/test_a.py`` covers ``tests/test_a.py::x``, but ``tests/test_a`` covers
    neither — a half-name is a coincidence, not a selection.
    """
    for token in shlex.split(selector or ""):
        token = token.rstrip("/")
        if token == test_id:
            return True
        if test_id.startswith(token) and test_id[len(token) :][:1] in ("/", ":"):
            return True
    return False


def outside(report: TestReport, selector: str) -> list[str]:
    """Failing tests this task never claimed, in report order and without duplicates.

    Empty when the selector is missing or unparseable. With no selector the task claimed
    nothing, so every failure is potentially the agent's and a re-run proves nothing — the
    conservative answer is to fail the run rather than re-roll it.
    """
    if not selector.strip() or not understood(selector):
        return []
    seen: set[str] = set()
    ids = []
    for f in report.failures:
        if f.test_id not in seen and not covers(selector, f.test_id):
            seen.add(f.test_id)
            ids.append(f.test_id)
    return ids


def selector_for(test_ids: Iterable[str]) -> str:
    """A pytest selector for exactly these tests.

    Quoted, because a parametrised node id contains brackets and often spaces
    (``test_pages[a b]``), and an unquoted one becomes two arguments and a glob.
    """
    return " ".join(shlex.quote(t) for t in test_ids)


# ---- what the Debugger reads ---------------------------------------------------------


def source_context(
    report: TestReport, worktree: Path, radius: int = CONTEXT_RADIUS
) -> dict[str, str]:
    """Numbered source windows per failing test, innermost repository frame first.

    Innermost, unlike the signature, which keys on the outermost in-repo frame. The two
    want different things: a fingerprint wants the frame that moves least between edits,
    and a reader wants the line that actually raised.
    """
    out: dict[str, str] = {}
    for f in report.failures:
        blocks = []
        for frame in [fr for fr in f.frames if fr.in_repo][::-1][:MAX_CONTEXT_FRAMES]:
            body = src.around(frame.file, frame.line, worktree, radius)
            if body:
                blocks.append(f"{tr.where(frame)}\n{body}")
        if blocks:
            out[f.test_id] = "\n\n".join(blocks)
    return out


def needs_triage(failures: Iterable[TestFailure]) -> list[TestFailure]:
    """Failures the parser could not say anything useful about.

    ``exception`` is the fallback class, so an ``exception`` with no message means the
    runner told us a test failed and nothing else. That is the only case worth a model
    call; everything else already has a class the parser stands behind.
    """
    out = []
    for f in failures:
        message = f.message.strip()
        if f.kind == "exception" and (
            not message or message == "unknown failure" or len(message) < SHORT_MESSAGE_CHARS
        ):
            out.append(f)
    return out[:MAX_TRIAGE]


def render_for_triage(failures: list[TestFailure]) -> str:
    lines = []
    for f in failures:
        lines.append(f"## {f.test_id}\nmessage: {f.message or '(empty)'}")
        for frame in f.frames:
            where = "repo" if frame.in_repo else "library"
            lines.append(f"  [{where}] {frame.file}:{frame.line} in {frame.function}: {frame.code}")
    return "\n".join(lines)


class TesterAgent(Agent):
    """Only ``triage``. Everything else about a test report is decided above, in Python."""

    role: ClassVar[str] = "tester"
    prompt_file: ClassVar[str] = "tester"

    async def classify_unknown(
        self, provider: LLMProvider, failures: list[TestFailure]
    ) -> tuple[dict[str, FailureKind], Usage]:
        """Put a name to failures the parser could not classify.

        The result is *advice*, delivered to the Debugger as a note. It deliberately does
        not overwrite ``TestFailure.kind``, because ``kind`` feeds the signature and the
        signature is how the transition table notices that an attempt changed nothing. A
        label that can come back differently on an identical report would make that
        comparison flap, and three debug attempts would each look like progress.

        Never raises. A run must not fail because an optional courtesy did.
        """
        if not failures:
            return {}, Usage()
        req = Request(
            role=self.role,
            system=self.system_prompt(),
            messages=[{"role": "user", "content": render_for_triage(failures)}],
            max_tokens=2000,
        )
        try:
            triage, usage = await provider.parse(req, Triage)
        except Exception as e:  # advice is not worth a failed run
            log.warning("triage_failed", error=f"{type(e).__name__}: {e}")
            return {}, Usage()
        wanted = {f.test_id for f in failures}
        return {c.test_id: c.kind for c in triage.classifications if c.test_id in wanted}, usage


def render_triage(kinds: dict[str, FailureKind]) -> dict[str, str]:
    """Triage results as per-test notes, for merging into the Debugger's context."""
    return {
        test_id: f"triage: the runner gave no usable message; this looks like a {kind} failure"
        for test_id, kind in kinds.items()
    }
