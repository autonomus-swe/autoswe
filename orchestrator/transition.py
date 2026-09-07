"""Pure phase transitions. v1 covers SETUP -> CODE -> TEST -> PR -> DONE."""

from __future__ import annotations

from orchestrator.state import Phase, RunState


def transition(s: RunState) -> Phase:
    match s.phase:
        case Phase.SETUP:
            return Phase.CODE
        case Phase.CODE:
            return Phase.TEST if s.task_result else Phase.FAILED
        case Phase.TEST:
            report = s.last_test_report
            return Phase.PR if report and report.passed else Phase.FAILED
        case Phase.PR:
            return Phase.DONE if s.pr_url else Phase.FAILED
        case _:
            raise ValueError(f"no transition defined for phase {s.phase}")
