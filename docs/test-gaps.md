# Verified test gaps

Not a wish list. Every entry below was **proved** by mutating the source and watching the
suite stay green, with the mutation confirmed to have applied against a green baseline —
because a mutation that fails to apply is indistinguishable from a test that caught it
(`pytest` exits non-zero for "no such test" too).

**How this was produced.** A coverage run over the Phase 6 modules, then one agent per
module reading the uncovered lines and proposing an exact mutation for each thing a real
defect could hide behind, then every proposal executed. 47 proposed, **43 survived**, 4
could not be judged because the module has no test file at all.

**14 are now closed** — see `tests/unit/test_mcp_tool_bodies_are_reached.py`,
`tests/unit/test_task_fields_reach_the_run.py` and the two additions to
`tests/unit/test_mcp_workspace.py`. Each of the 14 was re-run after the tests were written
and is now caught. The rest are recorded here rather than fixed, because one reviewable
change cannot hold 43 tests and a list that lives in a chat log is a list that is gone.

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
- answer()'s tool_call_id never reaches the inbox in any test- _pending()'s awaiting_input guard is never exercised (stale-pending approval)- list_runs upper clamp is asserted only by a 200 status code- The arq-unavailable degraded path is never taken by any test- An answer's `tool_call_id` is put in the inbox message by nothing that is tested
### `evals/report.py` — 4 open
- _why's empty-error guard is never exercised: a mixed suite (some rows errored, some clean) is untested- Cost totals over unverifiable tasks are unasserted: spend on crashed tasks can vanish from the report- main()'s --by grouping has no call site test: the documented ablation comparison can blend all arms into one- conditions(note=...) is never passed a note, so the caveat can be dropped silently
### `evals/run.py` — 4 open
- checkout_and_verify clones without --branch: the verifier can measure the wrong code and nothing notices- Client.cancel's real HTTP call is untested and swallows every failure, so a timed-out run can leak its worker slot forever- The verify process's exit code is never asserted against a real subprocess, so the harness can report 100% resolved- --ablate is never proven to reach run_suite, so an ablation arm can silently run as the baseline
### `evals/swebench.py` — 4 open
- --limit is never checked: load_instances could load the whole split- main() never reaches predict in any test: --provider can be dropped- The problem-statement truncation length is asserted by nothing- write() is only ever tested into an existing directory
### `mcp_bridge/client.py` — 4 open
- MountedTool.run is never invoked — the mounted-tool call site is untested- Session is not dropped on a transport failure — the reconnection path is untested- MAX_RESULT_CHARS truncation never fires in any test- structured_content fallback is computed and never checked
### `mcp_bridge/config.py` — 4 open
- Only command[0] is ever asserted — the rest of argv can be dropped silently- Configured timeout_s is parsed and never observed by any test- command element type guard (line 119) can be disarmed invisibly — str() coerces behind it- env key/value string check (line 175): half of it can go dead via and/or swap
### `orchestrator/resume.py` — 4 open
- Resume's worktree-recreate branch passes a committish nothing checks- Post-resume network-isolation guard can be demoted with the suite green- Nothing asserts a resumed run keeps holding the repo lock- CPU raise/restore around the resumed install is never exercised

### `evals/scale.py` — 4 unjudgeable

**This module has no test file at all** (0 % covered, 67 statements). Every mutation in it
survives trivially, so the four candidates below are recorded rather than counted:

- goal dropped at the render_symbol_map call site (silent keyword default)
- index_s timing window can collapse to ~0 with no cross-check
- _tokens is an untested re-implementation of the budget divisor
- _count_files counts .git internals, so files_total means two things in one JSONL

It earns a test file more than its coverage number suggests: `docs/numbers.md` quotes its
output. `_tokens` is an independent re-implementation of the budget divisor, and
`_count_files` counts `.git` internals — so `files_total` means two different things
depending on which row you read.
