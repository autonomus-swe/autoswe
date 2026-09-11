"""Pure phase transitions. The model never chooses the next phase."""

from __future__ import annotations

from orchestrator.state import Phase, RunState


def transition(s: RunState) -> Phase:
    match s.phase:
        case Phase.SETUP:
            return Phase.ANALYZE
        case Phase.ANALYZE:
            return Phase.PLAN if s.repo else Phase.FAILED
        case Phase.PLAN:
            if s.plan is None:
                return Phase.FAILED
            return Phase.AWAITING_INPUT if s.plan.open_questions else Phase.DECOMPOSE
        case Phase.AWAITING_INPUT:
            # an answer arrived (or the run was cancelled, which the runner handles first)
            return Phase.PLAN
        case Phase.DECOMPOSE:
            return Phase.CODE if s.tasks else Phase.FAILED
        case Phase.CODE:
            return Phase.TEST if s.task_result else Phase.FAILED
        case Phase.TEST:
            report = s.last_test_report
            if report is None or not report.passed:
                return Phase.FAILED  # Phase 3 replaces this with DEBUG / ESCALATE
            more = s.tasks.next_ready() if s.tasks else None
            return Phase.CODE if more else Phase.PR
        case Phase.PR:
            return Phase.DONE if s.pr_url else Phase.FAILED
        case _:
            raise ValueError(f"no transition defined for phase {s.phase}")
