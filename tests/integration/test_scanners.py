"""The four scanners, run for real: two in the sandbox, two on the worker.

The parsers are unit-tested against saved output. This is the other half — that the tools
are actually present where the design says they are, that the two in the sandbox work with
the network disconnected, and that the split between sandbox and worker is real rather than
aspirational.

One trap is worth knowing about before reading the secret test: gitleaks **allowlists the
well-known AWS documentation key**. A test planting `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`
and expecting the gate to fire finds nothing and proves nothing — I wrote that first and it
passed for the wrong reason.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from contracts import SecurityFinding
from repo import diff as d
from repo.gitcmd import git
from sandbox.docker import DockerSandbox
from tools import scanners

pytestmark = pytest.mark.integration

IMAGE = "agent-sandbox:python-3.12"

VULNERABLE = """import hashlib
import subprocess


def weak(password: str) -> str:
    return hashlib.md5(password.encode()).hexdigest()


def run(cmd: str) -> bytes:
    return subprocess.check_output(cmd, shell=True)
"""


def _docker_ready() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(IMAGE)
        return True
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_ready(), reason=f"docker or image {IMAGE} unavailable (run `make sandbox-image`)"
)
requires_gitleaks = pytest.mark.skipif(
    not __import__("shutil").which("gitleaks"), reason="gitleaks is not on PATH"
)


def planted_secret(tag: str) -> str:
    """A key gitleaks will flag, derived rather than written down.

    Written as a literal it trips this repository's own pre-commit gitleaks hook — which is
    the hook being right: it cannot tell a fixture from a real key, and allowlisting the
    file so the fixture could live in it would hide a real one pasted here later. Deriving
    it keeps the source clean and the value deterministic, so the test cannot flake.

    Forty hex characters clear the `generic-api-key` entropy threshold; verified against
    gitleaks directly rather than assumed, because the other thing this file knows about
    gitleaks — the allowlist in the docstring above — was also not guessable.
    """
    return hashlib.sha256(f"autoswe-scanner-test-{tag}".encode()).hexdigest()[:40]


@pytest.fixture
async def project(host_tmp: Path) -> AsyncIterator[tuple[DockerSandbox, Path, str]]:
    """A git repository with one vulnerable file committed, in a networkless sandbox."""
    root = host_tmp / "repo"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "scanme"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
    )
    (root / "src" / "safe.py").write_text("def ok() -> int:\n    return 1\n")
    await git("init", "-q", "-b", "main", cwd=root)
    await git("add", "-A", cwd=root)
    await git("commit", "-q", "-m", "base", cwd=root)
    base_sha = (await git("rev-parse", "HEAD", cwd=root)).strip()

    # what a run would have written
    (root / "src" / "app.py").write_text(VULNERABLE)
    await git("add", "-A", cwd=root)
    await git("commit", "-q", "-m", "feat: add app", cwd=root)

    sandbox = DockerSandbox(
        uuid.uuid4(),
        root,
        image=IMAGE,
        network="agent-install",
        user=f"{os.getuid()}:{os.getgid()}",
    )
    await sandbox.start()
    try:
        await sandbox.disconnect_network()
        assert not await sandbox.has_network(), "the premise: these two run without one"
        yield sandbox, root, base_sha
    finally:
        await sandbox.stop()


@requires_docker
async def test_bandit_finds_the_planted_weaknesses_in_the_sandbox(
    project: tuple[DockerSandbox, Path, str],
) -> None:
    """Present in the image, and working with no network — which is when it has to work."""
    sandbox, _root, _sha = project

    findings = await scanners.run_bandit(sandbox)

    assert findings, "bandit found nothing in a file written to trip it"
    assert all(f.tool == "bandit" for f in findings)
    assert any(f.rule == "scan-failed" for f in findings) is False, [f.message for f in findings]
    rules = {f.rule for f in findings}
    assert rules & {"B324", "B303"}, f"expected a weak-hash rule, got {rules}"
    assert rules & {"B602", "B603", "B604"}, f"expected a shell=True rule, got {rules}"


@requires_docker
async def test_semgrep_runs_against_the_rules_vendored_into_the_image(
    project: tuple[DockerSandbox, Path, str],
) -> None:
    """The rules are baked in at a pinned commit, because `--config auto` needs a network
    the sandbox does not have — a run would otherwise scan against nothing and say so
    quietly."""
    sandbox, _root, _sha = project

    findings = await scanners.run_semgrep(sandbox)

    failed = [f for f in findings if f.rule == "scan-failed"]
    assert not failed, failed[0].message if failed else ""
    assert findings, "semgrep found nothing, which means the rules are not where it looked"
    assert any("subprocess" in f.rule or "shell" in f.rule for f in findings), {
        f.rule for f in findings
    }


@requires_docker
async def test_semgrep_does_not_wait_on_a_version_check_it_can_never_finish(
    project: tuple[DockerSandbox, Path, str],
) -> None:
    """The largest single cost in this scanner, and it buys nothing.

    semgrep contacts semgrep.dev for a version check that `--metrics=off` does not cover,
    and the sandbox has no network by the time a scan runs — so it waits out the connect
    timeout. Measured in this image, networkless, on a two-file repository: 2m36s with the
    check and 1m01s without, against a 300s budget.

    Pinned in two places because they fail differently: the command carries it so a stale
    image cannot reintroduce the wait, and the image's ENV carries it so anything else run
    in there (a human debugging a scan) does not pay it either.
    """
    sandbox, _root, _sha = project
    assert "SEMGREP_ENABLE_VERSION_CHECK=0" in scanners.SEMGREP_CMD

    shown = await sandbox.exec("printenv SEMGREP_ENABLE_VERSION_CHECK")

    assert shown.stdout.strip() == "0", "the image no longer disables the version check"


@requires_docker
async def test_the_vendored_ruleset_is_narrowed_to_the_security_rules(
    project: tuple[DockerSandbox, Path, str],
) -> None:
    """semgrep's cost here is rule *parsing*, not scanning.

    Measured inside this sandbox's own limits: the full 593-file set took 96.6s on a
    two-file repository and 98.6s on an empty one — the target barely registers. Narrowing
    to 337 files took a 181-file repository from 104.1s to 58.4s.

    Asserted rather than trusted because the narrowing lives in a `find` in the Dockerfile.
    A rebuild that drops it costs 46s of every run for lint findings the Reviewer already
    reports, and a rebuild that over-narrows loses security coverage — both silently.
    """
    sandbox, _root, _sha = project

    listing = await sandbox.exec("find /opt/semgrep-rules -name '*.yaml'")
    paths = listing.stdout.split()

    assert paths, "no rules are vendored, so semgrep scans against nothing and says so quietly"
    assert [p for p in paths if "/security/" in p], "the rules this scanner exists for"
    lint = [p for p in paths if any(c in p for c in ("/maintainability/", "/best-practice/"))]
    assert not lint, f"lint rules are the Reviewer's job, not a security gate's: {lint[:3]}"
    assert not [p for p in paths if "/secrets/gitleaks/" in p], (
        "the real gitleaks runs on the worker over the run's commits; this is its port"
    )
    assert [p for p in paths if "/secrets/security/" in p], (
        "semgrep's own secret rules stay: gitleaks reads commits, so it cannot see a secret "
        "in an untracked file, and full_diff deliberately includes untracked files"
    )
    assert len(paths) < 450, f"the narrowing did not take: {len(paths)} rule files"


@requires_gitleaks
async def test_a_secret_committed_by_the_run_is_found_and_never_echoed(host_tmp: Path) -> None:
    """The hard gate of this phase.

    The key is deliberately random-looking. gitleaks allowlists the AWS documentation
    example, so the obvious test — plant `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` — finds
    nothing, and would have had me conclude the scanner was broken.
    """
    root = host_tmp / "leaky"
    root.mkdir()
    (root / "ok.py").write_text("x = 1\n")
    await git("init", "-q", "-b", "main", cwd=root)
    await git("add", "-A", cwd=root)
    await git("commit", "-q", "-m", "base", cwd=root)
    base_sha = (await git("rev-parse", "HEAD", cwd=root)).strip()

    secret = planted_secret("committed")
    (root / "config.py").write_text(f'AWS_SECRET_ACCESS_KEY = "{secret}"\n')
    await git("add", "-A", cwd=root)
    await git("commit", "-q", "-m", "chore: config", cwd=root)

    findings = await scanners.run_gitleaks(root, base_sha)

    assert findings, "a committed secret was not found"
    (f,) = [x for x in findings if x.rule != "scan-failed"]
    assert f.severity == "critical", "a committed secret is not a matter of degree"
    assert f.file.endswith("config.py")
    assert secret not in f.message, "the value must never travel into a report or a PR body"
    assert "value withheld" in f.message


@requires_gitleaks
async def test_a_secret_already_in_the_history_is_not_blamed_on_this_run(
    host_tmp: Path,
) -> None:
    """Scoped with --log-opts to this run's commits. Otherwise every run against a
    repository with an old leak would be blocked forever and could never be unblocked."""
    root = host_tmp / "inherited"
    root.mkdir()
    (root / "config.py").write_text(f'TOKEN = "{planted_secret("inherited")}"\n')
    await git("init", "-q", "-b", "main", cwd=root)
    await git("add", "-A", cwd=root)
    await git("commit", "-q", "-m", "the leak, before the agent arrived", cwd=root)
    base_sha = (await git("rev-parse", "HEAD", cwd=root)).strip()

    (root / "feature.py").write_text("def added() -> int:\n    return 2\n")
    await git("add", "-A", cwd=root)
    await git("commit", "-q", "-m", "feat: what the run actually did", cwd=root)

    findings = await scanners.run_gitleaks(root, base_sha)

    assert [f for f in findings if f.rule != "scan-failed"] == []


@requires_docker
async def test_a_project_with_no_lockfile_reports_why_rather_than_nothing(
    project: tuple[DockerSandbox, Path, str],
) -> None:
    """The common case, not a broken one — and silence would read as "no vulnerabilities"."""
    sandbox, root, _sha = project
    assert not (root / "uv.lock").exists(), "the premise"

    findings = await scanners.run_pip_audit(sandbox, root)

    (f,) = findings
    assert f.rule == "scan-failed" and f.severity == "info"
    assert "lockfile" in f.message
    # The shell redirect creates the file before `uv export` runs, so a failed export
    # leaves an empty one behind — litter inside the change being reviewed.
    assert not (root / scanners.REQUIREMENTS_REL).exists(), "the export file is cleaned up"


@requires_docker
async def test_all_four_run_together_and_only_new_findings_are_tagged(
    project: tuple[DockerSandbox, Path, str],
) -> None:
    """The whole scanner pass, with the tagging that decides what can gate.

    `src/app.py` is the run's work and `src/safe.py` was already there, so a finding in the
    first is the agent's and a finding in the second is not — drawn from the diff, not from
    anything a model says.
    """
    sandbox, root, base_sha = project
    files = d.split_by_file(await d.full_diff(root, base_sha))
    assert [f.path for f in files] == ["src/app.py"]

    findings = await scanners.run_all(sandbox, root, base_sha, files)

    tools = {f.tool for f in findings}
    assert {"bandit", "semgrep"} <= tools, f"a sandbox scanner did not report: {tools}"
    mine = [f for f in findings if f.in_diff]
    assert mine, "nothing was attributed to the run, though it wrote the vulnerable file"
    assert all(f.file in ("src/app.py", "") for f in mine), [f.file for f in mine]
    assert not any(f.file == "src/safe.py" and f.in_diff for f in findings)


@requires_docker
async def test_one_scanner_failing_does_not_take_the_others_with_it(
    project: tuple[DockerSandbox, Path, str],
) -> None:
    """The point of running four is that they disagree about what to look for."""
    sandbox, root, base_sha = project

    async def explode(_sandbox: object) -> list[SecurityFinding]:
        raise RuntimeError("the scanner fell over")

    import tools.scanners as mod

    original = mod.run_semgrep
    mod.run_semgrep = explode  # type: ignore[assignment]
    try:
        findings = await scanners.run_all(sandbox, root, base_sha, [])
    finally:
        mod.run_semgrep = original

    assert any(f.tool == "bandit" and f.rule != "scan-failed" for f in findings), (
        "bandit's findings were lost with semgrep's failure"
    )
    broken = next(f for f in findings if f.tool == "semgrep")
    assert broken.rule == "scan-failed" and broken.severity == "info"
    assert "fell over" in broken.message
