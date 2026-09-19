"""The chaos scenarios as the agent actually meets them: installed, in a real sandbox.

There are three levels at which these fixtures can be checked, and they catch different
things:

1. `test_chaos_fixtures.py` runs the suite on the host. Fast, no Docker, but the host has
   a network and the host's interpreter, so it cannot see what the agent sees.
2. **This file** installs the repository in the real sandbox — non-root, no network after
   the install — and parses the report the run would parse. No model, so it is cheap and
   deterministic, and it covers everything up to the moment an agent would be asked to
   think.
3. `tests/e2e/test_m3.py` adds the model. It needs a funded key and is the only part still
   unproven.

Level 2 exists because level 1 cannot reach it. `d-network` *passes* on a networked host;
only here does it fail the way it is supposed to. That is not hypothetical — writing this
file is what found `socket.gaierror` being classified as a `timeout`, because the line that
caused it reads `urlopen(url, timeout=5)`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from contracts import TestReport
from orchestrator.nodes import install_command
from repo import profile as repo_profile
from repo.gitcmd import git
from repo.stacks import PYTHON
from sandbox.docker import DockerSandbox
from tests.e2e.chaos import materialise
from tools.test_report import parse_json_report
from tools.tests import test_command as build_test_command

pytestmark = pytest.mark.integration

IMAGE = "agent-sandbox:python-3.12"
TEST_COMMAND = "uv run --no-sync pytest -q"
INSTALL_TIMEOUT_S = 900


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


@pytest.fixture
async def installed(host_tmp: Path) -> AsyncIterator[tuple[DockerSandbox, Path]]:
    """A sandbox with the fixture repository installed in it.

    Per test rather than per module: the install is a second on a project this small, and
    a scenario that has to share a venv with the previous one is not testing itself.
    """
    root = host_tmp / "repo"
    await materialise(root)
    sandbox = DockerSandbox(
        uuid.uuid4(),
        root,
        image=IMAGE,
        network="agent-install",
        user=f"{os.getuid()}:{os.getgid()}",
    )
    await sandbox.start()
    try:
        await sandbox.connect_install_network()
        result = await sandbox.exec(
            install_command(root, repo_profile.collect(root)), timeout_s=INSTALL_TIMEOUT_S
        )
        why = (result.stdout + result.stderr)[-800:]
        assert result.ok, f"the fixture must install cleanly: {why}"
        await sandbox.disconnect_network()
        assert not await sandbox.has_network(), "the scenarios depend on there being none"
        yield sandbox, root
    finally:
        await sandbox.stop()


async def run_branch(sandbox: DockerSandbox, root: Path, branch: str) -> TestReport:
    await git("checkout", "-q", branch, cwd=root)
    (root / PYTHON.report_rel).unlink(missing_ok=True)
    await sandbox.exec(build_test_command(TEST_COMMAND, ""), timeout_s=600)
    report_file = root / PYTHON.report_rel
    assert report_file.is_file(), f"{branch} produced no report"
    return parse_json_report(json.loads(report_file.read_text()), TEST_COMMAND, worktree=root)


@requires_docker
async def test_the_fixture_installs_and_main_is_green(
    installed: tuple[DockerSandbox, Path],
) -> None:
    """If this fails, every other scenario is measuring the environment, not the bug."""
    sandbox, root = installed
    report = await run_branch(sandbox, root, "main")
    assert report.passed and report.total == 3, report.failures


@requires_docker
async def test_the_off_by_one_gives_the_debugger_both_values(
    installed: tuple[DockerSandbox, Path],
) -> None:
    sandbox, root = installed
    report = await run_branch(sandbox, root, "a-off-by-one")

    assert report.failed == 2 and not report.passed
    assert {f.kind for f in report.failures} == {"assertion"}
    # Two different tests must not collapse to one fingerprint, or the run cannot tell
    # fixing half the problem from fixing none of it.
    assert len({f.signature for f in report.failures}) == 2
    dropped = next(f for f in report.failures if "partial_page" in f.test_id)
    assert "[[1, 2], [3, 4]] == [[1, 2], [3, 4], [5]]" in dropped.message


@requires_docker
async def test_the_missing_import_names_the_line_that_would_not_load(
    installed: tuple[DockerSandbox, Path],
) -> None:
    sandbox, root = installed
    report = await run_branch(sandbox, root, "b-missing-import")

    (failure,) = report.failures
    assert failure.kind == "import"
    assert [f.file for f in failure.frames] == ["tests/test_stamp.py", "chaos/stamp.py"]
    assert "EPOCH = datetime(" in failure.frames[-1].code, "read from disk, not from the report"


@requires_docker
async def test_no_network_is_an_environment_failure_not_a_timeout(
    installed: tuple[DockerSandbox, Path],
) -> None:
    """The scenario that can only be checked here, and the bug that proves the point.

    On a networked host this test passes and there is nothing to classify. In the sandbox
    it fails with `socket.gaierror` — and the traceback quotes `urlopen(url, timeout=5)`,
    which a classifier reading message and traceback together scored as a `timeout`.

    The direction of the error is what makes it worth a test: the Debugger is told that
    `environment` failures are not its to fix, and told nothing of the sort about
    `timeout`. Misclassified, the one scenario built to stop it mocking the network was
    the one that invited it to.
    """
    sandbox, root = installed
    report = await run_branch(sandbox, root, "d-network")

    (failure,) = report.failures
    assert failure.kind == "environment", f"classified {failure.kind}: {failure.message}"
    assert "name resolution" in failure.message or "gaierror" in failure.message
    assert any(f.in_repo and f.file == "chaos/fetch.py" for f in failure.frames)


@requires_docker
async def test_the_inherited_failure_is_separable_from_the_task(
    installed: tuple[DockerSandbox, Path],
) -> None:
    """What the baseline has to be able to tell apart, on a real report."""
    from agents import tester

    sandbox, root = installed
    report = await run_branch(sandbox, root, "e-baseline")

    legacy = [f for f in report.failures if "test_legacy" in f.test_id]
    assert len(legacy) == 1, "the failure the repository already had"

    # Excused as a baseline failure, the run is left with only what it was asked about.
    kept, dropped = tester.filter_baseline(report, {legacy[0].signature})
    assert dropped == [legacy[0].test_id]
    assert kept.failures and all("test_pages" in f.test_id for f in kept.failures)
    assert not kept.passed, "the task's own tests are still failing, and still its problem"
