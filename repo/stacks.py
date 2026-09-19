"""What a repository's toolchain is called, in the three places the harness has to know.

Phase 5 gave a Node repository a Node container, and then ran `uv pip install pytest` in it
and stapled `--json-report` onto `npm test`. The image was right and everything around it
was Python. This is the part around it.

Three things differ per stack and nothing else does:

- **the harness install** — what has to be present beyond the repository's own dependencies
  before tests can produce a machine-readable report. Python needs three pip packages; Node
  needs nothing (node 20 ships a test runner and a JUnit reporter); Go needs nothing
  (`gotestsum` is baked into the image).
- **the test command** — how a selector and a report destination are spliced into it.
- **the report format** — pytest's `--json-report` JSON, or JUnit XML for the other two.

## Keyed on the package manager, for the reason `sandbox/select` is

Same key, same argument: `RepoFacts.languages` has a non-deterministic tie-break and holes
where it matters, while `package_manager` is a handful of `is_file()` checks in a fixed
order. A test asserts the two tables agree on which managers they know about, because a
manager that gets a Node *image* and a Python *test command* is worse than one that gets
neither.

## The test command is the harness's, not the repository's

A Node repository's own `npm test` might run jest, vitest, mocha or a shell script, and the
harness cannot parse what it does not recognise. So for Node the harness runs
`node --test` with the built-in JUnit reporter rather than the repository's script.

That is a real limitation and worth stating plainly: a repository whose tests only run
under jest will report zero tests here. The alternative — run `npm test` and parse nothing
— is worse, because `environment_report` then says "no test report produced" and every run
looks broken in the same way whether the tests passed or not.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts import RepoFacts

# Where every stack writes its machine-readable report, relative to the worktree. One path
# so the tool can clear it before a run without knowing which stack it is about to run.
REPORT_DIR = ".autoswe"


@dataclass(frozen=True)
class Stack:
    """How to install, test and read the results for one family of repositories."""

    name: str
    # Appended to the repository's own dependency install. Empty where the image already
    # carries everything the harness needs.
    harness_install: str
    report_rel: str
    report_format: str  # "pytest-json" | "junit"
    # The command template. `{selector}` is the part a caller may narrow, and `{report}` is
    # where the report goes.
    template: str
    default_selector: str
    # The command a repository gets when nothing was detected for it.
    fallback_test: str

    def test_command(self, base: str, selector: str = "") -> str:
        """The command to run, with a selector spliced in.

        `base` is the repository's own detected test command. Python extends it — a repo
        may need `uv run` or a plugin, and its own command carries that. Node and Go
        replace it, because their own command is a script the harness cannot parse.
        """
        chosen = selector.strip() or self.default_selector
        if self.report_format == "pytest-json":
            sel = f" {chosen}" if chosen else ""
            return f"{base}{sel} {self.template.format(report=self.report_rel)}"
        return self.template.format(selector=chosen, report=self.report_rel)


PYTHON = Stack(
    name="python",
    harness_install="uv pip install pytest pytest-json-report pytest-timeout",
    report_rel=f"{REPORT_DIR}/report.json",
    report_format="pytest-json",
    template="--json-report --json-report-file={report} -p no:cacheprovider",
    default_selector="",
    fallback_test="uv run --no-sync pytest -q",
)

NODE = Stack(
    name="node",
    # Nothing. Node 20 ships both the test runner and the JUnit reporter, which is most of
    # why the image pins 20 rather than following the latest.
    harness_install="",
    report_rel=f"{REPORT_DIR}/report.xml",
    report_format="junit",
    # Two reporters, and the second is not decoration. When a test file fails to *load* —
    # a bad import, a syntax error — the JUnit report contains one testcase named after the
    # file whose entire message is "test failed", and the real
    # `ERR_MODULE_NOT_FOUND: Cannot find module …` goes to stdout and nowhere else.
    # Measured. `truncated_output` carries stdout into the report, so adding the spec
    # reporter is the difference between a Debugger that knows what is missing and one
    # told only that something failed.
    template=(
        "node --test --test-reporter=spec --test-reporter-destination=stdout "
        "--test-reporter=junit --test-reporter-destination={report} {selector}"
    ),
    default_selector="test/",
    fallback_test="node --test test/",
)

GO = Stack(
    name="go",
    harness_install="",  # gotestsum is in the image
    report_rel=f"{REPORT_DIR}/report.xml",
    report_format="junit",
    template="gotestsum --junitfile {report} -- {selector}",
    default_selector="./...",
    fallback_test="go test ./...",
)

STACKS: dict[str, Stack] = {
    "uv": PYTHON,
    "poetry": PYTHON,
    "pip": PYTHON,
    "npm": NODE,
    "pnpm": NODE,
    "yarn": NODE,
    "go": GO,
}

BY_NAME: dict[str, Stack] = {s.name: s for s in (PYTHON, NODE, GO)}


def stack_for(facts: RepoFacts | None) -> Stack:
    """The toolchain for this repository, defaulting to Python.

    Python rather than an error, for the same reason the image falls back to it: an agent
    can still read, search and edit a Rust repository, and refusing the run would turn "we
    do not know this stack" into "you cannot use this tool here".
    """
    return STACKS.get((facts.package_manager if facts else None) or "", PYTHON)


def by_name(name: str) -> Stack:
    """A stack from its name, for a `RunContext` that carries the name rather than the
    object — the context is built per step and a string survives being written down."""
    return BY_NAME.get(name, PYTHON)
