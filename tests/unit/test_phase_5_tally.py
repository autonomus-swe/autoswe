"""The summary count matches the boxes it summarises.

`docs/design-notes.md` is pinned to the code by `test_design_notes.py` because a constant
can move out from under prose. This is the other half of the same problem and it bit
harder: a *summary* is the one part of a document nobody re-derives. It is written once,
from a count taken at the time, and then carried forward through every later edit that
changes the thing it counts.

Which is exactly what happened. The Phase 5 summary read "Eight of ten met" while the
section above it showed seven ticked boxes — I miscounted when ticking the caching
criterion, and the wrong number survived several revisions because every reader, including
me, took it as given rather than counting.

The fix is to count. This test is deliberately about arithmetic and nothing else: it makes
no claim about whether a box *deserves* its tick, which is what the criterion's own
evidence is for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PHASE_5 = Path(__file__).resolve().parents[2] / "docs" / "PHASE-5-scale-and-cost.md"
WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


@pytest.fixture(scope="module")
def doc() -> str:
    return PHASE_5.read_text()


def exit_criteria(doc: str) -> list[str]:
    """Just the §1 checkboxes. §7 has its own list and counting both would flatter."""
    section = doc[doc.index("## 1. Exit criteria") : doc.index("## 2. Architecture slice")]
    return re.findall(r"^- \[([ x~])\]", section, re.M)


def test_the_section_still_has_ten_criteria(doc: str) -> None:
    """The control. If the parse found none, every count below would agree at zero."""
    boxes = exit_criteria(doc)

    assert len(boxes) == 10, f"found {len(boxes)} criteria, not ten"


def test_the_summary_count_is_the_number_of_ticked_boxes(doc: str) -> None:
    """The one that failed when this was written."""
    ticked = sum(1 for b in exit_criteria(doc) if b == "x")
    claim = re.search(r"\*\*(\w+) of ten ticked", doc)

    assert claim, "the summary no longer states a tally in the form this checks"
    assert WORDS[claim.group(1).lower()] == ticked, (
        f"the summary says {claim.group(1)}, the boxes say {ticked}"
    )


def test_a_criterion_is_either_done_open_or_explicitly_partial(doc: str) -> None:
    """`[~]` is allowed and meaningful — it carried the caching criterion honestly for a
    while — but only those three states, so the count above cannot miss one."""
    assert set(exit_criteria(doc)) <= {"x", " ", "~"}
