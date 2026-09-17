"""The Security agent: scanner output, checked against the code, with the gate in code.

The shape mirrors the Reviewer, and for the same reason — enumeration and scepticism reward
opposite instincts. Here the cheap enumerating pass is not a model at all: four scanners
produce findings mechanically, and this agent is the expensive pass that opens the files
they point at and decides `verified` or `false_positive` with a reason.

Three rules hold it together, and all three are about not letting a claim stand in for
evidence.

**`critical` is recomputed from the severities.** `SecurityReport.critical` is a field the
model fills in. The harness reads the severities and decides, exactly as it does for
`ReviewReport.blocking`. The model judges findings; the code judges the gate.

**A finding the model does not mention does not vanish.** Omission is not a rejection. Any
finding that goes out for verification and does not come back is kept, unverified, and
still counts — so the way to clear a finding is to reject it with a reason, in writing.

**The checklist never gates by itself.** Eleven fixed questions (README §9) make the model
look at eleven specific things, and an unticked box becomes a visible finding so that
quietly skipping one is not possible. But a box is not a failure: gating needs a finding
that names the hole. Whether a box even applies is computed from the diff rather than
asked, because a model that has just written the code is not the right judge of whether
its own SQL handling was in scope.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from agents.base import Agent
from agents.submit import submit_tool
from contracts import SecurityFinding, SecurityReport, TaskSpec
from core.errors import AgentError
from gateway.provider import Hooks, LLMProvider, RunOutcome
from observability.logging import get_logger
from repo import diff as diffmod
from tools.base import RunContext
from tools.registry import ROLE_TOOLS

log = get_logger(__name__)

SECURITY_KEY = "security"
MAX_ITERATIONS = 40
# Scanners are not budgeted the way a model is: a noisy rule pack can produce hundreds of
# findings on one file. Past this count the rest are kept unverified rather than dropped —
# see `triage`, and note that unverified findings still gate.
MAX_TO_VERIFY = 40

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# The eleven checklist keys, in README §9 order. Listed here rather than derived from
# `SecurityChecklist.model_fields` so that a key added to the contract without a thought
# about what makes it applicable shows up as a failing test rather than as a box that is
# silently always in scope.
CHECKLIST_KEYS = (
    "no_secrets",
    "inputs_validated",
    "parameterized_sql",
    "xss_escaped",
    "csrf_protected",
    "auth_on_endpoints",
    "rate_limited_auth",
    "no_stack_traces",
    "env_validated",
    "no_pii_in_cache",
    "no_pii_in_logs",
)

# What has to appear in the lines a run *added* for a checklist key to be in play.
#
# `None` means always applicable. These are regexes over a diff, so they are a rough
# instrument, and the direction of the error is chosen deliberately: over-matching costs a
# `medium` finding nobody needed, under-matching costs an `info` instead of a `medium`.
# Neither one opens or closes the gate, because the checklist does not gate — that is what
# makes it safe to decide applicability with a regex at all.
SIGNALS: dict[str, re.Pattern[str] | None] = {
    # Any line of any file can hold a key. There is no diff that puts this out of scope.
    "no_secrets": None,
    "inputs_validated": re.compile(
        r"@\w+\.(get|post|put|patch|delete)\b|\brequest\.|\bBody\(|\bQuery\(|\bForm\(|"
        r"\bargparse\b|\binput\(|\bjson\.loads\b"
    ),
    "parameterized_sql": re.compile(
        r"\bexecute(many)?\(|\btext\(|\bcursor\b|\braw\(|\bSELECT\b|\bINSERT\b|\bUPDATE\b|"
        r"\bDELETE\b|\bFROM\b\s+\w",
        re.IGNORECASE,
    ),
    "xss_escaped": re.compile(
        r"render_template|\bMarkup\(|innerHTML|dangerouslySetInnerHTML|<script|"
        r"HTMLResponse|\.html\b|\|\s*safe\b"
    ),
    "csrf_protected": re.compile(r"@\w+\.(post|put|patch|delete)\b|\bcsrf\b|set_cookie", re.I),
    "auth_on_endpoints": re.compile(
        r"@\w+\.(get|post|put|patch|delete)\b|\bDepends\(|authoriz|authenticat|\bcurrent_user\b",
        re.IGNORECASE,
    ),
    "rate_limited_auth": re.compile(
        r"\blogin\b|\bsign_?in\b|\bpassword\b|\botp\b|\btoken\b|\bcredential", re.IGNORECASE
    ),
    "no_stack_traces": re.compile(
        r"\bexcept\b|\btraceback\b|HTTPException|JSONResponse|\braise\b.*\bstr\(e\)"
    ),
    "env_validated": re.compile(r"os\.environ|getenv|BaseSettings|\bSettings\(|\bdotenv\b"),
    "no_pii_in_cache": re.compile(r"\bredis\b|\bcache\b|\bsetex\(|\bmemcache|\bsession\[", re.I),
    "no_pii_in_logs": re.compile(
        r"\blog(ger)?\.(debug|info|warning|error|exception|critical)\b|\bprint\("
    ),
}


def added_text(files: list[diffmod.FileDiff]) -> str:
    """The lines a run added, without the diff's own furniture.

    Applicability is about what the change *introduced*, so a removed line that mentioned
    SQL does not put SQL in scope — deleting the last raw query is the opposite of a reason
    to ask whether queries are parameterised.
    """
    out = []
    for f in files:
        for line in f.text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                out.append(line[1:])
    return "\n".join(out)


def applicable(files: list[diffmod.FileDiff]) -> dict[str, bool]:
    """Which checklist keys this diff puts in play, decided from the diff.

    Asked of the code rather than of the model on purpose. A model that has just written a
    change is the last thing that should rule on whether its own SQL handling was in scope,
    and "not applicable" is the cheapest way for any reviewer to make a question go away.
    """
    text = added_text(files)
    return {key: True if sig is None else bool(sig.search(text)) for key, sig in SIGNALS.items()}


def finding_key(f: SecurityFinding) -> tuple[str, str, str, int]:
    """Identity for merging the model's answers back onto what the scanners reported."""
    return (f.tool, f.rule, f.file, f.line)


def is_critical(findings: list[SecurityFinding]) -> bool:
    """The gate, in code. Never read from the model's own boolean.

    Three conditions, each load-bearing:

    - **`critical` or `high`.** Not `critical` alone. Of the four scanners only gitleaks
      ever emits `critical`; bandit and semgrep top out at `high` and pip-audit is always
      `high`. A gate on `critical` alone would make three of the four scanners decorative.
    - **`in_diff`.** A vulnerability the agent inherited is worth listing and is not its to
      answer for, and the distinction comes from the diff rather than from anything a model
      says.
    - **not `false_positive`.** The only way to clear a finding is to reject it with a
      reason. An unverified finding still gates: a scanner result nobody got to is a
      question nobody answered, and failing closed is the whole point of a security gate.
    """
    return any(
        f.severity in ("critical", "high") and f.in_diff and not f.false_positive for f in findings
    )


def triage(findings: list[SecurityFinding]) -> tuple[list[SecurityFinding], list[SecurityFinding]]:
    """``(to_verify, unverified)`` — worst and nearest first.

    A noisy rule pack can produce hundreds of findings on one file, and verification opens
    files. What is left over is kept rather than dropped, and because an unverified finding
    still gates, running out of verification budget cannot quietly open the gate — it can
    only leave a finding on the report with nobody's reasoning attached to it.
    """
    ordered = sorted(
        findings,
        key=lambda f: (not f.in_diff, SEVERITY_RANK.get(f.severity, 9), f.tool, f.file, f.line),
    )
    return ordered[:MAX_TO_VERIFY], ordered[MAX_TO_VERIFY:]


def render_scanner_findings(findings: list[SecurityFinding]) -> str:
    """The scanner output, as the model sees it."""
    if not findings:
        return "(no scanner produced a finding. Read the diff yourself.)"
    out = []
    for i, f in enumerate(findings, 1):
        where = f"{f.file}:{f.line}" if f.file else "(no file)"
        mine = "in this change" if f.in_diff else "pre-existing — not this run's to fix"
        out.append(f"\n## {i}. [{f.severity}] {f.tool} {f.rule} — {where} ({mine})\n{f.message}")
    return "\n".join(out)


def render_checklist(applies: dict[str, bool]) -> str:
    """The eleven questions, marked with the ones this diff puts in play."""
    out = []
    for key in CHECKLIST_KEYS:
        mark = "**in play**" if applies.get(key, True) else "probably not in play"
        out.append(f"- `{key}` — {mark}")
    return "\n".join(out)


def verify_message(
    goal: str,
    findings: list[SecurityFinding],
    files: list[diffmod.FileDiff],
    applies: dict[str, bool],
) -> str:
    """Input to the agent: what was asked, what changed, what the scanners said."""
    return "\n\n".join(
        [
            f"# Goal\n{goal}",
            f"# Change\n{diffmod.diff_stat(files)}",
            f"# Scanner findings\n{render_scanner_findings(findings)}",
            "# Checklist\nAnswer all eleven. The marks are computed from the lines this "
            f"change added, and are a hint about where to look, not an instruction:\n"
            f"{render_checklist(applies)}",
            "Open the files. Reject what is wrong with a reason, keep what is real, add "
            "anything the scanners missed, then call submit_security once.",
        ]
    )


def merge(
    original: list[SecurityFinding], returned: list[SecurityFinding]
) -> list[SecurityFinding]:
    """The model's answers, with anything it failed to mention kept as it was.

    Omission is not rejection. A model that quietly drops a finding it could not explain
    would otherwise clear the gate by silence — so the only thing that clears a finding is
    a `false_positive` with a reason, in writing, which survives into the artifact.
    """
    answered = {finding_key(f): f for f in returned}
    out = [answered.pop(finding_key(f), f) for f in original]
    # Findings the model added that no scanner reported: its own reading of the code.
    out.extend(answered.values())
    return out


def checklist_findings(
    checklist: dict[str, bool], findings: list[SecurityFinding], applies: dict[str, bool]
) -> list[SecurityFinding]:
    """An unticked box, as a finding — and never as a gate.

    The severity says how much the box is worth, and neither value can gate:

    - **`medium`** when the key is in play and nothing in the report explains it. The model
      said a security property does not hold and named no specific problem, which is worth
      a human's eye and is not something to block a run over.
    - **`info`** when the diff does not put the key in play at all.

    A box that *is* explained — the model unticked `parameterized_sql` and also filed a
    finding about a concatenated query — adds nothing here, because that finding is already
    on the report carrying its own severity, and that is the thing that gates. This is the
    same rule the Reviewer runs on: without a failure you have a feeling, not a finding.
    """
    explained = {f.rule for f in findings if not f.false_positive}
    out = []
    for key in CHECKLIST_KEYS:
        answered = checklist.get(key)
        if answered is True or key in explained:
            continue
        in_play = applies.get(key, True)
        if answered is None:
            why = "was not answered"
        else:
            why = "was marked not satisfied, and no finding says what is wrong"
        out.append(
            SecurityFinding(
                tool="checklist",
                rule=key,
                file="",
                line=0,
                severity="medium" if in_play else "info",
                message=(
                    f"`{key}` {why}. "
                    + (
                        "The lines this change added touch this area, so it is worth a look."
                        if in_play
                        else "Nothing this change added appears to touch this area."
                    )
                ),
                verified_by_llm=False,
                false_positive=False,
                rationale="",
                in_diff=False,  # a checklist answer is not a line of code, and cannot gate
            )
        )
    return out


class SecurityAgent(Agent):
    """Reads what the scanners found, against the files they point at."""

    role: ClassVar[str] = "security"
    prompt_file: ClassVar[str] = "security"
    tool_names: ClassVar[list[str]] = ROLE_TOOLS["security"]
    max_iterations: ClassVar[int] = MAX_ITERATIONS

    async def run(
        self,
        provider: LLMProvider,
        ctx: RunContext,
        goal: str,
        findings: list[SecurityFinding],
        files: list[diffmod.FileDiff],
        hooks: Hooks,
    ) -> tuple[SecurityReport, RunOutcome]:
        """``(report, outcome)``. ``critical`` is recomputed here, not trusted."""
        applies = applicable(files)
        to_verify, unverified = triage(findings)
        submit = submit_tool("submit_security", SecurityReport, SECURITY_KEY)
        outcome = await self.run_tools(
            provider,
            ctx,
            verify_message(goal, to_verify, files, applies),
            hooks,
            extra_tools=[submit],
            must_call=submit.name,
            rubric=RUBRIC,
        )
        report = ctx.submitted.get(SECURITY_KEY)
        if not isinstance(report, SecurityReport):
            raise AgentError(
                f"security agent did not submit a report (stop_reason={outcome.stop_reason}, "
                f"turns={outcome.turns})"
            )
        merged = merge(to_verify, report.findings) + unverified
        merged += checklist_findings(report.checklist, merged, applies)
        final = report.model_copy(update={"findings": merged, "critical": is_critical(merged)})
        log.info(
            "security_done",
            scanner_findings=len(findings),
            verified=sum(1 for f in merged if f.verified_by_llm),
            false_positives=sum(1 for f in merged if f.false_positive),
            unverified=len(unverified),
            critical=final.critical,
            model_said_critical=report.critical,
        )
        return final, outcome


# Rendered into agents/prompts/security.md at `{rubric}`.
RUBRIC = "\n".join(
    [
        "| Severity | Meaning |",
        "|---|---|",
        "| critical | a secret in the history; remote code execution; authentication "
        "bypassed outright |",
        "| high | injection, a missing authorisation check, a known-vulnerable "
        "dependency, weak crypto on something that matters |",
        "| medium | a hardening gap: a missing rate limit, an over-broad CORS policy, "
        "an error that leaks internals |",
        "| low | defence in depth that is absent but not reachable |",
        "| info | context. Cannot gate anything, and that is what it is for. |",
    ]
)


def gating(findings: list[SecurityFinding]) -> list[SecurityFinding]:
    """The findings that are actually holding the run up — for the fix task and the report."""
    return [
        f
        for f in findings
        if f.severity in ("critical", "high") and f.in_diff and not f.false_positive
    ]


def render_findings(findings: list[SecurityFinding]) -> str:
    """The findings as a task description the Coder can work from, grouped by file."""
    by_file: dict[str, list[SecurityFinding]] = {}
    for f in findings:
        by_file.setdefault(f.file or "(no file)", []).append(f)
    out = [
        "A security scan of your change raised these. Fix each one, or say in "
        "`notes_for_reviewer` why it is not a problem — a rebuttal is a valid answer.",
        "",
        "Never paste a secret into a fix, a commit message or a note. If a finding is a "
        "committed credential, remove it from the code and say that it needs rotating.",
    ]
    for path in sorted(by_file):
        out.append(f"\n## {path}")
        for f in sorted(by_file[path], key=lambda x: x.line):
            where = f"line {f.line}" if f.line else "(no line)"
            out.append(f"- **{where}** [{f.severity}] {f.tool} {f.rule}: {f.message}")
            if f.rationale:
                out.append(f"  confirmed because: {f.rationale}")
    return "\n".join(out)


def fix_task(findings: list[SecurityFinding], round_n: int) -> TaskSpec:
    """An ordinary task, so it gets the debug attempts and the test gate every task gets."""
    return TaskSpec(
        id=f"fix-security-{round_n}",
        title=f"Address security findings (round {round_n})",
        description=render_findings(findings),
        depends_on=[],
        files=sorted({f.file for f in findings if f.file}),
        acceptance_criteria=[
            "Each listed finding is fixed, or rebutted in notes_for_reviewer with a reason",
            "No secret is written into the code, a commit message, or a note",
            "Existing tests still pass, and new behaviour has a test that would catch it",
        ],
        test_selector="",
    )


def unresolved(findings: list[SecurityFinding]) -> list[str]:
    """Findings as `known_issues` lines, for when the fix budget ran out."""
    return [
        f"[{f.severity}] {f.tool} {f.rule} — {f.file}:{f.line} {f.message}"
        for f in gating(findings)
    ]


def artifact(report: SecurityReport, applies: dict[str, bool]) -> dict[str, Any]:
    """What gets stored: the report, and what the diff was judged to put in play."""
    return {
        "report": report.model_dump(mode="json"),
        "applicable": applies,
        "gating": [f.model_dump(mode="json") for f in gating(report.findings)],
    }
