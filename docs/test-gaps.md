# Verified test gaps

**All 43 of the Phase 6 sweep are closed, and so is the one fix that was deferred.** The
method has since been pointed at the rest of the codebase; what it has found so far is in
"Beyond Phase 6" below. This file is kept as the
record of how they were found and what each one was, because the method is reusable and the
list is the evidence that the closing tests are about something.

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

## The one fix that was reverted, now made properly

Writing `test_a_verify_command_that_hangs_is_killed_rather_than_waited_on` — the first test
ever to reach that branch — surfaced a warning: `proc.kill()` sends the signal and returns,
so the child is left unreaped and its transport is finalised after the event loop has
closed, raising "Event loop is closed" out of `__del__`. Once per timed-out task, from a
place with nothing to do with the task.

The first attempt at `await proc.wait()` **broke the test**, and it was reverted rather than
patched further. That was the right call on the evidence available and the wrong diagnosis.
Probing the three shapes in isolation settled it:

| shape | result |
|---|---|
| `kill()` alone | leaks the transport — reproduces the warning |
| `kill()` + `await wait()` | clean |
| `kill()` + `await communicate()` | clean |

So the fix was correct all along. What broke was **the test's own outer
`asyncio.wait_for`**: an inner `wait_for` that times out cancels the current task, and
awaiting anything in the resulting `except` block while an outer `wait_for` is still active
lets that cancellation be caught by the outer scope and re-raised as its `TimeoutError`.
Production never nests — `run_task` awaits the verifier directly and `run_suite` uses
`gather` — so the nesting existed only in the test, as belt-and-braces that turned a correct
fix into a red test.

**And the first assertion written for it did not work.** Marking the test
`filterwarnings("error::pytest.PytestUnraisableExceptionWarning")` looked like it pinned the
fix and does not: the unraisable is reported at collection time, usually after the test that
caused it, so removing the reap **still passed**. Measured, not assumed — the mutation
survived. The test now asserts the process's `returncode` directly, which is the same fact
available synchronously: `None` until reaped, and negative when death was by signal. Both
mutations — dropping the reap, and dropping the kill — are caught.

## Beyond Phase 6: the clone sweep

The same method, applied to the modules the Phase 6 sweep did not cover. The first thin spot
it found was `orchestrator/gc.py` at 67 %, and within it one function with **no test at all**.

`tests/integration/test_gc.py` has nine tests and covers the container and worktree sweeps
thoroughly. **`sweep_clones` appears in none of them**, and four mutations survived:

| mutation | consequence |
|---|---|
| the attached-worktree guard → never | **a clone with live worktrees attached is deleted** |
| the young-clone guard → never | a clone fetched into minutes ago is deleted |
| the unreadable-clone skip → fall through | a clone is deleted *because* its state could not be read |
| `ttl_days * 24 * 3600` → `ttl_days` | the TTL becomes seconds, so everything is ancient |

The first is the serious one, and the module's own docstring says why: a clone is "the
expensive thing to rebuild … and it is shared by every run against that repository, so age
alone is not enough". Deleting one with worktrees attached takes the git objects out from
under every live run on that repository at once — the worktree directories survive, and
every git command inside them fails afterwards with something that names neither the clone
nor the collector.

The third is a shape worth naming on its own: **an error reading the state became permission
to delete.** That is the opposite of what an unreadable thing deserves, and it is one
`continue`.

Closed by `tests/unit/test_gc_clone_sweep.py`, against real `git` repositories in `tmp_path`
— the guard's whole question is what `git worktree list` says, so a fake would be asserting
the fake. Unit rather than integration because `sweep_clones` touches no database.

## The security-claim audit: nine gaps, and four miscitations

`docs/security.md` says "a guarantee with no test beside it is a hope". Every claim in it was
mutation-tested. Most held — all seven sandbox-hardening claims, both command claims, all
three path-containment guards, the budget ledger. **Nine did not**, and each is now closed:

| § | mutation that survived | what it meant |
|---|---|---|
| 2 | the install network created with `internal=False` | the sandbox gets a default route around the proxy |
| 2 | SETUP's `has_network()` guard removed | the Coder starts with network access and nothing says so |
| 7 | the container handed the whole host environment | every key the worker holds, inside the sandbox |
| 7 | `not environ.get(name)` → `name not in environ` | a **set but empty** `${VAR}` becomes an empty credential |
| 8 | the answer route losing its auth dependency | one write route open to anyone |
| 8 | `parsed.scheme != "https"` → `False` | `http://` repositories accepted |
| 8 | the event stream losing its query-parameter key | the documented exception, untested |
| 8 | reads and writes sharing one rate-limit bucket | **a single GET refills the write allowance** |
| 9 | `state.waiting_s += seconds` → `pass` | the four-hour approval wait charged to the run's clock |

Two are worth dwelling on.

**The shared rate-limit bucket survived both cited tests**, because neither interleaves a read
with a write burst: one does 20 reads then a write (the Lua clamps, so it passes), the other
does 6 writes with no reads (so it passes). The real effect is worse than a merged limit — one
GET between writes tops the write allowance back up, so the write limit stops limiting. The
new test exhausts the write bucket, makes reads, and requires the bucket to be *still*
exhausted.

**The guard for SETUP's network isolation was tested in `resume.py` and not in `nodes.py`.**
The resume twin has a test precisely because that same mutation survived there once. The
SETUP one — the path every first-attempt run takes — did not.

### Four rows cited the wrong thing

Separate from the gaps, and fixed in the same pass. Each was checked directly rather than
taken from a report:

- **§7 located `expand` in `mcp_bridge/config.py`.** It is in `core/envsubst.py`, shared with
  `evals/suite.py` — a reader looking where the doc points would not find it.
- **§7's "caught in CI" cited `.pre-commit-config.yaml`**, which is a *local* hook CI never
  runs. The CI gate is the `gitleaks` job in `.github/workflows/ci.yml`. Both are real; they
  are different claims and now have different rows.
- **§6's rejection row cited a test that asserts the `Decision` object**, not the string the
  model reads as the tool result — which is what the claim says. That string is built in
  `orchestrator/hooks.py`, and an uncited test holds it.
- **§3's "ordinary commands are not refused" cited a test that only calls `check_bash`** —
  the DENY list. The ASK half was held by an uncited integration test.

### What is recorded rather than closed

- **`proxy/allowlist.txt` losing a `$` anchor is untested**, and the file's own header calls
  that "the whole allow-list defeated by anyone who can register a domain". It is untestable
  by mutation alone: the file is baked into the proxy image, so a mutation never reaches the
  running container unless the image is rebuilt first. Closing it needs an image-rebuild step
  in the test, which is a bigger change than this pass.
- **`.pre-commit-config.yaml` and the CI workflow cannot be held by pytest at all.** No test
  can notice a disabled hook. The doc now names them as config rather than implying a test.
- **§8's "every route requires a key" is literally false** for `/healthz`, `/metrics`, `/`,
  `/ui/*`, `/openapi.json` and `/docs`. All defensible, none previously written down. The new
  structural test classifies each with its reason, so the exceptions are a list somebody chose
  rather than a slogan nobody checked.

## Repeating this

`docs/numbers.md` is the standing argument for why: this project quotes numbers about
itself, and a measurement tool nobody tests is a number nobody can trust. The sweep is worth
re-running whenever a module's coverage drops or a new surface is added — the finding step
is cheap, and the verification step is the only part that has to be careful.

The rule that makes it worth anything: **a mutation must be confirmed to have applied,
against a green baseline, with the exit code read directly rather than through a pipe.** All
three of those have caught this project out before.

`scripts/mutcheck.py` is that rule as a tool, because doing it by hand is where the mistakes
live and this sweep lost its scratch copy to `/tmp` twice. It enforces all three — the old
string must appear exactly once and the file must be observed to change; commands run
without a shell, so a pipe is an error rather than a wrong verdict; and the baseline runs
first and must be green. `--self-test` proves it distinguishes `caught`, `SURVIVED`,
`INAPPLICABLE` and `RED_BASELINE` against a disposable subject, which is worth having
because a harness that always said `caught` would be indistinguishable from a well-tested
project.
