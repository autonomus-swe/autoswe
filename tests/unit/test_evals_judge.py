"""Scoring a pull-request description against its diff.

The judge is a model grading a model, which makes its own failure modes the thing to test:
a judge that raises stops a batch on its worst input, and a judge that silently drops one
result shifts every score after it onto the wrong run.
"""

from __future__ import annotations

from typing import Any

import pytest

from contracts import Usage
from evals import judge
from gateway.provider import Request

pytestmark = pytest.mark.unit


def verdict(**scores: int) -> judge.Verdict:
    base = {"accuracy": 5, "testing": 4, "known_issues": 3, "rollback": 2, **scores}
    return judge.Verdict(
        **{name: judge.Score(score=value, why="because") for name, value in base.items()}
    )


class FakeProvider:
    """Returns a scripted verdict per call, or raises for the ones set to raise."""

    provider_name = "fake"
    model = "fake"

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def model_for(self, tier: str | None) -> str:
        return self.model

    async def parse(self, req: Request, output: type) -> tuple[Any, Usage]:
        self.prompts.append(str(req.messages[0]["content"]))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, Usage()

    async def run_tools(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - unused here
        raise NotImplementedError


async def test_a_verdict_carries_four_scores_and_a_mean() -> None:
    provider = FakeProvider([verdict()])
    result = await judge.judge_one(provider, "r1", "body", "diff")
    assert result.verdict is not None
    assert result.verdict.mean == 3.5
    row = result.row()
    assert row["run_id"] == "r1" and row["accuracy"] == 5 and row["mean"] == 3.5


async def test_every_score_carries_its_reason() -> None:
    """A number on its own cannot be argued with, and a rubric nobody can argue with is a
    rubric nobody will correct."""
    provider = FakeProvider([verdict()])
    row = (await judge.judge_one(provider, "r1", "body", "diff")).row()
    assert set(row["why"]) == {"accuracy", "testing", "known_issues", "rollback"}
    assert all(row["why"].values())


async def test_a_score_outside_one_to_five_is_refused_by_the_contract() -> None:
    """Guided decoding will produce a 0 or an 11 eventually; the contract is what stops it
    becoming a mean nobody can reproduce."""
    with pytest.raises(ValueError):
        judge.Score(score=0, why="x")
    with pytest.raises(ValueError):
        judge.Score(score=6, why="x")


async def test_a_refusal_becomes_a_row_rather_than_stopping_the_batch() -> None:
    """The input a judge chokes on is the one most worth having scored, and a raise takes
    the other twenty-nine with it."""
    provider = FakeProvider([RuntimeError("the model refused")])
    result = await judge.judge_one(provider, "r1", "body", "diff")
    assert result.verdict is None
    assert result.error is not None and "refused" in result.error
    assert result.row() == {"run_id": "r1", "error": result.error}


async def test_results_stay_attached_to_their_run_when_one_fails() -> None:
    """Keyed by run id throughout, never by position. The plan's warning about the Batches
    API — "results keyed by `custom_id`, never by position" — is the same hazard the moment
    one call fails and a list shortens under you."""
    provider = FakeProvider([verdict(accuracy=5), RuntimeError("boom"), verdict(accuracy=1)])
    results = await judge.judge_many(
        provider, [("a", "b1", "d1"), ("b", "b2", "d2"), ("c", "b3", "d3")], concurrency=1
    )
    by_run = {r.run_id: r for r in results}
    assert set(by_run) == {"a", "b", "c"}
    assert by_run["a"].verdict is not None and by_run["a"].verdict.accuracy.score == 5
    assert by_run["b"].verdict is None
    assert by_run["c"].verdict is not None and by_run["c"].verdict.accuracy.score == 1


def test_the_prompt_shows_the_description_and_the_diff() -> None:
    text = judge.prompt("the description", "--- a/x\n+++ b/x\n")
    assert "the description" in text and "--- a/x" in text
    assert "```diff" in text


def test_an_empty_description_is_shown_as_empty_rather_than_omitted() -> None:
    """A run that wrote no PR body should score 1 on accuracy, which it cannot do if the
    judge is shown a prompt that simply has no description section."""
    assert "(empty)" in judge.prompt("   ", "diff")


def test_a_long_diff_is_cut_at_the_end_and_says_so() -> None:
    """The head of a diff is the files it touched, which is what answers "does this
    description match this change". The cut goes at the tail, and it is announced rather
    than silently shortening the evidence."""
    text = judge.prompt("body", "HEAD-MARKER\n" + ("x" * judge.MAX_DIFF_CHARS) + "TAIL-MARKER")
    assert "HEAD-MARKER" in text
    assert "TAIL-MARKER" not in text
    assert "truncated" in text


def test_the_rubric_names_the_failure_it_exists_to_catch() -> None:
    """A description of a change that was planned but not made is the one a reader cannot
    check cheaply, and the one a fluent model produces most readily."""
    assert "planned but not made" in judge.RUBRIC
    assert "Do not reward length" in judge.RUBRIC
