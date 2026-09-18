"""Four scanners, four shapes, one finding type.

Every fixture under ``tests/fixtures/scanners/`` is output a real scanner produced on a
file written to trip it. That is not ceremony — generating them turned up two facts no
hand-written sample would have carried, and both are asserted below because both change
what the parser has to do:

- **pip-audit reports every vulnerability twice.** Ten entries for five ids on one package.
- **pip-audit has no severity field at all**, so "map by CVSS if present" is always `high`.

A third came from the tool rather than the format: gitleaks *allowlists* the well-known AWS
documentation key, so a test planting `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` and
expecting the gate to fire proves nothing. The fixture uses a key gitleaks actually flags.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo import diff as d
from tests.fakes import planted_secret
from tools import scanners
from tools.scanner_parsers import parse_bandit, parse_gitleaks, parse_pip_audit, parse_semgrep

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scanners"


def load(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text())


# ---- bandit ---------------------------------------------------------------------------


def test_bandit_findings_keep_their_rule_place_and_severity() -> None:
    findings = parse_bandit(load("bandit"))  # type: ignore[arg-type]

    assert len(findings) == 6
    assert all(f.tool == "bandit" for f in findings)
    md5 = next(f for f in findings if f.rule == "B324")
    assert md5.severity == "high"
    assert md5.file == "src/app.py" and md5.line == 6
    assert "MD5" in md5.message
    shell = next(f for f in findings if f.rule == "B602")
    assert shell.severity == "high" and "shell=True" in shell.message


def test_bandit_severities_map_to_ours_without_inventing_a_critical() -> None:
    """bandit's highest is HIGH. Promoting it to `critical` would put a gate in a map."""
    assert set(scanners_severity_values()) == {"high", "medium", "low"}


def scanners_severity_values() -> list[str]:
    from tools.scanner_parsers.bandit import SEVERITY

    return list(SEVERITY.values())


def test_a_file_bandit_could_not_parse_becomes_a_finding() -> None:
    """A file nobody scanned is not a file with nothing wrong in it."""
    findings = parse_bandit({"results": [], "errors": [{"filename": "a.py", "reason": "syntax"}]})

    (f,) = findings
    assert f.rule == "scan-error" and f.severity == "info"
    assert "could not scan" in f.message and "syntax" in f.message


def test_bandit_with_nothing_to_say_says_nothing() -> None:
    assert parse_bandit({"results": [], "errors": []}) == []
    assert parse_bandit({}) == []


# ---- semgrep --------------------------------------------------------------------------


def test_semgrep_reads_the_fields_it_nests_under_extra() -> None:
    findings = parse_semgrep(load("semgrep"))  # type: ignore[arg-type]

    results = [f for f in findings if f.rule != "scan-error"]
    assert len(results) == 3
    shell = next(f for f in results if "subprocess-shell-true" in f.rule)
    assert shell.severity == "high", "semgrep's ERROR is our high"
    assert shell.file == "src/app.py" and shell.line == 10
    md5 = [f for f in results if "md5" in f.rule.lower()]
    assert len(md5) == 2 and all(f.severity == "medium" for f in md5), "WARNING is medium"


def test_a_semgrep_rule_that_could_not_run_is_reported_as_info() -> None:
    """Coverage smaller than it looks is worth saying, and this is the only place it shows."""
    findings = parse_semgrep(load("semgrep"))  # type: ignore[arg-type]

    error = next(f for f in findings if f.rule == "scan-error")
    assert error.severity == "info"
    assert "could not parse rule pack" in error.message


def test_semgrep_info_cannot_reach_a_gate() -> None:
    """INFO is advisory, so it maps to `info` and never to `low`."""
    from tools.scanner_parsers.semgrep import SEVERITY

    assert SEVERITY["INFO"] == "info"


# ---- gitleaks -------------------------------------------------------------------------


def test_every_leak_is_critical_because_a_committed_secret_is_not_a_matter_of_degree() -> None:
    findings = parse_gitleaks(load("gitleaks"))  # type: ignore[arg-type]

    (f,) = findings
    assert f.severity == "critical"
    assert f.tool == "gitleaks" and f.rule == "generic-api-key"
    assert f.file == "src/config.py" and f.line == 1


def test_the_secret_itself_never_reaches_the_finding() -> None:
    """It would travel into a report, an artifact, a log line — and a pull request body on
    GitHub, which is publishing the thing the scanner exists to catch."""
    raw = load("gitleaks")
    assert isinstance(raw, list) and "Secret" in raw[0], "the tool does report it"

    findings = parse_gitleaks(raw)

    body = json.dumps([f.model_dump() for f in findings])
    assert "Secret" not in body
    assert raw[0]["Secret"] not in body
    assert "value withheld" in findings[0].message


def test_the_commit_is_named_so_a_human_can_go_and_rotate_it() -> None:
    findings = parse_gitleaks(load("gitleaks"))  # type: ignore[arg-type]
    assert "in commit" in findings[0].message


def test_no_leaks_is_no_findings() -> None:
    assert parse_gitleaks([]) == []


# ---- pip-audit ------------------------------------------------------------------------


def test_pip_audit_duplicates_are_collapsed() -> None:
    """Its real output lists every advisory once per source: ten entries, five ids. Parsed
    naively, every count in the report would be double."""
    raw = load("pip_audit")
    assert isinstance(raw, dict)
    entries = [v["id"] for dep in raw["dependencies"] for v in dep.get("vulns", [])]
    assert len(entries) == 10 and len(set(entries)) == 5, "the fixture keeps the duplication"

    findings = parse_pip_audit(raw)

    assert len(findings) == 5
    assert len({f.rule for f in findings}) == 5


def test_a_dependency_finding_says_the_version_and_the_fix() -> None:
    findings = parse_pip_audit(load("pip_audit"))  # type: ignore[arg-type]

    f = next(f for f in findings if f.rule == "PYSEC-2018-28")
    assert "requests 2.19.0" in f.message
    assert "fixed in: 2.20.0" in f.message, "the actionable part"
    assert f.file == "(dependencies)" and f.line == 0, "a pin has no line to point at"


def test_every_dependency_finding_is_high_because_there_is_no_severity_to_read() -> None:
    """pip-audit's JSON carries id, fix_versions, aliases and a description — nothing else.
    Mapping "by CVSS if present" would be inventing a number."""
    raw = load("pip_audit")
    assert isinstance(raw, dict)
    for dep in raw["dependencies"]:
        for vuln in dep.get("vulns", []):
            assert not {"severity", "cvss", "cvss_score"} & set(vuln), vuln.keys()

    assert {f.severity for f in parse_pip_audit(raw)} == {"high"}


def test_a_clean_dependency_contributes_nothing() -> None:
    raw = load("pip_audit")
    assert isinstance(raw, dict)
    clean = next(d for d in raw["dependencies"] if not d.get("vulns"))
    assert parse_pip_audit({"dependencies": [clean]}) == []


# ---- which findings are this run's ----------------------------------------------------


def file_diff(path: str, added_lines: set[int]) -> d.FileDiff:
    return d.FileDiff(
        path=path, added=len(added_lines), removed=0, text="", added_lines=added_lines
    )


def test_only_a_finding_on_a_line_the_run_added_is_in_the_diff() -> None:
    """A vulnerability the agent inherited is worth listing and is not its to answer for."""
    findings = parse_bandit(load("bandit"))  # type: ignore[arg-type]
    files = [file_diff("src/app.py", {6})]  # the run wrote line 6 and nothing else

    tagged = scanners.tag_in_diff(findings, files)

    mine = [f for f in tagged if f.in_diff]
    assert {f.line for f in mine} == {6}
    assert all(not f.in_diff for f in tagged if f.line != 6)


def test_a_dependency_finding_is_the_runs_only_if_it_touched_a_manifest() -> None:
    """ "You added a vulnerable dependency" and "this was already here" are different
    claims, and only the first is the agent's."""
    findings = parse_pip_audit(load("pip_audit"))  # type: ignore[arg-type]

    untouched = scanners.tag_in_diff(findings, [file_diff("src/app.py", {1})])
    assert all(not f.in_diff for f in untouched)

    bumped = scanners.tag_in_diff(findings, [file_diff("uv.lock", {1})])
    assert all(f.in_diff for f in bumped)


def test_a_finding_with_no_line_falls_back_to_the_file() -> None:
    """A scanner that named a file the run created is talking about the run's work even if
    it could not say where in it."""
    findings = parse_bandit({"results": [], "errors": [{"filename": "src/new.py", "reason": "x"}]})

    tagged = scanners.tag_in_diff(findings, [file_diff("src/new.py", {1, 2})])
    assert tagged[0].in_diff

    elsewhere = scanners.tag_in_diff(findings, [file_diff("src/other.py", {1})])
    assert not elsewhere[0].in_diff


# ---- a scanner that does not run ------------------------------------------------------


def test_a_failed_scanner_is_a_finding_because_silence_reads_as_clean() -> None:
    f = scanners.failure("semgrep", "exit 137: killed")

    assert f.tool == "semgrep" and f.rule == "scan-failed"
    assert f.severity == "info", "it cannot gate: nothing was found, something was not looked for"
    assert "did not run" in f.message and "exit 137" in f.message


def test_json_survives_a_tool_that_printed_something_first() -> None:
    """Scanners prepend progress lines and warnings often enough to handle it."""
    assert scanners._loads('Scanning...\n{"results": []}') == {"results": []}
    assert scanners._loads('warning: slow\n[{"a": 1}]') == [{"a": 1}]
    assert scanners._loads("") is None
    assert scanners._loads("not json at all") is None
    # The ordering matters: looking for an object first would return the first dict nested
    # inside a list, so gitleaks' whole report would arrive as its first finding.
    assert scanners._loads('info\n[{"RuleID": "a"}, {"RuleID": "b"}]') == [
        {"RuleID": "a"},
        {"RuleID": "b"},
    ]


def test_a_gitleaks_finding_is_tagged_into_the_diff_so_the_report_can_gate() -> None:
    """The step `SecurityReport.critical` depends on, and it had no test.

    `parse_gitleaks` sets `in_diff=False` — it reads commits and knows nothing about the
    diff — and `security.is_critical` requires `in_diff`. So a committed secret reaches
    `critical=True` only because `tag_in_diff` flips it here. If that flip broke, the push
    gate would still refuse (it ignores `in_diff` on purpose) but the report would say
    `critical: no`, and the pull request body would say it too.
    """
    findings = parse_gitleaks(load("gitleaks"))  # type: ignore[arg-type]
    (leak,) = findings
    assert leak.in_diff is False, "the parser cannot know; the premise of this test"

    tagged = scanners.tag_in_diff(findings, [file_diff(leak.file, {leak.line})])

    assert tagged[0].in_diff is True
    from agents.security import is_critical

    assert is_critical(tagged) is True, "which is what makes the report say critical"


def test_a_secret_on_a_line_the_run_did_not_touch_does_not_make_the_report_critical() -> None:
    """The other direction. A credential already in the file is a real problem and is not
    this run's; the push gate still refuses it, but the report does not blame the agent."""
    findings = parse_gitleaks(load("gitleaks"))  # type: ignore[arg-type]
    (leak,) = findings

    tagged = scanners.tag_in_diff(findings, [file_diff(leak.file, {leak.line + 50})])

    from agents.security import is_critical

    assert tagged[0].in_diff is False
    assert is_critical(tagged) is False


# ---- the one scanner that quoted what it found ----------------------------------------


def test_bandits_hardcoded_password_findings_never_carry_the_value() -> None:
    """Found by a test asserting the value stays out of a report, and it did not.

    bandit 1.8.6's B105 reports `Possible hardcoded password: 'hunter2'` — the literal, in
    `issue_text`, which `parse_bandit` copied into `message` and `repo/pr_body.py` renders
    into the published body. gitleaks redacts its own findings and semgrep's rule messages
    are static templates, so this was the only path by which a credential reached a forge.
    """
    # Derived, not written down — this repository's own pre-commit hook flags a literal
    # here, which is the hook being right: a test about not publishing a credential is no
    # place to paste one. `planted_secret` is the same helper the scanner tests use.
    secret = planted_secret("bandit-b105")
    raw = {
        "results": [
            {
                "test_id": "B105",
                "test_name": "hardcoded_password_string",
                "filename": "src/settings.py",
                "line_number": 1,
                "issue_severity": "LOW",
                "issue_text": f"Possible hardcoded password: '{secret}'",
            }
        ],
        "errors": [],
    }

    (f,) = parse_bandit(raw)

    assert secret not in f.message
    assert "value withheld" in f.message
    assert "hardcoded_password_string" in f.message, "still says what the rule was"
    assert f.file == "src/settings.py" and f.line == 1, "and where to go and look"


def test_the_low_entropy_case_is_why_this_matters() -> None:
    """ "hunter2" is flagged by B105 and by nothing else.

    gitleaks needs one of its rules to match, and a short common password matches none — so
    the push is *not* refused, the run completes, and the pull request body is published.
    That is the case where withholding the value is the only thing standing between a
    credential and a forge, which is why this is fixed in the parser and not in the gate.
    """
    raw = {
        "results": [
            {
                "test_id": "B105",
                "test_name": "hardcoded_password_string",
                "filename": "src/db.py",
                "line_number": 4,
                "issue_severity": "LOW",
                "issue_text": "Possible hardcoded password: 'hunter2'",
            }
        ],
        "errors": [],
    }

    (f,) = parse_bandit(raw)

    assert "hunter2" not in f.message


def test_bandits_other_findings_keep_their_own_words() -> None:
    """Only the rules that quote a literal are rewritten. B324's text explains the problem
    and replacing it would cost the reader the explanation."""
    findings = parse_bandit(load("bandit"))  # type: ignore[arg-type]

    md5 = next(f for f in findings if f.rule == "B324")
    assert "MD5" in md5.message and "value withheld" not in md5.message


def test_every_bandit_rule_that_quotes_a_value_is_in_the_withheld_set() -> None:
    """The set is a claim about bandit's own behaviour, so it is written down next to the
    reason. If a future bandit adds another quoting rule this test will not catch it — what
    it catches is somebody trimming the set without reading why it exists."""
    from tools.scanner_parsers.bandit import SECRET_RULES

    assert SECRET_RULES == {"B105", "B106", "B107"}, (
        "B105 string, B106 funcarg, B107 default — the hardcoded-password family, all of "
        "which interpolate the literal into issue_text"
    )
