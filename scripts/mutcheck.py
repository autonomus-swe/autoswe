#!/usr/bin/env python3
"""Check whether the suite would notice a given change to the source.

This project's standard for "is this tested" is not coverage, it is: **mutate the source
and see whether the suite goes red.** That standard found 43 real gaps across Phase 6, every
one of which produced a plausible, well-formed, wrong result rather than a crash — the
category a green suite is supposed to be evidence against.

Doing it by hand is where the mistakes live, and this project has made all three:

1. **A mutation that does not apply is indistinguishable from one that was caught**, because
   `pytest` exits non-zero for "no such test" too. So the old string must appear exactly
   once, and the file must be observed to change on disk.
2. **`pytest … | tail` reports `tail`'s exit code.** Commands run without a shell, so a
   pipe is a `FileNotFoundError` here rather than a wrong verdict.
3. **A red baseline makes everything look caught.** The baseline runs first and must be
   green, or the result is `RED_BASELINE` rather than a verdict.

A fourth, learned the hard way and not fixable in code: **do not run this while anything
else can write to the tree.** Agents editing source during a sweep produced seven false
`INAPPLICABLE` verdicts. See `docs/test-gaps.md`.

## Usage

    uv run python scripts/mutcheck.py cases.json
    uv run python scripts/mutcheck.py --self-test

where `cases.json` is a list of:

    [{"title": "what breaking this would mean",
      "file": "orchestrator/gc.py",
      "mutation_old": "<verbatim, unique substring of that file>",
      "mutation_new": "<what it becomes>",
      "test_command": "uv run pytest tests/integration/test_gc.py -q"}]

## Reading the result

| verdict | meaning |
|---|---|
| `caught` | the suite went red. This behaviour is tested. |
| `SURVIVED` | **a real gap.** The suite cannot tell the difference. |
| `INAPPLICABLE` | the old string was absent or ambiguous — fix the case, it proves nothing |
| `RED_BASELINE` | the command fails unmutated; nothing can be judged against it |

The file is always restored, including when the test command crashes, and the restore is
asserted rather than assumed.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent


def run(cmd: str) -> int:
    """The command's own exit code, output discarded.

    Split with `shlex` and run without a shell, which is what makes trap 2 impossible
    rather than merely documented: a command containing a pipe cannot be given to
    `subprocess` this way, so it fails loudly here instead of quietly reporting the exit
    code of whatever was on the right-hand side.
    """
    return subprocess.run(  # noqa: S603 — the command is the developer's own test command
        shlex.split(cmd),
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


_BASELINES: dict[str, int] = {}


def baseline_for(cmd: str) -> int:
    """The unmutated exit code, once per distinct command.

    Cached because many cases share a command and the baseline cannot move between them —
    every mutation is reverted before the next is applied, and that revert is asserted.
    """
    if cmd not in _BASELINES:
        _BASELINES[cmd] = run(cmd)
    return _BASELINES[cmd]


def check(case: dict[str, Any]) -> dict[str, Any]:
    target = REPO / case["file"]
    old, new, cmd = case["mutation_old"], case["mutation_new"], case["test_command"]
    out = {"title": case.get("title", "?"), "file": case["file"], "test_command": cmd}

    if not target.is_file():
        return {**out, "result": "ERROR", "detail": "no such file"}

    original = target.read_text()
    occurrences = original.count(old)
    if occurrences != 1:
        return {
            **out,
            "result": "INAPPLICABLE",
            "detail": f"old string appears {occurrences} times, need exactly 1",
        }

    if (code := baseline_for(cmd)) != 0:
        return {**out, "result": "RED_BASELINE", "detail": f"baseline exit {code}"}

    mutated = original.replace(old, new, 1)
    if mutated == original:
        return {**out, "result": "INAPPLICABLE", "detail": "replacement changed nothing"}

    try:
        target.write_text(mutated)
        if target.read_text() != mutated:
            return {**out, "result": "ERROR", "detail": "file did not change on disk"}
        code = run(cmd)
    finally:
        target.write_text(original)
        # Louder than a log line: a half-restored tree poisons every later verdict and the
        # next person to run the suite.
        assert target.read_text() == original, f"FAILED TO RESTORE {target}"

    return {
        **out,
        "result": "SURVIVED" if code == 0 else "caught",
        "detail": f"mutant exit {code}",
    }


SUBJECT = Path("scripts/.mutcheck-selftest-subject.txt")
MARKER = "the-marker-the-command-looks-for"


def self_test() -> int:
    """Prove the harness tells its three outcomes apart, before anyone trusts a verdict.

    Against a disposable subject file rather than against this script: a self-test that
    mutates its own source is a self-test whose subject keeps moving, and the first version
    of this one did exactly that and reported two wrong verdicts about itself.

    Every verdict this tool prints is an argument about whether a test is real. A harness
    that always said `caught` would be indistinguishable from a well-tested project, and one
    that always said `SURVIVED` would send somebody writing tests for behaviour that is
    already covered.
    """
    path = REPO / SUBJECT
    path.write_text(f"{MARKER}\na line the command does not look at\n")
    # Exits 0 only while the marker is present, so removing it is a red suite and editing
    # the other line is a green one.
    cmd = f"grep -q {MARKER} {SUBJECT}"

    cases = [
        {
            "title": "must be caught: break what the command checks",
            "file": str(SUBJECT),
            "mutation_old": MARKER,
            "mutation_new": "something-else",
            "test_command": cmd,
            "expect": "caught",
        },
        {
            "title": "must be SURVIVED: change what the command ignores",
            "file": str(SUBJECT),
            "mutation_old": "a line the command does not look at",
            "mutation_new": "a different line it also does not look at",
            "test_command": cmd,
            "expect": "SURVIVED",
        },
        {
            "title": "must be INAPPLICABLE: an old string that is not there",
            "file": str(SUBJECT),
            "mutation_old": "no such text in the subject",
            "mutation_new": "x",
            "test_command": cmd,
            "expect": "INAPPLICABLE",
        },
        {
            "title": "must be RED_BASELINE: a command that fails unmutated",
            "file": str(SUBJECT),
            "mutation_old": MARKER,
            "mutation_new": "something-else",
            "test_command": "false",
            "expect": "RED_BASELINE",
        },
    ]

    try:
        results = [(c["expect"], check(c)) for c in cases]
    finally:
        path.unlink(missing_ok=True)

    failed = False
    for expect, r in results:
        ok = r["result"] == expect
        failed = failed or not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {r['result']:<14} {r['title']}  ({r['detail']})")

    if failed:
        print("\nSELF-TEST FAILED: the harness does not tell its outcomes apart")
        return 1
    print("\nself-test passed: caught, SURVIVED, INAPPLICABLE and RED_BASELINE all distinguished")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    cases = json.loads(Path(sys.argv[1]).read_text())
    results = [check(c) for c in cases]
    for i, r in enumerate(results, 1):
        print(
            f"[{i}/{len(results)}] {r['result']:<14} {r['title'][:62]}  ({r['detail']})", flush=True
        )

    print("\n=== summary ===")
    for label in ("SURVIVED", "caught", "INAPPLICABLE", "RED_BASELINE", "ERROR"):
        if n := sum(1 for r in results if r["result"] == label):
            print(f"  {label}: {n}")
    if any(r["result"] == "SURVIVED" for r in results):
        print("\nSURVIVED means the suite cannot tell the difference. Those are the gaps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
