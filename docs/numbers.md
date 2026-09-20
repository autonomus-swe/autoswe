# Numbers

What this project costs, measured rather than estimated. Every figure here came from
`evals/scale.py` or from a run that wrote a row to `evals/results/`.

Where a number is missing the row says so, and where a criterion is **not** met this file
says that too. A previous version of this document reported the map as meeting its budget
when it did not — see "the calibration was wrong" below.

## Repository intelligence, measured

`uv run python -m evals.scale <checkout>` — no model, no key, no network.

| | django | sympy | pydantic |
|---|---|---|---|
| Files in the checkout | **7 120** | 2 122 | 879 |
| Files with symbols (what the map ranks) | 2 049 | 1 393 | 427 |
| Symbols | 44 292 | 40 595 | 14 161 |
| **Parse** | **9.9 s** | 22.8–25.4 s | 6.7 s |
| Graph + PageRank | 2.9 s | 10.2–15.3 s | 1.5 s |
| **Map tokens (v2), real** | **3 079** | 3 453 | 3 084 |
| Map tokens (v1 tree) | 1 220 | 1 275 | 1 258 |
| Embedding chunks | 50 346 | 47 755 | 14 675 |
| Embeddings (hash provider) | 17.5 s | 31.9–41.4 s | 6.8 s |

Machine: 12 CPUs, 38 GiB, 8 parse workers. "Real" map tokens are counted with a BPE
tokenizer, not estimated — see the calibration note below. Two sympy runs are recorded
because the rate moves a lot with load; django was measured at load 9.5 and is still the
fastest of the three, because its Python files are smaller on average.

### Two honest caveats on the parse figure

- **It is parse only.** `index_repo` also writes the rows, and `evals/scale.py` does not
  time that. On sympy's 40 595 symbols the insert is several seconds more.
- **It is measured host-side**, with `python -m evals.scale`. The criterion says "on the
  worker". The work is the same code either way, but nothing here has measured it inside a
  worker process.

## Against the phase's exit criteria

Audited by agents told to disprove each one, not to confirm it.

| Criterion | Verdict |
|---|---|
| Index a 3 000-file repo under 60 s; same-SHA re-run is a no-op | **met** — measured on 7 120 files |
| Repo map ranks the named function's file first; under 4 000 tokens | **met**, after the calibration fix |
| `search_code(semantic=True)` on a fixture auth service | **met** — real embedding model, with a lexical control |
| Cache hit rate above 60 % on a real run | **met** — 0.832 run-level over 34 calls, 5 roles |
| Compaction blocks preserved (`provider.compacted`) | **not met** — needs the Anthropic provider |
| Budget downgrade table | **met** — a downgrade reaches a different model that really answers |
| OTel spans and `/metrics` | **met** |
| Node/Go repos end to end; egress allow/deny | **met** — both halves, tested in the real images |
| Scale run, PR opened, under $5 | **not met** — needs quota |

### Indexing: met

Both halves, and neither is an extrapolation any more.

**Under 60 s**: django, 7 120 files and 44 292 symbols, parsed in **9.9 s** — more than
twice the repository size the criterion asks for, and six times inside its budget. An
earlier version of this document extrapolated from sympy at 2 122 files; measuring a real
one was cheaper than defending the arithmetic.

**The same-SHA no-op** is met and now actually tested. The test named for it only exercised
the storage predicate, so deleting the reuse check left the suite green; there is now one
that drives `index_repo` twice and checks both the skip and the row count, verified to fail
when the check is removed. (`repo_symbols` has no unique constraint, so a regression there
would silently double the table rather than error.)

### Semantic search: met

The criterion is specific — *"where is rate limiting handled" returns the right file on the
fixture auth service* — and the fixture is built so that only meaning can answer it:
`app/auth/bucket.py` implements a token bucket and contains neither "rate" nor "limit".
A test asserts that, so the query cannot quietly become a keyword match.

Run against `nomic-embed-text` through Ollama. No key and no quota: an embedding is one
forward pass per chunk rather than a generation loop, which is why this criterion is
reachable on a laptop while the two below are not.

**The control is the point.** The same query against `HashProvider` scores **0.0 against
every chunk** — it shares no word with anything in the repository. A semantic test that a
lexical index also passes has measured nothing.

### Prompt caching: the reads are measured

Three turns against a local server that reports `prompt_tokens_details.cached_tokens`,
through our own provider rather than beside it:

| Turn | Input tokens billed | Cache read | Hit rate |
|---|---|---|---|
| 1 | 2 678 | 5 | 0.002 |
| 2 | **1** | **2 682** | **1.000** |
| 3 | 1 | 2 682 | 1.000 |

The first turn writes the prefix and the rest read it, which is the shape the criterion
describes. The 60 % threshold is a *run-level* figure and this is three turns, so it is
evidence for the mechanism and not a claim about a run — see below.

**The control is again the point.** Moving `Run 7f3a started at 12:04. ` to the front of the
same system prompt collapses the hit rate, because a cache prefix is a prefix: one varying
character at the start invalidates everything behind it. That is the whole reason
`build_system` puts the run block *after* the stable text, and the test fails if it is moved
back. A caching test with no invalidation control passes just as well on a build with no
caching at all.

### The caching criterion, met on a real run

34 calls, five roles, through SETUP, ANALYZE, PLAN, DECOMPOSE and the whole CODE / TEST /
DEBUG loop, against the fixture repository on a local model:

| Role | Calls | Input | Cache read | Rate |
|---|---|---|---|---|
| analyzer | 6 | 5 066 | 8 015 | 0.613 |
| planner | 4 | 1 514 | 4 581 | 0.752 |
| decomposer | 1 | 219 | 601 | 0.733 |
| **coder** | **16** | 3 193 | 33 845 | **0.914** |
| debugger | 7 | 3 159 | 18 175 | 0.852 |
| **run level** | **34** | **13 151** | **65 217** | **0.832** |

**0.832 against a criterion of 0.60.**

The shape is the one predicted two entries above, and it is worth pointing at because the
prediction was made while the number was *failing*: at seven calls the run-level figure was
0.559, below the threshold, because each role pays its own opening cache write and nothing
had yet amortised them. The Coder loop is what does it — 16 calls sharing one prefix, at
0.914 — and it drags the whole run from 0.559 to 0.832.

That is the argument for putting the repo map, facts and profile in the cached prefix
rather than in the messages, stated as a measurement instead of a design intention. The
roles that make one or two calls never do better than ~0.7; the role that loops does 0.914,
and it is the role that spends the most.

### A run that finished

A second run, same fixture repository and goal, against `poolside/laguna-s-2.1:free` on
OpenRouter. It completed the whole pipeline:

    setup -> analyze -> plan -> decompose -> code -> test -> code -> test
          -> review -> security -> pr

**42 calls, about 16 minutes, $0.00, and it reached the PR phase and opened one.** There is
no DEBUG phase in that sequence: the Coder got the fixture's tests green on its second
attempt without the Debugger being called at all. The local 7B model made 40 Debugger calls
on the same goal and never landed it.

That settles what the local run could not: the pipeline works end to end, and the
milestone's demo line — repository URL plus an English goal to an opened pull request — is
real rather than aspirational.

| | local qwen2.5:7b | hosted laguna-s-2.1 |
|---|---|---|
| Calls | 69+ (never finished) | **42** |
| Wall clock | 183 min, unfinished | **~16 min, complete** |
| Debugger calls | 40 | **0** |
| Reached | DEBUG loop on task 1 | **PR** |
| Cache hit rate | **0.832** | 0.065 |

**The two cache figures are the same finding twice.** The free hosted model does not
implement prompt caching — measured directly, `cached_tokens: 0` on an identical 3 322-token
prefix — so its 0.065 says nothing about the breakpoint design and everything about the
model. The caching criterion rests on the local run, which caches properly, and the
completion criterion rests on the hosted one, which writes working code. Neither model does
both, and no single run here can show both at once. Worth saying plainly rather than
quoting whichever number suits.

### What a real run actually spent

The same run, measured whole. 67 calls over 183 minutes against a three-file fixture
repository, on qwen2.5:7b locally:

| Role | Calls | Input | Cache read | Output | Rate | Median latency |
|---|---|---|---|---|---|---|
| analyzer | 6 | 5 066 | 8 015 | 919 | 0.613 | 728 s |
| planner | 4 | 1 514 | 4 581 | 577 | 0.752 | 319 s |
| decomposer | 1 | 219 | 601 | 136 | 0.733 | 43 s |
| coder | 16 | 3 193 | 33 845 | 982 | 0.914 | 30 s |
| debugger | **40** | 35 717 | 99 394 | 6 557 | 0.736 | 52 s |

Totals: **45 709 input, 146 436 cache read, 9 171 output**. Cost **$0.00** — the model is
local, so the scale run's `$5` ceiling is met trivially and means nothing here.

**Two things in that table are worth more than the totals.**

*The latency spread was environmental.* Median 48 s, minimum 9 s, maximum 1 124 s. Earlier
entries in this document quoted 439 s and 823 s as though they were the machine's speed;
they were the machine's speed while a second model was resident and competing. The
Analyzer's 728 s median is the same artefact — it ran during the contended window. Once
that cleared, the Coder's median was 30 s. A number measured on a contended host describes
the host, not the system.

*The Debugger made 40 of 67 calls.* Four CODE phases, five TEST phases, four DEBUG phases
and three escalations, across two tasks (`t1`, then a replanned `t1.1`) — the run never
reached PR. That is not a caching or orchestration failure; every one of those transitions
is the state machine doing what it is for, bounding a model that cannot get the fixture
green. It is what a 7B local model costs: the loop works, the code does not land.

This is the honest read on "can a local model drive this system". It can drive it, produce
a valid plan, a task graph, edits, test runs and a debug cycle — and measure prompt caching
properly while doing so. What it cannot do is finish.

### The same mechanism, one step at a time

The table above is three turns of one synthetic prompt. This is the Analyzer step of an
actual run against the fixture repository, read from the `llm_calls` ledger:

| Call | Input | Cache read | Rate |
|---|---|---|---|
| 1 | 1 398 | 0 | 0.000 |
| 2 | 243 | 1 412 | **0.853** |
| 3 | 155 | 1 814 | **0.921** |

The run has since reached PLAN, and the fuller picture is less flattering than those first
three calls suggested — which is the point of measuring:

| # | Role | Input | Cache read | Rate | Cumulative |
|---|---|---|---|---|---|
| 1 | analyzer | 1 398 | 0 | 0.000 | 0.000 |
| 2 | analyzer | 243 | 1 412 | 0.853 | 0.462 |
| 3 | analyzer | 155 | 1 814 | 0.921 | 0.642 |
| 4 | analyzer | 358 | 2 082 | 0.853 | 0.711 |
| 5 | analyzer | 48 | 2 707 | 0.983 | 0.784 |
| 6 | analyzer | 2 864 | 0 | 0.000 | 0.613 |
| 7 | planner | 1 271 | 6 | 0.005 | **0.559** |

Per role: analyzer **0.613** over 6 calls, planner 0.005 over 1.

**The run-level figure is currently 0.559 — below the 60 % the criterion asks for.** It is
recorded here rather than waited out, because the shape is exactly what was predicted one
section above and is worth stating while it is inconvenient: every role carries a different
system prompt, so every role pays its own opening cache write, and a run of many short
steps sits lower than one dominated by a long Coder loop. Whether this run clears 60 %
depends on the Coder loop amortising those writes, which has not happened yet.

**Call 6 lost its cache entirely**, mid-step, after five turns above 0.85. That is not a
moving prefix on our side: `cleared_tool_results` is never logged in this run, so context
trimming did not fire, and the system prompt and run block are fixed for a run by
construction. The likeliest explanation is server-side eviction — another model was
repeatedly loading on this host, and Ollama discards a model's KV cache when it unloads it.
Stated as likely rather than proven: nothing here measures the server's eviction directly.
It does mean the figures above are depressed by the environment rather than by the design,
and that a contended host is a poor place to certify a caching criterion.

What it does settle is the half that used to rest only on a synthetic test: in the real
agent loop, with a real repo map and real tool definitions in the prefix, reads start at
turn 2 and the prefix holds still across turns.

### Budget downgrade: measured against a server that says which model answered

A unit test can show `model_for("sonnet") != model_for("opus")` and still be consistent with
a deployment where both resolve to the same model and the saving is imaginary. So the
downgrade is driven against two real local models and checked against the `model` field the
server echoes: a downgraded Planner is served by the smaller one, Coder and Debugger are
served by the larger one under the same pressure, and a single-model deployment reports an
empty `downgraded` list.

These are a 7B and a 3B, not an expensive and a cheap hosted model — the wrong kind of
difference, and the numbers here are about resolution rather than dollars. What it rules out
is the failure that matters: reporting a downgrade that never reached a different model.

### The calibration was wrong, and the map criterion was not met

`CHARS_PER_TOKEN` was **4**, the rule of thumb for English prose. Code is much denser.
Measured with a BPE tokenizer over the rendered map: **2.69 chars/token on sympy, 2.90 on
pydantic**.

So a map "under its 3 500-token budget" was really **5 193 tokens on sympy** — the 4 000
criterion missed by a third, while this document reported it as met. The divisor is now
2.6, calibrated from that measurement and rounded down because overestimating tokens is the
safe direction. Both maps now render at ~3 450 and ~3 080 **real** tokens.

The phase document asks for exactly this calibration and says it needs a funded key for
`count_tokens`. It does not: a local tokenizer settles it, and the answer was a third out.

Two budget escapes were fixed alongside it. The `other files` tail was appended after the
loop that watched the budget, and the top-ranked file's block was emitted whole however
large — a fifty-fold overshoot at small budgets, in exactly the case a caller sets a small
budget for. The invariant now holds across nine budgets on both repositories.

### The ablation

v1 renders 1 275 tokens against v2's 3 490 — but they are not substitutes. v1 is a path
list; v2 carries every signature with line ranges. Which is better is a question about
whether ranking puts the right file first, and answering that on a real goal needs a model.

## The model half: what a local model could settle, and what it could not

The line here was drawn in the wrong place twice, and both times in the same direction —
assuming a local model could not do something, rather than measuring it.

**First**, everything involving a model went behind "needs quota". True of the scale run,
false of caching and the downgrade, which need a handful of short calls; both were closed
against a local Ollama server once that was questioned.

**Then** I claimed a local model could not drive a run at all, because "qwen2.5 timed out at
240 s on a single tool-calling turn, at 7B and at 3B". That was wrong, and it was not the
model's fault: the probe crashed in its own reporting line — `ChatTurn` has no `.text` — and
I read the resulting stack trace as a timeout. Re-measured properly, a tool-calling turn is
**20.1 s on qwen2.5:7b** and **158.4 s on qwen2.5:3b**, and both emit a correct
`tool_calls` payload with `finish_reason=tool_calls`. `LLM_TIMEOUT_S` defaults to 600 s, so
nothing was ever close to timing out; my probe was hardcoded to 240 s.

The lesson is the one this document keeps relearning: a number that excuses you from doing
the work deserves more scrutiny than one that does not.

| Number | Status | Where it comes from |
|---|---|---|
| Tool-calling turn latency | **measured** | 20.1 s on qwen2.5:7b, 158.4 s on qwen2.5:3b |
| Cache reads from turn 2 | **measured** | `tests/live/test_cache_hits.py` |
| Downgrade reaches a different model | **measured** | `tests/live/test_routing.py` |
| Semantic search on the fixture | **measured** | real embedding model, lexical control |
| Analyzer turn under real context | **measured** | **439 s** on qwen2.5:7b — see below |
| Tasks, attempts, review rounds | **measured** | 2 tasks, 4 code / 5 test / 4 debug phases, 3 escalations |
| Per-role token spend | **measured** | see the table above; cost $0 on a local model |
| Cache reads inside a real agent loop | **measured** | 0.853 / 0.921 on turns 2-3, Analyzer step |
| **Run-level** cache hit rate (all roles) | **0.832** | 34 calls, 5 roles, CODE/TEST/DEBUG included |
| Wall clock and total cost for a scale run | needs quota | a 3 000-file repo |

**"Pending a run" rather than "blocked", and the distinction was earned the hard way.**
`tests/e2e/test_m5_cache.py` has now been run against the local server four times. Every
failure was a defect in this repository, not a wall:

1. `acceptance_criteria: list[str]` answered with a bare string — repaired by wrapping.
2. An honest `null` for `test_selector`, whose schema had no default — the field now
   carries the default the rest of the system already assumed.
3. The Analyzer declining `submit_profile` through both reminders. This one I reported as
   a model capability. It was not: `run_tools` sent `tool_choice="auto"` on every turn,
   including the reminders, so the loop asked twice and gave up while `parse()` had been
   forcing its call all along. The last reminder now names the tool.

**What is actually left is throughput.** One Analyzer turn under real context — repo map,
facts, tool definitions — measured **439 s**, against **20.1 s** for a toy prompt on a warm
model. The isolated probe over-promised by roughly 20x, which is worth recording: a
micro-benchmark of a model says very little about that model inside an agent loop. A full
run on this hardware is hours.

### What the criterion actually needs, measured against both options

"Needs quota" was too vague to act on, so both alternatives were probed directly. Neither
can produce the number, and they fail for opposite reasons:

| | Speed per call | Caching |
|---|---|---|
| Local Ollama, qwen2.5:7b | 439–823 s | **works** — 0.853, 0.921 measured |
| OpenRouter free tier | **3.4 s** | **none** — see below |

`poolside/laguna-s-2.1:free` answers a tool-calling turn in 3.4 s, roughly 130× faster than
this laptop, and emits correct parallel tool calls. It also reports `cached_tokens: 0` on
two consecutive calls carrying an *identical* 3 322-token prefix with an explicit
`cache_control` breakpoint. That is not a measurement artefact: no free model on the
platform lists `input_cache_read` pricing at all, and a model that does not price cache
reads does not perform them.

So a free-tier run would complete quickly and report a cache hit rate of exactly zero.

**This analysis was right about the free tier and wrong about the conclusion.** It ended
"the criterion needs a paid model". It did not: the contended local model, the slow one,
produced 0.832 once the run was simply allowed to continue. What a paid model buys is the
*scale* run — volume and wall-clock — not the caching measurement.

**What that costs is the surprise.** 265 tool-capable models publish cache-read pricing,
and the cheap end is very cheap:

| Model | $/Mtok input | $/Mtok cached | Est. fixture run |
|---|---|---|---|
| `inclusionai/ling-3.0-flash` | 0.02 | 0.004 | **~$0.001** |
| `openai/gpt-5-nano` | 0.05 | 0.005 | ~$0.002 |
| `google/gemini-2.5-flash-lite` | 0.05 | 0.010 | ~$0.003 |

Estimated over ~40 calls at a ~3 000-token prefix, 70 % cached. The M5 fixture run costs a
fraction of a cent, and the scale run's **$5 ceiling has three orders of magnitude of
headroom** at these rates. The dependency is therefore a *minimum* credit purchase, not $10
of consumption — the runs themselves are nearly free.

`evals/results/` takes a row per run, and `test_m5_cache` writes its report in a `finally`,
so even a run that stalls leaves its numbers behind.

## Cost per solved task

The metric, when there is one — not cost per run. A run that spent half as much and
finished one task instead of three cost more per unit of work. It is in the PR footer as
`$X.XX ($Y.YY/task)` and in `autoswe_tokens_per_solved_task`, divided by tasks **done**.
