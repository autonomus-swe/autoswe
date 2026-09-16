"""Pure phase transitions. The model never chooses the next phase.

v3 adds the verification loop: a failing test goes to DEBUG while attempts remain, and to
ESCALATE when they do not. Two guards run before the phase machine, because cancellation
and an exhausted budget outrank whatever the run was about to do.

This function may set bookkeeping on ``s`` — ``strategy``,
``previous_failure_signature``, ``escalation_reason`` — but it performs no I/O and reads
nothing except ``s``. That is what keeps it table-testable, and the table is the closest
thing this project has to a specification of its own behaviour.
"""

from __future__ import annotations

from orchestrator.state import Phase, RunState

MAX_DEBUG_ATTEMPTS = 3


def transition(s: RunState) -> Phase:
    # A human asked to stop. Nothing else matters, including a half-finished task.
    if s.cancelled:
        return Phase.FAILED

    # Budgets are checked here rather than inside each node so that no node can spend
    # past a limit by forgetting to look. ESCALATE decides what to do about it.
    if s.budget.exceeded(s.usage, s.elapsed_s(), cost_measurable=s.cost_measurable):
        s.escalation_reason = s.budget.reason(
            s.usage, s.elapsed_s(), cost_measurable=s.cost_measurable
        )
        return Phase.ESCALATE

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
            # An answer arrived. Where it resumes depends on what asked: the Planner's
            # open questions go back to PLAN, an escalation's answer goes to DEBUG.
            return s.resume_phase or Phase.PLAN
        case Phase.DECOMPOSE:
            return Phase.CODE if s.tasks else Phase.FAILED
        case Phase.CODE:
            if s.current_task_id and s.current_task_id in s.task_results:
                return Phase.TEST
            # The Coder produced nothing. That is a failed attempt, not a dead run —
            # ESCALATE counts it and sends it back if there is budget for another.
            s.escalation_reason = "coder_no_result"
            return Phase.ESCALATE
        case Phase.TEST:
            return _after_test(s)
        case Phase.DEBUG:
            return Phase.TEST
        case Phase.ESCALATE:
            # escalate_node has already decided: CODE, AWAITING_INPUT, or FAILED.
            return s.resume_phase or Phase.FAILED
        case Phase.REVIEW:
            # A waiting fix task *is* the grant: `review_node` owns the round budget and
            # appends the task when it decides there is one to spend. Asking about the task
            # rather than re-counting the rounds is what keeps the two from disagreeing.
            if s.review and s.review.blocking and s.tasks and s.tasks.next_ready():
                return Phase.CODE
            # Blocking with no task waiting means the budget is gone; the findings are on
            # `known_issues` and the pull request will carry them and be a draft.
            return Phase.PR
        case Phase.PR:
            # The v3 sketch in the phase doc returns DONE unconditionally here. Keeping
            # the check from v2: a run that reached PR without producing a URL has not
            # succeeded, and DONE would be a claim the run cannot support.
            return Phase.DONE if s.pr_url else Phase.FAILED
    raise ValueError(f"no transition defined for phase {s.phase}")


def _after_test(s: RunState) -> Phase:
    report = s.last_test_report
    if report is None:
        s.escalation_reason = "no_test_report"
        return Phase.ESCALATE

    if report.passed:
        # A clean run clears the debug memory: the next task's first failure is its own
        # first failure, not a continuation of this one.
        s.previous_failure_signature = None
        s.strategy = None
        # A fix task was granted by whichever gate asked for it, and its tests have just
        # passed: go back to that gate rather than on to the next one. Checked before
        # `next_ready` because a fix round is finished when its own task is, not when the
        # graph is — and popped as it is read, so one grant buys one hop.
        if s.return_to is not None:
            back, s.return_to = s.return_to, None
            return back
        if s.tasks and s.tasks.next_ready():
            return Phase.CODE
        # Every task done means the change is final, so it gets reviewed before it is
        # pushed. Phase 4 puts SECURITY between REVIEW and PR; until then REVIEW is the
        # last gate, and a run with nothing to review records an empty report and moves on.
        return Phase.REVIEW

    if s.attempts.get(s.current_task_id or "", 0) >= MAX_DEBUG_ATTEMPTS:
        s.escalation_reason = "debug_attempts_exhausted"
        return Phase.ESCALATE

    # The same signature twice means the last hypothesis changed nothing that mattered.
    # Saying so is the whole point of the signature: without it the Debugger would form
    # the same theory again, three times, and call it three attempts.
    s.strategy = "alternative" if report.signature == s.previous_failure_signature else None
    s.previous_failure_signature = report.signature
    return Phase.DEBUG
