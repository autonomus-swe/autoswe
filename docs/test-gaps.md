# Verified test gaps

Not a wish list. Every entry below was **proved** by mutating the source and watching the
suite stay green, with the mutation confirmed to have applied against a green baseline —
because a mutation that fails to apply is indistinguishable from a test that caught it
(`pytest` exits non-zero for "no such test" too).

**How this was produced.** A coverage run over the Phase 6 modules, then one agent per
module reading the uncovered lines and proposing an exact mutation for each thing a real
defect could hide behind, then every proposal executed. 47 proposed, **43 survived**, 4
could not be judged because the module has no test file at all.

**22 are now closed**, in two passes, and each was re-run through the harness after its
test was written:

| pass | tests | closed |
|---|---|---|
| the MCP and eval surfaces | `test_mcp_tool_bodies_are_reached.py`, `test_task_fields_reach_the_run.py`, two additions to `test_mcp_workspace.py` | 14 |
| the published numbers | `test_evals_scale.py` (new file), `test_evals_verifier.py` (new file) | 8 |

The rest are recorded here rather than fixed, because one reviewable change cannot hold 43
tests and a list that lives in a chat log is a list that is gone.

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

## Still open

### `api/service.py` — 5 open

- answer()'s tool_call_id never reaches the inbox in any test
- _pending()'s awaiting_input guard is never exercised (stale-pending approval)
- list_runs upper clamp is asserted only by a 200 status code
- The arq-unavailable degraded path is never taken by any test
- An answer's `tool_call_id` is put in the inbox message by nothing that is tested

### `evals/report.py` — 4 open

- _why's empty-error guard is never exercised: a mixed suite (some rows errored, some clean) is untested
- Cost totals over unverifiable tasks are unasserted: spend on crashed tasks can vanish from the report
- main()'s --by grouping has no call site test: the documented ablation comparison can blend all arms into one
- conditions(note=...) is never passed a note, so the caveat can be dropped silently

### `evals/swebench.py` — 4 open

- --limit is never checked: load_instances could load the whole split
- main() never reaches predict in any test: --provider can be dropped
- The problem-statement truncation length is asserted by nothing
- write() is only ever tested into an existing directory

### `mcp_bridge/client.py` — 4 open

- MountedTool.run is never invoked — the mounted-tool call site is untested
- Session is not dropped on a transport failure — the reconnection path is untested
- MAX_RESULT_CHARS truncation never fires in any test
- structured_content fallback is computed and never checked

### `mcp_bridge/config.py` — 4 open

- Only command[0] is ever asserted — the rest of argv can be dropped silently
- Configured timeout_s is parsed and never observed by any test
- command element type guard (line 119) can be disarmed invisibly — str() coerces behind it
- env key/value string check (line 175): half of it can go dead via and/or swap

### `orchestrator/resume.py` — 4 open

- Resume's worktree-recreate branch passes a committish nothing checks
- Post-resume network-isolation guard can be demoted with the suite green
- Nothing asserts a resumed run keeps holding the repo lock
- CPU raise/restore around the resumed install is never exercised

### `evals/scale.py` — closed

**This module had no test file at all** (0 % covered, 67 statements), so every mutation in it
survived trivially and the four below could not even be judged. `tests/unit/test_evals_scale.py`
now exists and all four are caught:

- goal dropped at the render_symbol_map call site (silent keyword default)
- index_s timing window can collapse to ~0 with no cross-check
- _tokens is an untested re-implementation of the budget divisor
- _count_files counts .git internals, so files_total means two things in one JSONL

It earned that test file more than its coverage number suggested, because `docs/numbers.md`
quotes its output. Two findings came out of writing them:

- **`_tokens` is the inverse of the map's own budget arithmetic**, and only stays so if the
  two move together. A divisor wrong in the generous direction reports an over-budget map as
  under it — which `docs/numbers.md` opens by admitting happened once. The test changes
  `repomap.CHARS_PER_TOKEN` and requires `_tokens` to follow, rather than asserting the
  current value, because asserting the value passes on a frozen copy.
- **`files_total` counts `.git`.** It is files on disk, not files in the project. Pinned by a
  test and now stated beside the table in `docs/numbers.md` rather than quietly changed —
  redefining the measurement would invalidate every figure already published while the
  numbers kept looking comparable.

## One fix attempted and reverted

Writing `test_a_verify_command_that_hangs_is_killed_rather_than_waited_on` — the first test
ever to reach that branch — surfaced a `PytestUnraisableExceptionWarning`: `proc.kill()`
sends the signal and never reaps the process, so the transport is finalised after the event
loop has closed and raises "Event loop is closed" out of `__del__`.

Adding `await proc.wait()` after the kill **broke the test**: `asyncio.wait_for` has already
cancelled `communicate()`, and awaiting in the handler re-raises `CancelledError`. Reverted
rather than patched further — the symptom is a warning on a timed-out task, not a leak (the
process is killed either way), and a correct fix is a deliberate change to that error path
rather than something to bolt onto a testing change. Recorded here so it is a known, located
issue instead of an unexplained warning in the suite output.
