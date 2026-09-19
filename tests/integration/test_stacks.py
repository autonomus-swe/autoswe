"""A Node repository and a Go repository, run in their own images, end to end.

The images and the image *selection* landed in Phase 5.9a, and the pipeline around them
did not: `install_command` appended `uv pip install pytest` to every install, and
`test_command` stapled pytest's flags onto whatever it was handed. So a Go repository got
a Go container and was then asked to run

    go test ./... --json-report --json-report-file=.autoswe/report.json

These tests drive the real thing — the real image, the real install, the real test command,
the real report parsed by the real parser — because every part of this is a string that has
to be right in a container, and a unit test of the string proves only that I typed what I
meant to type.

The failing halves matter more than the passing ones. A harness that reports a green run
when the tests failed is worse than one that cannot run them at all.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from contracts import RepoFacts
from orchestrator.nodes import install_command
from repo import stacks
from repo.profile import collect
from sandbox.docker import DockerSandbox
from tools.junit import parse_junit

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
STACKS = {
    "node": ("agent-sandbox:node-20", "node_repo"),
    "go": ("agent-sandbox:go-1.23", "go_repo"),
}


def _image_ready(image: str) -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(image)
        return True
    except Exception:
        return False


def requires(image: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not _image_ready(image), reason=f"{image} unavailable (run `make sandbox-images`)"
    )


async def run_fixture(host_tmp: Path, stack_name: str, *, break_it: str = "") -> tuple[object, str]:
    """Copy a fixture into a workspace, install and test it in its own image.

    Returns `(report, raw output)`. `break_it` replaces a substring in the fixture's source
    so a passing suite becomes a failing one — done by editing the *source*, not the test,
    because that is what a Coder does and what the Debugger then has to read.
    """
    image, folder = STACKS[stack_name]
    workspace = host_tmp / f"ws-{stack_name}"
    shutil.copytree(FIXTURES / folder, workspace)
    if break_it:
        before, after = break_it.split("->")
        for path in workspace.rglob("*"):
            if path.is_file() and before in path.read_text(errors="replace"):
                path.write_text(path.read_text().replace(before, after))
                break

    facts: RepoFacts = collect(workspace)
    stack = stacks.stack_for(facts)
    assert stack.name == stack_name, f"detected {stack.name} for {folder}"

    sandbox = DockerSandbox(
        uuid.uuid4(),
        workspace,
        image=image,
        network="agent-install",
        user=f"{os.getuid()}:{os.getgid()}",
    )
    await sandbox.start()
    try:
        await sandbox.connect_install_network()
        install = await sandbox.exec(install_command(workspace, facts), timeout_s=600)
        assert install.ok, f"install failed: {install.stdout}\n{install.stderr}"
        await sandbox.disconnect_network()

        (workspace / stack.report_rel).parent.mkdir(exist_ok=True)
        cmd = stack.test_command(facts.test_command or stack.fallback_test)
        result = await sandbox.exec(cmd, timeout_s=900)
        output = (result.stdout + "\n" + result.stderr).strip()
        raw = (workspace / stack.report_rel).read_text()
    finally:
        await sandbox.stop(remove=True)
    return parse_junit(raw, cmd, output, worktree=workspace), output


# ---- the happy path ---------------------------------------------------------------------


@requires("agent-sandbox:node-20")
async def test_a_node_repository_installs_and_tests_in_its_own_image(host_tmp: Path) -> None:
    report, _ = await run_fixture(host_tmp, "node")

    assert report.passed, report.failures  # type: ignore[attr-defined]
    assert report.total >= 2  # type: ignore[attr-defined]


@requires("agent-sandbox:go-1.23")
async def test_a_go_repository_installs_and_tests_in_its_own_image(host_tmp: Path) -> None:
    report, _ = await run_fixture(host_tmp, "go")

    assert report.passed, report.failures  # type: ignore[attr-defined]
    assert report.total >= 2  # type: ignore[attr-defined]


# ---- the half that matters --------------------------------------------------------------


@requires("agent-sandbox:node-20")
async def test_a_broken_node_source_is_reported_with_a_file_and_a_line(host_tmp: Path) -> None:
    """A failure the Debugger can act on: which file, which line, and what it said. A
    report that says only "a test failed" sends it reading the tree to find out."""
    report, _ = await run_fixture(host_tmp, "node", break_it="a - b->a + b")

    assert not report.passed, "breaking the source did not fail the suite"  # type: ignore[attr-defined]
    failure = report.failures[0]  # type: ignore[attr-defined]
    top = next((f for f in failure.frames if f.in_repo), None)
    assert top is not None, f"no frame in the repository: {failure.frames}"
    assert top.file.endswith(".js") and top.line > 0
    assert top.code, "and the source line itself, which the prompt quotes"


@requires("agent-sandbox:go-1.23")
async def test_a_broken_go_source_is_reported_with_a_file_and_a_line(host_tmp: Path) -> None:
    """Go names the file by basename only and puts the package in `classname`, so this is
    also the test that the basename is resolved against the tree."""
    report, _ = await run_fixture(host_tmp, "go", break_it="return a - b->return a + b")

    assert not report.passed, "breaking the source did not fail the suite"  # type: ignore[attr-defined]
    failure = report.failures[0]  # type: ignore[attr-defined]
    top = next((f for f in failure.frames if f.in_repo), None)
    assert top is not None, f"no frame in the repository: {failure.frames}"
    assert top.file.endswith("_test.go") and "/" in top.file, "resolved to a path, not a basename"
    assert top.line > 0


# ---- the install is the stack's -----------------------------------------------------------


@pytest.mark.parametrize("folder", ["node_repo", "go_repo"])
def test_a_non_python_install_does_not_reach_for_pip(folder: str) -> None:
    """The bug this file exists for. `uv pip install pytest …` was appended to every
    install command, so a Node container was asked for a Python package."""
    facts = collect(FIXTURES / folder)

    command = install_command(FIXTURES / folder, facts)

    assert "uv pip install" not in command and "pytest" not in command, command


def test_a_python_install_still_brings_the_harness() -> None:
    """The control: the Python path is unchanged, and the harness packages are exactly why
    `run_tests` can parse anything at all."""
    facts = collect(FIXTURES / "fixture_repo")

    command = install_command(FIXTURES / "fixture_repo", facts)

    assert command.endswith(stacks.PYTHON.harness_install)
