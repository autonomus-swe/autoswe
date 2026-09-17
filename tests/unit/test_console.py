"""The run console's phase rail, checked against the phase enum it draws.

The console has no build step and no JavaScript test runner, which is a deliberate trade —
but it left one class of bug unguarded, and that class has now bitten twice. `renderRail`
finds the run's phase with `indexOf`, so a phase missing from both lists yields `-1`: no
chip is marked "now", none is marked "past", and the rail goes inert exactly while the run
is doing something worth watching.

It happened first for DEBUG and ESCALATE, which was fixed by adding `OFF_RAIL`. It then
happened again for REVIEW, which was added to the state machine without being added here
and so blanked the rail from the moment a run first reached it. The fix for a bug that
recurs is not a third careful edit; it is this test, which reads the two lists out of the
source and asserts the enum is covered.

Parsing JavaScript with a regex is ugly and would be the wrong tool for anything larger.
Here the alternative is a test runner and a dependency for two array literals, and the
regex fails loudly — an unparseable file raises rather than passing vacuously.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orchestrator.state import Phase

pytestmark = pytest.mark.unit

APP_JS = Path(__file__).resolve().parents[2] / "api" / "static" / "app.js"

# A status rather than a stage. A terminal run reports `failed` — including a cancelled
# one, which has no phase of its own — and `renderRail` marks the last stage it actually
# reached instead of drawing a chip for it.
STATUSES = {Phase.FAILED}


def source() -> str:
    text = APP_JS.read_text()
    assert text, f"{APP_JS} is empty"
    return text


def rail_phases() -> list[str]:
    match = re.search(r"const PHASES = (\[.*?\]);", source(), re.S)
    assert match, "could not find the PHASES literal — the rail cannot be checked"
    return list(json.loads(match.group(1)))


def off_rail_phases() -> dict[str, str]:
    match = re.search(r"const OFF_RAIL = \{(.*?)\};", source(), re.S)
    assert match, "could not find the OFF_RAIL literal"
    return dict(re.findall(r"(\w+):\s*\"(\w+)\"", match.group(1)))


def test_every_phase_the_machine_can_report_has_a_chip() -> None:
    """The test that would have caught REVIEW, and will catch the next one."""
    drawn = set(rail_phases()) | set(off_rail_phases())

    missing = sorted(p.value for p in Phase if p not in STATUSES and p.value not in drawn)

    assert not missing, (
        f"{missing} would set `indexOf` to -1 and blank the whole rail while the run is in "
        "that phase. Add a stage to PHASES, or an excursion to OFF_RAIL."
    )


def test_the_rail_draws_nothing_the_machine_cannot_report() -> None:
    """The other direction: a chip for a phase that no longer exists is a lie about the
    pipeline, and the rail is the first thing anybody looks at."""
    known = {p.value for p in Phase}

    assert [p for p in rail_phases() if p not in known] == []
    assert [p for p in off_rail_phases() if p not in known] == []


def test_an_excursion_is_anchored_to_a_stage_that_is_on_the_rail() -> None:
    """`renderRail` splices the excursion in after its anchor with `PHASES.indexOf(anchor)`.
    An anchor that is not on the rail returns -1 and splices it in at the front, which draws
    the run as being before SETUP."""
    stages = rail_phases()

    for phase, anchor in off_rail_phases().items():
        assert anchor in stages, f"{phase} is anchored to {anchor}, which is not a stage"


def test_the_stages_are_in_the_order_the_machine_visits_them() -> None:
    """Pinned because the rail is read left to right as the order of the pipeline. Phase 4
    put SECURITY between REVIEW and PR, and a rail that disagrees with the transition table
    is worse than no rail."""
    stages = rail_phases()

    for earlier, later in [
        ("setup", "analyze"),
        ("analyze", "plan"),
        ("plan", "decompose"),
        ("decompose", "code"),
        ("code", "test"),
        ("test", "review"),
        ("review", "security"),
        ("security", "pr"),
        ("pr", "done"),
    ]:
        assert stages.index(earlier) < stages.index(later), f"{earlier} should precede {later}"
