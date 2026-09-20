"""`docs/design-notes.md` states numbers. These are those numbers, read from the code.

Phase 5's checklist asks for three explanations before Phase 6 begins. Writing them down is
the easy half; keeping them true is the half that fails silently, because prose has no way
of breaking when the constant it describes moves.

This project has already shipped two bugs of exactly this shape — a downgrade table
documented as `"decompose"` when the role is `"decomposer"`, and a budget field documented
as `["usd"]` when it is `["budget_usd"]`. Both read perfectly well. Both were wrong, and
both were found by accident rather than by anything failing.

So the document is parsed and its claims are checked against the modules they describe. A
constant that changes without the explanation changing fails here, naming the number and
the file, which is the smallest useful signal: not "the docs are stale" but "this sentence
is now false".

The tests are deliberately about *load-bearing* numbers — the ones a reader would act on —
rather than every figure in the file. Pinning prose to the code too tightly produces a test
that fails whenever anyone rewords a sentence, which trains people to delete it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gateway import caching
from gateway.routing import DOWNGRADE, ROUTES
from repo import repomap

pytestmark = pytest.mark.unit

NOTES = Path(__file__).resolve().parents[2] / "docs" / "design-notes.md"


@pytest.fixture(scope="module")
def notes() -> str:
    return NOTES.read_text()


def test_the_document_exists_and_covers_the_three_things_the_checklist_names(notes: str) -> None:
    """The control for every other test in this file.

    Without it, a missing or truncated document makes every `in` assertion below vacuous in
    the same direction: an empty file contains no wrong numbers either.
    """
    assert len(notes) > 3_000, "the notes are too short to be explaining three designs"

    for heading in ("cache breakpoints go", "never downgrade", "repo map score"):
        assert heading in notes, heading


# ---- 1. breakpoints ----------------------------------------------------------------------


def test_the_prefix_order_is_described_most_stable_first(notes: str) -> None:
    """The ordering claim is the entire design. If the document listed the messages before
    the run block, it would be describing a build that caches nothing."""
    order = [notes.index(part) for part in ("[ tools ]", "role prompt", "run block", "messages")]

    assert order == sorted(order), "the documented prefix is not most-stable-first"


def test_the_documented_tool_result_interval_is_the_code(notes: str) -> None:
    assert "`TOOL_RESULT_EVERY` turns" in notes
    assert caching.TOOL_RESULT_EVERY == 8


def test_the_claim_that_only_a_few_marks_survive_matches_the_constant(notes: str) -> None:
    """The document says `moving_breakpoints` keeps "the two most recent marks"."""
    assert "two most recent marks" in notes
    assert caching.MOVING_BREAKPOINTS == 2
    assert caching.MAX_BREAKPOINTS - caching.SYSTEM_BREAKPOINTS == caching.MOVING_BREAKPOINTS


def test_the_small_prompt_floor_is_named_rather_than_numbered(notes: str) -> None:
    """Named on purpose: the value is a tuning parameter and the reasoning is what matters.
    The test still checks the constant exists and is a plausible floor, so the sentence
    cannot outlive the mechanism it describes."""
    assert "`MIN_CACHEABLE_CHARS`" in notes
    assert caching.MIN_CACHEABLE_CHARS > 0


def test_the_measured_cache_figures_quoted_are_the_ones_recorded(notes: str) -> None:
    """These three numbers are also in `docs/numbers.md` and in the commit that measured
    them. Quoted in two places, they can disagree in two places."""
    for figure in ("5 tokens of 2 678", "2 682"):
        assert figure in notes, figure


# ---- 2. the downgrade ----------------------------------------------------------------------


def test_every_role_the_document_says_downgrades_actually_does(notes: str) -> None:
    """The `decompose`/`decomposer` trap, which this repository has already fallen into
    once: the phase document's own example used a role name that does not exist, so the
    entry would never have matched and the Decomposer would have stayed on the top tier
    while the code looked like it downgraded."""
    claimed = {"planner", "decomposer", "review"}

    assert set(DOWNGRADE) == claimed
    for role in claimed:
        assert f"`{role}`" in notes, role
        assert role in ROUTES, f"{role} is not a real role"


def test_the_two_roles_the_document_protects_are_absent_from_the_table(notes: str) -> None:
    """The load-bearing half. A reader acts on this sentence."""
    assert "`coder`, `debugger`" in notes

    for role in ("coder", "debugger"):
        assert role in ROUTES
        assert role not in DOWNGRADE, f"{role} downgrades, and the notes say it does not"


def test_the_dollar_only_trigger_is_stated(notes: str) -> None:
    """Wall clock is the tempting second trigger and the document explains why it is not
    one. Losing that sentence is how it gets added."""
    assert "**dollar**" in notes
    assert "wall-clock" in notes


# ---- 3. the map score ----------------------------------------------------------------------


def test_the_documented_weights_are_the_ones_used(notes: str) -> None:
    """Read out of the source rather than hardcoded here, so this test cannot drift from
    the code in the same way the document can."""
    source = Path(repomap.__file__).read_text()
    weights = re.search(r"score = ([\d.]+) \* central\[f\] \+ ([\d.]+) \* lexical\[f\]", source)

    assert weights, "the scoring line moved; this test and the notes both need rereading"
    central, lexical = weights.groups()
    assert f"score = {central} * centrality + {lexical} * lexical" in notes
    assert float(central) + float(lexical) == pytest.approx(1.0)


def test_the_test_penalty_in_the_document_is_the_constant(notes: str) -> None:
    assert f"`TEST_PENALTY` ({repomap.TEST_PENALTY})" in notes
    assert 0.0 < repomap.TEST_PENALTY < 1.0, "a penalty above 1 would promote tests"


def test_the_pin_is_documented_as_a_pin_and_is_larger_than_any_score(notes: str) -> None:
    """`+10.0` on a 0..1 scale is a pin, not a boost, and the distinction is the point: a
    boost can be outranked, and the plan named this file."""
    source = Path(repomap.__file__).read_text()

    assert "score += 10.0" in source
    assert "`+10.0`" in notes
    assert "pin and not a boost" in notes


def test_the_calibrated_divisor_is_the_one_in_the_code(notes: str) -> None:
    """The figure that was wrong once, reported as met, and corrected. Worth pinning in
    both directions: the notes explain *why* it is 2.6, and the code is what it is."""
    assert repomap.CHARS_PER_TOKEN == 2.6
    assert f"`CHARS_PER_TOKEN` is {repomap.CHARS_PER_TOKEN}" in notes
    assert "2.69" in notes and "2.90" in notes, "the measurement behind the value is missing"
