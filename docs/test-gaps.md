# Verified test gaps

**All 43 are closed.** This file is kept as the record of how they were found and what each
one was, because the method is reusable and the list is the evidence that the closing tests
are about something.

Not a wish list, and never was. Every entry was **proved** by mutating the source and
watching the suite stay green, with the mutation confirmed to have applied against a green
baseline — because a mutation that fails to apply is indistinguishable from a test that
caught it (`pytest` exits non-zero for "no such test" too).

## How they were found

A coverage run over the Phase 6 modules, then one agent per module reading the uncovered
lines and proposing an exact mutation for each thing a real defect could hide behind, then
every proposal executed.

**47 proposed → 43 survived.** The other 4 could not be judged at all: `evals/scale.py` had
no test file, so every mutation in it survives trivially.

## How they were closed

Four passes, each test re-run through the harness after it was written:

| pass | tests | closed |
|---|---|---|
| the MCP and eval surfaces | `test_mcp_tool_bodies_are_reached.py`, `test_task_fields_reach_the_run.py`, additions to `test_mcp_workspace.py` | 14 |
| the published numbers | `test_evals_scale.py`, `test_evals_verifier.py` | 8 |
| the control plane and mounted tools | `test_api_service.py`, `test_mounted_tool_call_site.py` | 9 |
| the reporting, benchmark, config and resume paths | additions to `test_evals_report.py`, `test_swebench.py`, `test_mcp_config.py`; `test_resume_reattach.py` | 12 |

## What the 43 had in common

Almost all of them were one shape: **a value is produced and the place that consumes it is
not tested.** The helper has a test; the call site does not. Phase 6 shipped five instances
of it before this sweep — `upstream` in the PR node, `starting_commit`, the ablation arm,
the `done` gate in swebench, `wall_clock_s` — and the sweep found thirty-odd more.

Two sub-shapes worth naming, because both defeat a test that looks correct:

**A value equal to its own default proves nothing.** The eval harness's shared `task()`
helper built `base="main"`, which is `Task.base`'s default — so an assertion on
`base_branch == "main"` passed against a `create` that never sent the field at all.
`test_no_asked_value_equals_its_own_default` now enforces the rule rather than trusting
anyone to remember it.

**A fixture where both branches agree measures nothing.** `checkout_and_verify` is the
definition of "resolved", and dropping `--branch` from its clone survived — because the
fixture repository's branches were never made to disagree. The test now uses a repository
whose `main` exits 7 and whose agent branch exits 0.

## The ones that would have been worst

- **`return proc.returncode or 0` → `return 0`** in the eval verifier: *everything*
  resolves, including tasks whose tests failed, and a suite reporting 3/3 looks identical
  either way.
- **`_pending`'s `raise Conflict` → `pass`** in the control plane: an approval accepted by a
  run that is not waiting for one — the replay the module was written to prevent.
- **`base_sha or base_branch` → `base_branch`** on the resume path: a pinned run resumes
  against the head of a branch, produces a patch against a tree the instance never named,
  and nothing errors.
- **`_tokens`'s divisor** in the scale tool: a map over its 4 000-token budget reports as
  under it. `docs/numbers.md` opens by admitting that exact thing happened once already.
- **`engine=None`** at the MCP workspace call site: semantic search silently degrades to
  text search and still answers.

None of these crash. That is the whole point: every one produces a plausible, well-formed,
wrong result, which is the category of defect a green suite is supposed to be evidence
against.

## A note on how nearly this went wrong

The first pass of this verification ran the mutation harness **while the analysis agents
were still working in the same checkout**. Three of them edited source files. That
contaminated the run: 7 candidates came back `INAPPLICABLE` purely because an agent had
changed the line the mutation was supposed to match.

The whole thing was re-run on a quiet tree. Comparing the two: 7 verdicts changed, **all of
them `INAPPLICABLE` → `SURVIVED`**, and **no verdict ever flipped between `SURVIVED` and
`caught`**. So the contamination only ever failed conservatively and never hid a caught
mutation — but that was luck, not design. Do not run a mutation harness concurrently with
agents that can write to the same tree.

## One fix attempted and reverted

Writing `test_a_verify_command_that_hangs_is_killed_rather_than_waited_on` — the first test
ever to reach that branch — surfaced a `PytestUnraisableExceptionWarning`: `proc.kill()`
sends the signal and never reaps the process, so the transport is finalised after the event
loop has closed and raises "Event loop is closed" out of `__del__`.

Adding `await proc.wait()` after the kill **broke the test**: `asyncio.wait_for` has already
cancelled `communicate()`, and awaiting in the handler re-raises `CancelledError`. Reverted
rather than patched further — the symptom is a warning on a timed-out task, not a leak (the
process is killed either way), and a correct fix is a deliberate change to that error path
rather than something to bolt onto a testing change.

**This is the one thing in this document still open.** It is located and understood rather
than mysterious, which is the most a warning of this size deserves.

## Repeating this

`docs/numbers.md` is the standing argument for why: this project quotes numbers about
itself, and a measurement tool nobody tests is a number nobody can trust. The sweep is worth
re-running whenever a module's coverage drops or a new surface is added — the finding step
is cheap, and the verification step is the only part that has to be careful.

The rule that makes it worth anything: **a mutation must be confirmed to have applied,
against a green baseline, with the exit code read directly rather than through a pipe.**
All three of those have caught this project out before.
