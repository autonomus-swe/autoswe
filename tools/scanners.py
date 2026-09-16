"""Running the four scanners, and deciding which of their findings are this run's.

Where each one runs is forced by what it needs, not by preference:

| scanner | runs | because |
|---|---|---|
| bandit | sandbox | pure static analysis over the code, and the code is in there |
| semgrep | sandbox | same, with rule packs vendored into the image at build time |
| gitleaks | worker | it reads commits, and the sandbox has no git |
| pip-audit | worker | it queries a vulnerability database, and the sandbox has no network |

The split is the reason `run_all` takes both a sandbox and a worktree: two of these cannot
see what the other two are looking at.

**A scanner that fails is reported, not raised.** A crashed scanner means a whole class of
problem went unlooked-for, and silently producing no findings would read as "nothing
wrong". Each failure becomes one `info` finding naming the tool, which is visible in the
report and in the pull request.

**Only findings inside the change can gate.** `tag_in_diff` asks `repo.diff.touches` whether
a finding's line is one this run added. A vulnerability the agent inherited is worth
listing and is not its to answer for, and the distinction is drawn from the diff rather
than from anything a model says.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from contracts import SecurityFinding
from observability.logging import get_logger
from repo import diff as diffmod
from sandbox.base import Sandbox
from tools.scanner_parsers import parse_bandit, parse_gitleaks, parse_pip_audit, parse_semgrep

log = get_logger(__name__)

SCAN_TIMEOUT_S = 300
# Vendored into the sandbox image at build time, pinned there. `--config auto` and `p/…`
# packs fetch rules over the network, and the sandbox has none by the time this runs.
SEMGREP_RULES = "/opt/semgrep-rules"
REQUIREMENTS_REL = ".autoswe/req.txt"

BANDIT_CMD = "bandit -r . -f json -x ./.venv,./tests,./.autoswe -q"
# The version check is off for a measurable reason, not tidiness: semgrep contacts
# semgrep.dev for it, `--metrics=off` does not cover it, and the sandbox has no network by
# the time a scan runs — so it waits out the connect timeout every time. Measured in the
# image, networkless, on a two-file repository: 2m36s with the check, 1m01s without, inside
# a 300s budget. Set here as well as in the image's ENV so it holds against an older image.
SEMGREP_CMD = (
    "SEMGREP_ENABLE_VERSION_CHECK=0 "
    f"semgrep --config {SEMGREP_RULES} --json --metrics=off --exclude .venv --exclude .autoswe ."
)
EXPORT_CMD = (
    f"mkdir -p .autoswe && uv export --frozen --format requirements-txt > {REQUIREMENTS_REL}"
)


def failure(tool: str, why: str) -> SecurityFinding:
    """A scanner that did not run, as a finding. Silence would read as a clean result."""
    return SecurityFinding(
        tool=tool,
        rule="scan-failed",
        file="",
        line=0,
        severity="info",
        message=f"{tool} did not run, so nothing it looks for was checked: {why}"[:500],
        verified_by_llm=False,
        false_positive=False,
        rationale="",
        in_diff=False,
    )


def _loads(text: str) -> Any:
    """Scanner JSON, or None. A tool that printed a warning before its JSON is common."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some tools print a progress line before their JSON. Take from whichever opener
        # comes *first* — trying `{` before `[` finds the first object nested inside an
        # array and returns that instead of the array, which is how gitleaks' list would
        # have arrived as its first finding.
        candidates = [(text.find(c), c) for c in "[{" if text.find(c) != -1]
        if not candidates:
            return None
        start, opener = min(candidates)
        end = text.rfind("]" if opener == "[" else "}")
        if end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None


async def run_bandit(sandbox: Sandbox) -> list[SecurityFinding]:
    """In the sandbox. Exit 1 means findings, which is a result and not an error."""
    result = await sandbox.exec(BANDIT_CMD, timeout_s=SCAN_TIMEOUT_S)
    data = _loads(result.stdout)
    if data is None:
        return [failure("bandit", f"exit {result.exit_code}: {result.stderr[-200:] or 'no JSON'}")]
    return parse_bandit(data)


async def run_semgrep(sandbox: Sandbox) -> list[SecurityFinding]:
    """In the sandbox, against the rules vendored into the image."""
    result = await sandbox.exec(SEMGREP_CMD, timeout_s=SCAN_TIMEOUT_S)
    data = _loads(result.stdout)
    if data is None:
        return [failure("semgrep", f"exit {result.exit_code}: {result.stderr[-200:] or 'no JSON'}")]
    return parse_semgrep(data)


async def run_gitleaks(worktree: Path, base_sha: str) -> list[SecurityFinding]:
    """On the worker, over this run's commits only.

    Scoped with ``--log-opts`` so a secret that was already in the history is not reported
    as though the agent had just written it. Exit 1 means leaks found.
    """
    report = worktree / ".autoswe" / "gitleaks.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "gitleaks",
        "detect",
        "--source",
        str(worktree),
        f"--log-opts={base_sha}..HEAD",
        "--report-format",
        "json",
        "--report-path",
        str(report),
        "--redact",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), SCAN_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        return [failure("gitleaks", f"timed out after {SCAN_TIMEOUT_S}s")]
    if proc.returncode not in (0, 1):  # 1 is "leaks found", which is the point
        return [
            failure("gitleaks", f"exit {proc.returncode}: {err.decode(errors='replace')[-200:]}")
        ]
    if not report.is_file():
        return []  # no report written means no leaks
    data = _loads(report.read_text())
    report.unlink(missing_ok=True)
    return parse_gitleaks(data or [])


async def run_pip_audit(sandbox: Sandbox, worktree: Path) -> list[SecurityFinding]:
    """Split across both: export in the sandbox, audit on the worker.

    The export has to happen where the project's lockfile and interpreter are, and the
    audit has to happen where there is a network to reach the advisory database. Neither
    half can do the other's job.
    """
    requirements = worktree / REQUIREMENTS_REL
    try:
        export = await sandbox.exec(EXPORT_CMD, timeout_s=SCAN_TIMEOUT_S)
        # The shell redirect creates the file before `uv export` runs, so its existence
        # says nothing — an empty one is what a project with no lockfile leaves behind.
        # Checked for content, and removed on every path: an export file left in the
        # worktree is litter inside the very change being reviewed.
        exported = requirements.is_file() and requirements.stat().st_size > 0
        if not export.ok or not exported:
            return [
                failure(
                    "pip-audit",
                    f"could not export requirements (exit {export.exit_code}); "
                    "a project with no lockfile cannot be audited this way",
                )
            ]
        proc = await asyncio.create_subprocess_exec(
            "pip-audit",
            "-r",
            str(requirements),
            "--no-deps",
            "-f",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), SCAN_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            return [failure("pip-audit", f"timed out after {SCAN_TIMEOUT_S}s")]
    finally:
        requirements.unlink(missing_ok=True)
    data = _loads(out.decode(errors="replace"))
    if data is None:
        return [
            failure(
                "pip-audit",
                f"exit {proc.returncode}: {err.decode(errors='replace')[-200:] or 'no JSON'}",
            )
        ]
    return parse_pip_audit(data)


def tag_in_diff(
    findings: list[SecurityFinding], files: list[diffmod.FileDiff]
) -> list[SecurityFinding]:
    """Mark the findings that sit on a line this run added.

    Only these can make a report critical. A finding with no line to point at — pip-audit's
    dependency findings — is tagged by whether the change touched the manifest at all,
    because "you added a vulnerable dependency" and "this dependency was already here" are
    different claims and only the first is the agent's.
    """
    changed = {f.path for f in files}
    manifests = {"pyproject.toml", "uv.lock", "requirements.txt", "poetry.lock"}
    out = []
    for f in findings:
        if f.tool == "pip-audit":
            inside = bool(changed & manifests)
        elif f.line > 0:
            inside = diffmod.touches(files, f.file, f.line)
        else:
            # No line: fall back to the file. A scanner that named a file the run created
            # is talking about the run's work even if it could not say where.
            inside = f.file in changed
        out.append(f.model_copy(update={"in_diff": inside}))
    return out


async def run_all(
    sandbox: Sandbox, worktree: Path, base_sha: str, files: list[diffmod.FileDiff]
) -> list[SecurityFinding]:
    """All four, concurrently, tagged against the diff.

    Concurrent because two run in the sandbox and two on the worker, so they do not queue
    behind each other. `return_exceptions` because one scanner falling over must not take
    the other three with it — the whole point of running four is that they disagree about
    what to look for.
    """
    results = await asyncio.gather(
        run_bandit(sandbox),
        run_semgrep(sandbox),
        run_gitleaks(worktree, base_sha),
        run_pip_audit(sandbox, worktree),
        return_exceptions=True,
    )
    findings: list[SecurityFinding] = []
    for tool, result in zip(("bandit", "semgrep", "gitleaks", "pip-audit"), results, strict=True):
        if isinstance(result, BaseException):
            log.warning("scanner_raised", tool=tool, error=f"{type(result).__name__}: {result}")
            findings.append(failure(tool, f"{type(result).__name__}: {result}"))
        else:
            findings.extend(result)
    tagged = tag_in_diff(findings, files)
    log.info(
        "scanners_done",
        findings=len(tagged),
        in_diff=sum(1 for f in tagged if f.in_diff),
        by_tool={
            t: sum(1 for f in tagged if f.tool == t)
            for t in ("bandit", "semgrep", "gitleaks", "pip-audit")
        },
    )
    return tagged
