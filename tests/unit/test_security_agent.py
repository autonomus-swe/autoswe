"""The Security agent: what can gate, what cannot, and who decides.

Almost every test here is about *not trusting a claim*. A security gate is only worth
having if it cannot be talked out of firing, so: `critical` is recomputed from the
severities, a finding the model never mentions is kept rather than cleared, an inherited
vulnerability cannot block, and a checklist box cannot block on its own.

The two asymmetries are the ones worth reading carefully, because each is a deliberate
choice about which way to be wrong:

- **An unverified finding gates.** Running out of verification budget must not quietly open
  the gate, so a finding nobody got to still counts.
- **A gitleaks finding stops the push even untagged.** Every other gate needs `in_diff`.
  This one does not, because a secret gets worse by being transmitted and a line-mapping
  miss is the wrong thing to bet a credential on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from agents import security
from contracts import SecurityChecklist, SecurityFinding, SecurityReport, Usage
from core.errors import AgentError
from gateway.provider import LLMProvider
from repo import diff as d
from repo.github import gitleaks_gate
from tests.fakes import FakeProviderBase, make_ctx

pytestmark = pytest.mark.unit

ALL_TICKED = dict.fromkeys(security.CHECKLIST_KEYS, True)


def finding(
    tool: str = "semgrep",
    rule: str = "python.lang.security.audit.subprocess-shell-true",
    file: str = "src/a.py",
    line: int = 10,
    severity: str = "high",
    in_diff: bool = True,
    false_positive: bool = False,
    verified: bool = True,
    message: str = "subprocess call with shell=True",
) -> SecurityFinding:
    return SecurityFinding(
        tool=tool,
        rule=rule,
        file=file,
        line=line,
        severity=cast("Any", severity),
        message=message,
        verified_by_llm=verified,
        false_positive=false_positive,
        rationale="reached from the request handler" if verified else "",
        in_diff=in_diff,
    )


def file_diff(path: str = "src/a.py", body: str = "+x = 1\n") -> d.FileDiff:
    return d.FileDiff(path=path, added=1, removed=0, text=f"diff --git a/{path} b/{path}\n{body}")


class _NullHooks:
    async def before_tool(self, name: str, input: dict[str, Any]) -> str | None:
        return None

    async def after_tool(self, name: str, input: dict[str, Any], result: Any, ms: int) -> Any:
        return result

    async def on_message(self, message: Any, usage: Usage, latency_ms: int) -> None:
        return None


class Provider(FakeProviderBase):
    """Scripted: submits the report it was built with, and nothing else."""

    def __init__(self, report: SecurityReport | None) -> None:
        self.report = report

    async def run_tools(self, req: Any, tools: Any, ctx: Any, hooks: Any) -> Any:
        if self.report is not None:
            ctx.submitted[security.SECURITY_KEY] = self.report
        return type("O", (), {"stop_reason": "end_turn", "turns": 2, "usage": Usage()})()

    async def parse(self, req: Any, output: Any) -> Any:
        raise AssertionError("the security pass does not use parse")


async def run_agent(
    tmp_path: Path,
    submitted: SecurityReport | None,
    scanner_findings: list[SecurityFinding] | None = None,
    files: list[d.FileDiff] | None = None,
) -> SecurityReport:
    ctx = make_ctx(tmp_path, role="security")
    report, _outcome = await security.SecurityAgent().run(
        cast("LLMProvider", Provider(submitted)),
        ctx,
        "add a search endpoint",
        scanner_findings or [],
        files if files is not None else [file_diff()],
        cast("Any", _NullHooks()),
    )
    return report


# ---- the gate is the harness's, not the model's --------------------------------------


@pytest.mark.parametrize(
    ("severity", "gates"),
    [
        ("critical", True),
        ("high", True),
        ("medium", False),
        ("low", False),
        ("info", False),
    ],
)
def test_which_severities_can_gate(severity: str, gates: bool) -> None:
    """`high` gates, and that is not an accident.

    Of the four scanners only gitleaks ever emits `critical` — bandit and semgrep top out at
    `high`, and pip-audit has no severity field so every dependency finding is `high`. A
    gate on `critical` alone would leave three of the four scanners unable to stop anything,
    which is a scanner suite for decoration.
    """
    assert security.is_critical([finding(severity=severity)]) is gates


def test_the_models_own_critical_flag_is_ignored(tmp_path: Path) -> None:
    """Both directions, because a model has an incentive in both."""
    understated = SecurityReport(
        findings=[finding(severity="critical")], critical=False, checklist=ALL_TICKED
    )
    assert security.is_critical(understated.findings) is True

    overstated = SecurityReport(
        findings=[finding(severity="low")], critical=True, checklist=ALL_TICKED
    )
    assert security.is_critical(overstated.findings) is False


async def test_the_recomputed_flag_is_what_reaches_the_state(tmp_path: Path) -> None:
    """Not just the predicate — the report the node stores has to carry the harness's answer."""
    claimed = SecurityReport(
        findings=[finding(severity="critical")], critical=False, checklist=ALL_TICKED
    )

    report = await run_agent(tmp_path, claimed, [finding(severity="critical")])

    assert report.critical is True, "the model said False and a critical finding is present"


def test_a_vulnerability_the_run_inherited_cannot_block_it() -> None:
    """Worth listing, not the agent's to answer for — and decided from the diff, not from
    anything a model says."""
    assert security.is_critical([finding(severity="critical", in_diff=False)]) is False


def test_a_rejected_finding_cannot_block() -> None:
    assert security.is_critical([finding(severity="critical", false_positive=True)]) is False


def test_an_unverified_finding_still_blocks() -> None:
    """The asymmetry that makes the gate fail closed.

    A scanner result nobody got to is a question nobody answered. If running out of
    verification budget cleared findings, the cheapest way past this gate would be to
    produce enough noise to exhaust it.
    """
    assert security.is_critical([finding(severity="high", verified=False)]) is True


def test_a_scanner_that_did_not_run_cannot_block() -> None:
    """`run_all` reports a failure as an `info` finding so silence does not read as clean.
    It says something was not looked for, which is not the same as something found."""
    from tools.scanners import failure

    assert security.is_critical([failure("semgrep", "exit 137: killed")]) is False


async def test_a_report_that_never_arrives_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """A security pass that quietly returned nothing would be indistinguishable from one
    that found nothing."""
    with pytest.raises(AgentError, match="did not submit"):
        await run_agent(tmp_path, None)


# ---- omission clears nothing -----------------------------------------------------------


def test_a_finding_the_model_does_not_mention_is_kept() -> None:
    """Silence is not a rejection. Otherwise the cheapest way to clear a finding would be
    to not talk about it, and the whole verification pass would be optional."""
    scanner = finding(rule="B602", verified=False)

    merged = security.merge([scanner], [])

    assert merged == [scanner], "kept exactly as it was, still unverified"
    assert security.is_critical(merged) is True


def test_a_finding_the_model_rejects_keeps_the_rejection() -> None:
    scanner = finding(rule="B602", verified=False)
    rejected = scanner.model_copy(
        update={"false_positive": True, "verified_by_llm": True, "rationale": "test fixture"}
    )

    (merged,) = security.merge([scanner], [rejected])

    assert merged.false_positive and merged.rationale == "test fixture"
    assert security.is_critical([merged]) is False


def test_a_finding_the_model_adds_itself_is_kept() -> None:
    """Missing authorisation and PII in a log line are what no scanner finds, so this is
    the most valuable thing the pass produces."""
    scanner = finding(rule="B602")
    its_own = finding(tool="llm", rule="missing-authorization", line=44)

    merged = security.merge([scanner], [scanner, its_own])

    assert len(merged) == 2
    assert any(f.tool == "llm" for f in merged)


async def test_findings_past_the_verification_budget_survive(tmp_path: Path) -> None:
    """A noisy rule pack must not be able to push real findings off the report."""
    many = [finding(rule=f"R{i}", line=i, severity="medium") for i in range(60)]
    worst = finding(rule="B602", line=999, severity="high")
    submitted = SecurityReport(findings=[], critical=False, checklist=ALL_TICKED)

    report = await run_agent(tmp_path, submitted, [*many, worst])

    assert len([f for f in report.findings if f.tool != "checklist"]) == 61, "none were dropped"
    assert report.critical is True, "and the high one still gates"


def test_triage_puts_the_worst_and_nearest_first() -> None:
    """Verification opens files, so what goes first is what matters most."""
    inherited_critical = finding(severity="critical", in_diff=False, line=1)
    mine_medium = finding(severity="medium", line=2)
    mine_critical = finding(severity="critical", line=3)

    to_verify, _ = security.triage([inherited_critical, mine_medium, mine_critical])

    assert [f.line for f in to_verify] == [3, 2, 1]


# ---- applicability, computed from the diff ---------------------------------------------


def test_the_checklist_keys_match_the_contract_and_all_have_a_signal() -> None:
    """The drift guard. A key added to `SecurityChecklist` without a decision about what
    puts it in play would otherwise default to always-applicable, silently."""
    assert set(security.CHECKLIST_KEYS) == set(SecurityChecklist.model_fields)
    assert set(security.SIGNALS) == set(security.CHECKLIST_KEYS)


def test_a_diff_with_no_sql_puts_sql_out_of_play() -> None:
    applies = security.applicable([file_diff(body="+def add(a, b):\n+    return a + b\n")])

    assert applies["parameterized_sql"] is False
    assert applies["csrf_protected"] is False


def test_a_diff_with_a_raw_query_puts_sql_in_play() -> None:
    applies = security.applicable(
        [file_diff(body='+cur.execute("SELECT * FROM users WHERE id = " + uid)\n')]
    )

    assert applies["parameterized_sql"] is True


def test_secrets_are_always_in_play() -> None:
    """There is no diff that puts a credential out of scope: any line of any file can
    hold one."""
    applies = security.applicable([file_diff(body="+# a comment\n")])

    assert applies["no_secrets"] is True


def test_only_added_lines_count() -> None:
    """Deleting the last raw query is the opposite of a reason to ask whether queries are
    parameterised."""
    removed_sql = file_diff(body='-cur.execute("SELECT 1")\n+return 1\n')

    assert security.applicable([removed_sql])["parameterized_sql"] is False


def test_the_diff_header_is_not_read_as_a_change() -> None:
    """`+++ b/path` is furniture. A file named `login.py` would otherwise put every
    auth-related key in play by its name alone."""
    header_only = d.FileDiff(
        path="src/login.py",
        added=0,
        removed=0,
        text="diff --git a/src/login.py b/src/login.py\n+++ b/src/login.py\n",
    )

    assert security.applicable([header_only])["rate_limited_auth"] is False


# ---- the checklist reports, and never gates --------------------------------------------


def test_an_unticked_key_the_diff_touches_is_reported_and_cannot_gate() -> None:
    """The promise this design has to keep. A box is not a failure — gating needs a finding
    that names the hole — but an unticked box must still be impossible to skip quietly."""
    checklist = {**ALL_TICKED, "parameterized_sql": False}
    files = [file_diff(body='+cur.execute("SELECT 1")\n')]

    out = security.checklist_findings(checklist, [], security.applicable(files))

    (f,) = out
    assert f.rule == "parameterized_sql" and f.tool == "checklist"
    assert f.severity == "medium", "visible in the report and in the pull request"
    assert security.is_critical(out) is False, "and unable to gate, whatever its severity"
    assert "no finding says what is wrong" in f.message


def test_an_unticked_key_the_diff_does_not_touch_is_only_information() -> None:
    """The applicability fix. Without it every run blocks on the boxes it could not
    meaningfully tick, and a gate that always fires is a gate nobody keeps."""
    checklist = {**ALL_TICKED, "parameterized_sql": False}
    files = [file_diff(body="+def add(a, b):\n+    return a + b\n")]

    (f,) = security.checklist_findings(checklist, [], security.applicable(files))

    assert f.severity == "info"
    assert "Nothing this change added appears to touch this area" in f.message


def test_an_unticked_key_with_a_finding_behind_it_adds_nothing() -> None:
    """The finding is already on the report carrying its own severity, and that is the
    thing that gates. Reporting the box as well would double-count one problem."""
    checklist = {**ALL_TICKED, "parameterized_sql": False}
    explained = [finding(rule="parameterized_sql", severity="high")]

    out = security.checklist_findings(checklist, explained, dict.fromkeys(security.SIGNALS, True))

    assert out == []


def test_an_unanswered_key_is_not_the_same_as_a_ticked_one() -> None:
    """A model that returns ten of eleven keys has skipped one, and the report says so."""
    checklist = {k: True for k in security.CHECKLIST_KEYS if k != "no_pii_in_logs"}

    (f,) = security.checklist_findings(checklist, [], dict.fromkeys(security.SIGNALS, True))

    assert f.rule == "no_pii_in_logs" and "was not answered" in f.message


def test_a_fully_ticked_checklist_adds_nothing() -> None:
    assert security.checklist_findings(ALL_TICKED, [], dict.fromkeys(security.SIGNALS, True)) == []


async def test_the_checklist_reaches_the_report(tmp_path: Path) -> None:
    """End to end through the agent, because the wiring is where this could be lost."""
    submitted = SecurityReport(
        findings=[], critical=False, checklist={**ALL_TICKED, "no_secrets": False}
    )

    report = await run_agent(tmp_path, submitted)

    boxes = [f for f in report.findings if f.tool == "checklist"]
    assert [f.rule for f in boxes] == ["no_secrets"]
    assert report.critical is False, "a box on its own does not block a run"


# ---- the push gate ---------------------------------------------------------------------


def test_a_committed_secret_stops_the_push() -> None:
    leak = finding(tool="gitleaks", rule="generic-api-key", severity="critical", file="cfg.py")

    assert gitleaks_gate([leak]) == [leak]


def test_a_secret_stops_the_push_even_untagged() -> None:
    """The deliberate asymmetry. Every other gate needs `in_diff`; this one does not,
    because gitleaks is already scoped to this run's commits with `--log-opts`, so anything
    it reports was committed here — and a line-mapping miss is the wrong thing to bet a
    live credential on."""
    leak = finding(tool="gitleaks", rule="generic-api-key", severity="critical", in_diff=False)

    assert gitleaks_gate([leak]) == [leak]


def test_a_gitleaks_that_could_not_run_does_not_stop_the_push() -> None:
    """A scanner that did not run has found nothing. Refusing every push on a broken
    scanner would make the tool impossible to keep installed."""
    from tools.scanners import failure

    assert gitleaks_gate([failure("gitleaks", "not on PATH")]) == []


def test_another_tools_finding_does_not_stop_the_push() -> None:
    """bandit and semgrep block the *run* through the fix loop, and a draft pull request
    that names them is a better outcome than no pull request."""
    assert gitleaks_gate([finding(severity="critical")]) == []


# ---- what goes back to the Coder -------------------------------------------------------


def test_only_the_gating_findings_are_sent_back() -> None:
    """A fix round costs a pass through coding and testing, so it is spent on what is
    actually holding the run up."""
    gating = finding(rule="B602", severity="high")
    advisory = finding(rule="B105", severity="low", line=20)
    inherited = finding(rule="B303", severity="high", line=30, in_diff=False)

    assert security.gating([gating, advisory, inherited]) == [gating]


def test_the_fix_task_tells_the_coder_not_to_paste_the_secret() -> None:
    """The obvious wrong fix for a committed credential is to move it somewhere else in the
    same branch, which does not help at all."""
    leak = finding(tool="gitleaks", rule="generic-api-key", severity="critical")

    spec = security.fix_task([leak], 1)

    assert spec.id == "fix-security-1"
    assert "Never paste a secret" in spec.description
    assert any("No secret is written" in c for c in spec.acceptance_criteria)


def test_unresolved_lines_name_the_finding_rather_than_counting_it() -> None:
    """ "2 known issues" without saying what they are is worse than saying nothing."""
    lines = security.unresolved([finding(rule="B602", severity="high"), finding(severity="low")])

    assert len(lines) == 1
    assert "B602" in lines[0] and "src/a.py:10" in lines[0]


def test_the_artifact_records_what_the_diff_was_judged_to_put_in_play() -> None:
    """Applicability is a heuristic over a regex, so the answer it gave is worth storing —
    "why was this only info" needs to be answerable after the run."""
    report = SecurityReport(
        findings=[finding(severity="high")], critical=True, checklist=ALL_TICKED
    )
    files = [file_diff(body='+cur.execute("SELECT 1")\n')]

    stored = security.artifact(report, security.applicable(files))

    assert stored["applicable"]["parameterized_sql"] is True
    assert stored["applicable"]["xss_escaped"] is False
    assert len(stored["gating"]) == 1
