"""Score a run's pull-request description against a rubric, with a model.

The PR body is the one artifact a human reads before a human reads the diff, and it is the
one thing no test can check: a description can be fluent, confident and wrong about the
change it accompanies. So it is scored — against the diff, by a model that is shown both.

## Four criteria, one to five, each with the reason

`accuracy` is the one that matters and the only one a reader cannot verify cheaply: does
the description match the diff, or does it describe a change that was planned rather than
made. The other three — testing, known issues, rollback — are about whether the sections
are load-bearing or decorative.

Every score carries a `why`. A number on its own cannot be argued with, and a rubric whose
output cannot be argued with is a rubric nobody will correct.

## Not the Anthropic Batches API

The plan scores these with Opus through `client.messages.batches.create` at half price.
This build has no Anthropic provider and is not getting one, so the rubric goes through
whatever `LLM_PROVIDER` is configured, one call per run. That is more expensive per token
and a great deal cheaper in dependencies; on a free or local model it is free.

**A judge and a worker on the same model is a weak judge.** It is not independent, and it
shares the blind spots of the thing it is grading. Pass `--model` something else where you
have something else. `docs/evals.md` says so beside the numbers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from gateway.provider import LLMProvider, Request
from observability.logging import get_logger

log = get_logger(__name__)

MAX_DIFF_CHARS = 30_000
RUBRIC = """\
You are grading a pull-request description against the diff it describes.

Score each criterion 1-5 and give a one-sentence reason.

- accuracy: does the description match what the diff actually does? A description of a
  change that was planned but not made scores 1, however well written it is.
- testing: does it say what was tested and how, specifically enough to check?
- known_issues: does it name what is still wrong or untested? "None" when the diff clearly
  has rough edges scores 1.
- rollback: is the rollback concrete — a command, a revert, a flag — rather than "revert
  the commit"?

Judge only what is in front of you. Do not reward length.
"""


class Score(BaseModel):
    """One criterion. The reason is required, because a bare number cannot be argued with."""

    score: int = Field(ge=1, le=5)
    why: str = Field(min_length=1, max_length=400)


class Verdict(BaseModel):
    accuracy: Score
    testing: Score
    known_issues: Score
    rollback: Score

    @property
    def mean(self) -> float:
        parts = [self.accuracy, self.testing, self.known_issues, self.rollback]
        return round(sum(p.score for p in parts) / len(parts), 2)


@dataclass(frozen=True)
class Judgement:
    run_id: str
    verdict: Verdict | None
    error: str | None = None

    def row(self) -> dict[str, Any]:
        if self.verdict is None:
            return {"run_id": self.run_id, "error": self.error}
        v = self.verdict
        return {
            "run_id": self.run_id,
            "mean": v.mean,
            "accuracy": v.accuracy.score,
            "testing": v.testing.score,
            "known_issues": v.known_issues.score,
            "rollback": v.rollback.score,
            "why": {
                "accuracy": v.accuracy.why,
                "testing": v.testing.why,
                "known_issues": v.known_issues.why,
                "rollback": v.rollback.why,
            },
        }


def prompt(pr_body: str, diff: str) -> str:
    """The two artifacts, fenced and labelled, with the diff truncated at the tail.

    The head of a diff is the files it touched; the tail is whatever was appended last. For
    "does this description match this change", the beginning is the part that answers it,
    so the cut goes at the end and says so rather than silently shortening the evidence.
    """
    clipped = diff[:MAX_DIFF_CHARS]
    note = "" if len(diff) <= MAX_DIFF_CHARS else f"\n…[diff truncated at {MAX_DIFF_CHARS} chars]"
    return (
        f"## Pull-request description\n\n{pr_body.strip() or '(empty)'}\n\n"
        f"## Diff\n\n```diff\n{clipped}{note}\n```\n"
    )


async def judge_one(provider: LLMProvider, run_id: str, pr_body: str, diff: str) -> Judgement:
    """One run's description. A refusal or an unparseable answer is a row, not an exception.

    A judge that raises stops a batch of thirty on its worst input, which is the input most
    worth having scored.
    """
    request = Request(
        role="review",
        system=RUBRIC,
        messages=[{"role": "user", "content": prompt(pr_body, diff)}],
    )
    try:
        verdict, _usage = await provider.parse(request, Verdict)
    except Exception as e:
        log.warning("judge_failed", run_id=run_id, error=f"{type(e).__name__}: {e}")
        return Judgement(run_id=run_id, verdict=None, error=f"{type(e).__name__}: {e}")
    return Judgement(run_id=run_id, verdict=verdict)


async def judge_many(
    provider: LLMProvider, items: list[tuple[str, str, str]], *, concurrency: int = 3
) -> list[Judgement]:
    """`(run_id, pr_body, diff)` triples, scored with bounded concurrency.

    Keyed by run id throughout and never by position: the plan's warning about the Batches
    API — "results keyed by `custom_id`, never by position" — is the same hazard here the
    moment one call fails and the list shortens under you.
    """
    limit = asyncio.Semaphore(max(1, concurrency))

    async def one(item: tuple[str, str, str]) -> Judgement:
        async with limit:
            return await judge_one(provider, *item)

    return list(await asyncio.gather(*(one(i) for i in items)))
