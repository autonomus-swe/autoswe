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

### One caveat closed, one still open

- **It is parse only.** `index_repo` also writes the rows, and `evals/scale.py` does not
  time that. On sympy's 40 595 symbols the insert is several seconds more.
- ~~It is measured host-side.~~ **Closed.** The criterion says "on the worker", and this
  was measured with `python -m evals.scale` until a real run did it: a scale run against a
  7 091-file Django clone indexed **3 043 files and 44 292 symbols in 12.06 s** inside the
  orchestrator, in SETUP, with the sandbox up and the worker doing the work. Same numbers
  as the host-side figure, now from the place the criterion names.

## Against the phase's exit criteria

Audited by agents told to disprove each one, not to confirm it.

| Criterion | Verdict |
|---|---|
| Index a 3 000-file repo under 60 s; same-SHA re-run is a no-op | **met** — measured on 7 120 files |
| Repo map ranks the named function's file first; under 4 000 tokens | **met**, after the calibration fix |
| `search_code(semantic=True)` on a fixture auth service | **met** — real embedding model, with a lexical control |
| Cache hit rate above 60 % on a real run | **met** — 0.832 run-level over 34 calls, 5 roles |
| A session outliving its context window | **met** — 10 trims, 48 results cleared, 122 calls in one step |
| Budget downgrade table | **met** — a downgrade reaches a different model that really answers |
| OTel spans and `/metrics` | **met** |
| Node/Go repos end to end; egress allow/deny | **met** — both halves, tested in the real images |
| Scale run, PR opened, under $5 | **met** — sympy, draft PR with 3 commits, 9.4 min, $0.00 |

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

The authoritative figure, read from the `llm_calls` ledger at the end of the run rather
than scraped from its log mid-flight, and recorded in `evals/results/m5_cache.jsonl`:

**45 831 input, 157 413 cache read — a run-level hit rate of 0.7745 over 11 steps.**

An earlier version of this section said **0.832**. That was a snapshot taken at 34 calls,
while the run was still in the Coder loop where the rate is highest; the run went on to
spend 40 more calls in the Debugger at ~0.71–0.86 and settled lower. Both clear the 0.60
criterion, and the ledger figure is the one that counts — a number taken while a run is at
its best is a number chosen rather than measured.

The per-call breakdown below is from the same run at the 34-call mark and is kept because
the *shape* is the point:

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

### The scale run: what 7 091 files cost, and the bug it found

Run against a Django clone with `nvidia/nemotron-3-ultra-550b-a55b:free`:

| | |
|---|---|
| Repository | **7 091 files**, 3 043 indexed |
| Index, in-run | **44 292 symbols in 5.4 s** |
| Reached | SETUP → ANALYZE → **PLAN** |
| Input / cache read | 585 786 / **544 320** |
| **Run-level cache hit rate** | **0.482** |
| Wall clock | 16.8 min |
| Cost | **$0.00** |

**These figures are an undercount, and by a known amount.** They are computed from
`db.run_cost`, which reads the `llm_calls` table — and the forced turn that ends a
max-iterations step was not writing a row to it. The Analyzer's final turn on this run,
carrying the largest prefix of the step, is missing from every number above. The bug is
fixed; the run is gone, so the row stays as recorded rather than being adjusted by
arithmetic nobody can check. Treat 0.482 and 585 786 as floors.

The Analyzer's prefix on this repository is ~230 000 input tokens against 116 640 read from
cache; the Planner's is 355 458 against 427 680 — it reads more from cache than it sends.
On the three-file fixture the same roles moved a few thousand tokens. This is the scale the
cached prefix was designed for, and the first measurement of it.

**It stopped at 47 calls on `free-models-per-day: limit 50`.** That is the quantified
answer to what the scale run needs: not "quota" as a hand-wave, but three more calls than a
free tier allows in a day, on a run that costs nothing to make.

**The run also found a real bug, which is why it got as far as PLAN.** The first attempt
died with `analyzer did not submit a profile (stop_reason=max_iterations, turns=12)`. Every
`must_call` guard fired only on the path where a model *stops talking*; a model that keeps
calling tools until its iterations run out never reached them, so the loop simply ended and
threw away twelve turns of work. The bigger the repository, the more certain that exit
becomes — a 3 043-file tree is exactly what exhausts an explorer's budget — so the guard
was absent precisely where it was most needed. One forced turn outside the iteration budget
now closes it, and the same run then reached PLAN.

### The earlier scale attempt, and the wall it hit

`tests/e2e/test_m5_scale.py` codifies Step 5.10's procedure: clone a large repository, run
a small realistic goal against it, record everything. It ran against Django and stopped
partway with a limit that states its own remedy:

    Rate limit exceeded: free-models-per-day
    X-RateLimit-Limit: 50   X-RateLimit-Remaining: 0
    limit_source: openrouter_free_tier_daily
    "Add 10 credits to unlock 1000 free model requests per day"

What it got before that, recorded to `evals/results/m5.jsonl`:

| | |
|---|---|
| Repository | Django clone, **7 091 files** |
| Index, in-run | **3 043 files, 44 292 symbols, 12.06 s** |
| Analyzer **step** (all turns) | **43 990 input tokens** |
| Wall clock | 12.3 min (mostly rate-limit backoff) |
| Cost | $0.00 |

Two things worth keeping from a run that failed. The index figure closes the "host-side
only" caveat above. And the Analyzer's step spends 43 990 input tokens on a 7 000-file
repository, which is the argument for the cached prefix: the prefix is re-sent on every
turn, and it is the repetition that costs.

**That row said "first call" until it was checked.** `db.step_costs` returns one row per
*step* with usage summed over every call in it, and the field recording it was named
`calls` — so a twelve-turn step read as one enormous request, and this document said the
opening prefix was ~44 000 tokens. Measured directly, the Analyzer's first call on Django
is ~3 400 tokens of message and tool schemas plus a map capped at 3 500: roughly **6 500**.
The argument survives; it was seven times overstated. The field is now named `steps`.

The remaining gap is the daily free allowance, quantified rather than guessed: 50 requests,
and a fixture run needs 42. The blocker is no longer "quota" as a hand-wave but a specific
number with a $10 remedy printed in the error, against runs that cost fractions of a cent.

### The evidence file, and a guard that proved itself

`evals/results/m5_cache.jsonl` now holds both runs, which is where the phase document said
this number would live and where it had never yet appeared:

| Row | Model | Input | Cache read | Rate | Steps | Outcome |
|---|---|---|---|---|---|---|
| 1 | qwen2.5:7b (local) | 45 831 | 157 413 | **0.7745** | 11 | never reached PR |
| 2 | laguna-s-2.1 (hosted) | 211 774 | 14 720 | 0.065 | 8 | **completed to PR** |

Row 1 exists because of a guard added the same night. The local run **failed** — it never
got past the Debugger loop, so `test_m5_cache`'s phase assertion raised — and its report is
there anyway, because that report is computed in a `finally`. Had it been computed after a
successful `run()`, three hours of measurement would have vanished on the assertion, along
with the testcontainer Postgres holding the ledger.

The guard was written speculatively, for a run that might be terminated. It was collected
by an ordinary assertion failure instead, which is the more common case and the one nobody
plans for.

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

**It also needed a switch, which did not exist.** Step 5.10 prescribes running once with
the map ablated, and until now the only route to v1 was the symbol index failing — a fault
path that changes the pipeline as well as the map, so the comparison would have been
between a ranked map and a broken run. `REPO_MAP_VERSION=v1` now selects the arm directly,
returning before the index is read at all. The ablation is a pair of runs away rather than
unaskable.

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

### The scale run, completed

sympy — 2 093 files, 40 595 symbols — surveyed, planned, decomposed, coded, tested,
debugged through two escalations and a replan, and a **draft pull request opened with 3
commits**:

| | |
|---|---|
| Steps | **12** |
| Input / cache read | 720 209 / 1 162 437 |
| **Run-level cache hit rate** | **0.617** |
| Wall clock | **9.4 min** |
| **Cost** | **$0.00** |

| role | steps | input |
|---|---|---|
| analyzer | 1 | 24 197 |
| planner | 1 | 198 423 |
| decomposer | 1 | 1 309 |
| coder | 2 | 149 249 |
| **debugger** | **6** | 345 478 |
| pr_writer | 1 | 1 553 |

The debugger taking half the steps is the honest shape of this run: a free model needed
six attempts across two escalations and a replan, and the state machine bounded each one
and kept going rather than letting it loop. The $5 ceiling is met by three orders of
magnitude, which says more about free-tier pricing than about this system. The figure worth
keeping is **0.617 across a whole run on a 2 000-file repository** — the prefix earning its
place at the scale the phase is named for.

### Django, which did not finish

7 091 files, 11 steps, 1 631 741 input against 2 149 868 read, **0.5685**, $0.00, 21 min.
It reached the same CODE / TEST / DEBUG loop and cycled without converging through two
escalations. Recorded because a run that does not finish is still a measurement, and
because the contrast is the useful part: the same model, the same code, 3.4x the
repository, and the difference is convergence rather than any of the machinery.

### The earlier scale attempt, on 7 091 files

The run the phase is named for, against a Django clone with `gemini-3.1-flash-lite`:

| | |
|---|---|
| Repository | **7 091 files**, 3 043 indexed, 44 292 symbols |
| Calls / steps | **152** across 5 steps |
| Input / cache read | 1 167 021 / **1 886 663** |
| **Run-level cache hit rate** | **0.6178** |
| Wall clock | 15.2 min |
| **Cost** | **$0.00** |

Per role: planner 967 810 input against 1 659 064 read, debugger 136 199 / 150 618, coder
43 542 / 64 805, analyzer 19 470 / 12 176.

**0.6178 clears the 60 % the criterion asks for, on a repository 2.4× the size it
specifies, for nothing.** It reads lower than the 0.7745 measured locally for a structural
reason rather than a regression: this run spent most of its calls in a Planner step whose
context grows faster than the prefix it reuses.

What it did not do is open a pull request. It ran SETUP, ANALYZE, PLAN, DECOMPOSE, CODE,
TEST and two DEBUG rounds, then failed — see below. So the criterion's three parts stand at
numbers recorded, cost well under $5, and the pull request still outstanding *on a repo
this size*.

### Gemini on a 7 000-file repository, and the bug that blocked it

The scale run reached ANALYZE on Django with `gemini-3.1-flash-lite` and produced real
cache figures on a large repository for the first time:

    turn  input  cache_read  rate
      10   2060        8082  0.797
      11   2376        8077  0.773
      12   2424        8070  0.769

Twelve turns, no rate limiting. Getting there needed one fix. Gemini 3.x attaches an opaque
`extra_content.google.thought_signature` to every function call and rejects the *next* turn
without it — "Function call is missing a thought_signature in functionCall parts". Turn one
always worked; turn two always 400'd.

The cause was ours. `_assistant_message` rebuilt each tool call from `id`, `type` and
`function` and dropped everything else, while its own field is named `raw_message` and
documented as "appended back verbatim". It now carries unknown fields through, which is the
same thing the line below it already did for OpenRouter's `reasoning_details` — a provider
handing back a token it needs to see again is not a new idea, and enumerating the known
names guarantees the next one is missed.

**A wrong fix was shipped and withdrawn on the way.** The first attempt disabled Gemini's
thinking with `reasoning_effort: "none"`, on the theory that a non-thinking model would not
emit a signature. It did not help, because the signature was being stripped rather than
refused — and once the real fix landed, the setting was verified to be unnecessary and
removed rather than left in as insurance. A behaviour change whose justification has
collapsed is not free: it would have degraded every Gemini call for a reason that turned
out to be false.

**The run then died of something else**, and it is unexplained rather than diagnosed: a
segmentation fault, in a search worker thread, during garbage collection, with the topmost
frame a `Path` comparison in `tools/search.py`. Two candidate causes were checked and
eliminated — the tree is 10 411 paths with only 46 inside `.git`, so the walk is not
enormous, and the machine had 26 GB free with no OOM events. Sorting ten thousand paths
does not segfault. Recorded as open rather than fixed, because the next step is a
reproduction and not a third theory.

> **Second occurrence, 2026-09-23 — and it is not in `tools/search.py`.** The full test
> suite died with `SIGSEGV` (pytest exit 139) once, in `test_full_run.py`, with a topmost
> frame of `pathlib._parse_path` reached through `posixpath.realpath` ←
> `Path.resolve()` ← **`asyncpg._dot_postgresql_path`**, opening a Postgres connection
> inside a SQLAlchemy greenlet. Main thread, no garbage collection involved.
>
> What the two share is `pathlib`, and nothing else: different module, different thread,
> different call site. That **weakens** the Phase 5 attribution to the search tool rather
> than confirming it, and it is the reproduction that entry asked for — a second data
> point, not a third theory.
>
> Frequency: once in roughly 1 460 tests. It did not reproduce in 30 further executions of
> the crashing file (three clean runs). The greenlet's small stack is an obvious thing to
> suspect and is **not** being claimed — saying so would be the third theory, which is
> exactly what the entry above refused. Still open.

## Running the remaining measurements

Two numbers are still missing and both are one command away once there is model quota:

```bash
make scale-run        # the 3 000-file run
make scale-ablation   # the same run on the v1 tree
```

Both default to the Django clone and a cache-capable model; override with `SCALE_REPO=`,
`SCALE_MODEL=` and `SCALE_BASE_URL=`. The fixture clones the repository rather than using
it in place, so neither touches the checkout.

**The endpoint, the model and the key come from one file.** `SCALE_ENV_FILE` (default
`.env.cerebras`; `.gitignore` covers `.env.*`) is a dotenv with `LLM_BASE_URL`, `LLM_MODEL`
and `LLM_API_KEY`, so switching provider is one variable:

```bash
make scale-run SCALE_ENV_FILE=.env.openrouter.bak
```

That grouping is not tidiness. Overriding the model alone leaves the endpoint at whatever
`.env` says, which is how an earlier version of this target sent an OpenRouter model name
to a local Ollama and got a 404 — after starting a sandbox and indexing 3 043 files.

**The key is never written in the Makefile, never echoed, and never typed.** It resolves
inside the recipe where `make -n` cannot reach it, and every recipe touching it is
`@`-prefixed: make echoes commands by default, and the first version of these targets put
the key on stdout for anything capturing output.

When the model id is wrong the preflight prints the ids the key can actually see, so the
usual cause fixes itself. The model id and its endpoint move together — overriding `LLM_MODEL` alone leaves `LLM_BASE_URL` at
whatever `.env` says, which is how the first version of this target sent an OpenRouter
model name to a local Ollama and got a 404 after starting a sandbox and indexing 3 043
files.

Both targets run `make scale-preflight` first: one call, one second, and a message naming
what is wrong. A 404 means the model id does not belong to that endpoint; a 429 means the
daily free allowance is spent.

The free tier is 50 requests a day and the Django attempt reached PLAN at 47, so a
completed run needs credit rather than patience — at `inclusionai/ling-3.0-flash` rates the
run itself is about a tenth of a cent.

## Cost per solved task

The metric, when there is one — not cost per run. A run that spent half as much and
finished one task instead of three cost more per unit of work. It is in the PR footer as
`$X.XX ($Y.YY/task)` and in `autoswe_tokens_per_solved_task`, divided by tasks **done**.

---

# Phase 6 — the private eval suite

**2026-09-23 · `gemini-3.1-flash-lite` via Gemini's free OpenAI-compatible endpoint ·
`openai_compat` · three tasks against `Vatsalya001/autoswe-fixture-python` ·
concurrency 1.**

## The result

| task | resolved | tasks | debug | review | wall | input | output | cache | PR |
|---|---|---|---|---|---|---|---|---|---|
| `ops-guard-zero` | ✅ | 2 | 0 | 2 | 984 s | 181 289 | 3 050 | 0.0000 | [#4](https://github.com/Vatsalya001/autoswe-fixture-python/pull/4) |
| `ops-repr` | ✅ | 2 | 0 | 2 | 928 s | 237 411 | 4 373 | 0.0163 | [#5](https://github.com/Vatsalya001/autoswe-fixture-python/pull/5) |
| `ops-subtract-slugify` | ✅ | 2 | 2 | 2 | 817 s | 308 414 | 4 529 | 0.0370 | [#6](https://github.com/Vatsalya001/autoswe-fixture-python/pull/6) |

**3/3 resolved. 45.5 minutes of wall clock, 727 114 input and 11 952 output tokens,
$0.00.** Rows in `evals/results/phase6-gemini-retry.jsonl`.

"Resolved" is not the run reporting success. For each task the harness cloned the branch
the agent pushed and ran the task's own command in it — `ops-guard-zero`'s verify output
ends `2 passed in 0.01s` against a fresh checkout of `agent/87031d27…`.

## The first attempt was 0/3, and it is the more interesting number

An hour earlier the same suite, same provider, same commit of the agent, resolved **none**
of the three. Every failure was the same thing:

```
ProviderError: InternalServerError: Error code: 503 —
'This model is currently experiencing high demand ... Please try again later.'
```

at three different phases — CODE, DECOMPOSE, and SECURITY. The third had already written
the code and passed the tests; it died in the security scan, 15 minutes in. Rows in
`evals/results/phase6-gemini.jsonl`.

**That 0/3 was a measurement of a free tier's availability, not of the agent.** It is kept
rather than discarded, because a suite that only records its good afternoon is a suite
that will mislead somebody later.

## What that found, and what it did not

The provider retried `429` and a `200` with no choices — the latter because, in its own
words, "an agentic run dies on a blip after minutes of real work". A `503` asking to be
retried is the same blip wearing a status code, and it raised straight through.
`SERVER_ERROR_STATUSES` now retries `500/502/503/504` with exponential backoff.

**The fix fired zero times in the 3/3 run.** Gemini was simply healthy that hour. So the
improvement from 0/3 to 3/3 is *not* evidence for the retry, and nothing here should be
read as such — the retry is covered by unit tests and remains unproven in the field.

## Two smaller things the run says

**Gemini does not implement prompt caching.** 0.0000 on the first task and 0.0163–0.0370
on the others, against 0.7745 on a local model in Phase 5. The cached prefix is built and
sent; this endpoint charges for all of it. That matches the Phase 5 finding on a different
free endpoint, measured the same way.

**Both halves of the loop did real work.** `ops-subtract-slugify` took two Debugger
attempts and every task took two review rounds, so this is not three trivial runs that
happened to go straight through.

## The budget column was decoration, and an ablation found it

`--ablate no-debugger` sets `Budget.max_debug_attempts = 0`, which should send the first
failing test straight to ESCALATE. A Debugger step ran anyway. The row said `0`; the run
behaved as though it said `3`.

`runs.budget` had been **written by the API since Phase 1 and never read**.
`orchestrator/resume.initial_state` built a fresh `Budget()` from defaults and never
looked at the column, so every `--budget`, every `budget_usd` over MCP and every eval
task's ceiling was recorded and ignored. **A caller asking for a $3 ceiling got $10.**

The eval rows above say `budget_usd: 3.0`; those runs actually had $10. It cost nothing
here because the provider was free, and it would have cost real money on one that is not.

This is the fourth instance of the same defect class in this phase — `runs.provider`
before 6.3, `unattended` before 6.1, `Budget.max_debug_attempts` before the base-commit
work, and now the budget wholesale. In every case a column was written, read back by the
API, displayed in the console, and consulted by nothing.

## What this does not measure

Three tasks against one fixture, on one model, in one hour. It is calibration — a floor
that says the loop closes end to end — not a capability claim. A resolved rate worth
quoting needs real repositories and enough tasks that one bad afternoon does not move it,
which is the suite `docs/evals.md` §2 tells you how to write.

## The ablation: with and without the Debugger

**2026-09-23, `gemini-3.1-flash-lite`, the same three tasks.** Rows in
`evals/results/phase6-no-debugger.jsonl`.

| arm | resolved | Debugger steps | wall clock (median) | PRs |
|---|---|---|---|---|
| baseline | 3/3 | 2 | 928 s | #4 #5 #6 |
| `--ablate no-debugger` | 3/3 | **0** | 310 s | #8 #9 #10 |

The arm sets `Budget.max_debug_attempts = 0`, so a failing test goes straight to ESCALATE.
Zero Debugger steps ran, including on `ops-subtract-slugify`, which needed two in the
baseline. The step sequence shows what replaced them: `code, test, code, test` — ESCALATE
rewound the task, replanned it, and resumed at CODE.

**All three still resolved.** On this suite, on this model, the replan path did the
Debugger's job. That is a fact about three small tasks and not a case for removing the
Debugger: each of these is one function, and a replan is cheap when there is little to
replan. The Phase 5 sympy run, where the Debugger took six of twelve steps, is the shape
this suite cannot speak to.

**Do not read the wall clock as a speed-up.** The baseline ran while the free tier was
slower — the same suite's first attempt that day lost three tasks to `503`s. Two arms an
hour apart on a shared free endpoint are not a controlled comparison of anything but
themselves.

**This is also the end-to-end proof of the budget fix**, which is how the arm was worth
running twice: before it, `max_debug_attempts=0` was recorded and ignored, and a Debugger
step ran anyway.

---

# Phase 6 — the first SWE-bench instance

**2026-09-23 · `astropy__astropy-12907` from SWE-bench Lite · `gemini-3.1-flash-lite` via
Gemini's free endpoint.** Prediction in `evals/results/swebench-astropy-12907.jsonl`.

| | |
|---|---|
| Instance | `astropy__astropy-12907` — `separability_matrix` on nested `CompoundModel`s |
| Repository | `astropy/astropy`, cloned at the instance's pinned `base_commit` |
| Run | `e7f1b227-694c-4816-b296-af88f99c67e9` |
| Outcome | **no patch.** 22 steps, 7 of them Debugger, 3 151 s, $0.00, 118 LLM calls |
| Ended | `task t1.1 failed 3 times after a replan` |

**That prediction row was rebuilt from the ledger.** `--out` had written it under `/tmp`,
and a reboot cleared `/tmp` before it was copied into the repository. Every field in it is
a column of the run's own rows — `wall_clock_s` is `finished_at - started_at` — and the
row says `"reconstructed_from": "ledger"` so nobody later reads it as the file the
producer emitted. The run itself is untouched and still in the database; nothing here was
re-run to produce a better number.

**The pipeline holds on a real repository.** A 2 000-file scientific Python project was
cloned at a pinned commit, profiled, indexed, planned, decomposed and coded against, and
the tests were run repeatedly inside the sandbox. Nothing about SWE-bench needs a fork or
a special path; `--limit 1` produced a well-formed `predictions.jsonl`.

**The model could not solve it.** That is the result, and it is unsurprising: a free
flash-lite model against an astropy bug about separability of nested compound models is
not a fair fight. One instance is not a score, and this is not offered as one.

**The empty patch is correct, not a loss.** ESCALATE rewinds a task before replanning it,
so after seven Debugger steps and a replan the worktree was back at the base commit —
there was no patch to keep. An empty `model_patch` is the honest prediction.

## Two things this measured that reasoning had got wrong

**The diff-artifact boundary.** It had been claimed that a failed run "still holds the
patch it produced". True when the run's final tests passed and it died later — that is a
real 596-character diff in this database. Not true in general: `_store_diff` runs only on
a passing final TEST, so a run that never got there holds nothing. The claim was right
about the case it was drawn from and too broad beyond it.

**Every prediction said `wall_clock_s: 0.0`.** `measure()` never sets that field —
`run_task` does, and the SWE-bench path does not go through it. An instance that really
took fifty-two minutes was recorded as instant. Fixed by timing the instance where it is
run.

## Cache behaviour, incidentally

**0.4667 across the run** — 679 359 cached of 1 455 753 read, by the
`cached / (input + cached)` definition in `contracts/budget.py` — against 0.0000–0.2472
per task on the three-task fixture suite with the same model and endpoint.

The difference is the size of the stable prefix. Astropy's repo map, facts and profile are
large and unchanging, which is exactly what the cached prefix was built for; the fixture
repository has almost nothing to cache, and its highest figure (0.2472) is the one task
that ran long enough to reuse anything.

**Read it at the end, not during.** The draft of this section said 0.498, taken from the
detail endpoint while the run was still going; summing the columns afterwards gives
0.4667. It never left the working tree, but the trap is worth naming — a cache rate quoted
mid-run is a rate over the calls made so far, and it moves.

---

# Phase 6 — a clean clone, timed

**2026-09-23 · this machine** (Docker 29.8.0 snap, uv 0.11.17, Python 3.12.12). The
release checklist asks whether "a clean clone plus the setup docs reaches a green PR on
the fixture in under 30 minutes of setup". Nobody had ever timed it, so the entry said
**unmeasured** rather than met. This is the measurement.

| Step | |
|---|---|
| `git clone` | 3 s |
| `cp .env.example .env`, set ports and the two secrets | 9 s |
| `./scripts/bringup.sh` to a fully healthy stack, exit 0 | **33 s** |
| **Setup total** | **45 s** |

Against a 30-minute budget. What `bringup.sh` proved on its way through: postgres and
redis published and accepting connections, migrations at head (0007), three sandbox
images present, `/healthz` green on both database and redis, the console serving HTTP 200,
`/runs` returning 401 without a key, and the worker started and listening.

**These are warm caches and the number would be larger on a fresh machine.** The uv cache
was populated, the postgres image was pulled, and the three sandbox images had been built
three days earlier — `bringup.sh` reported them as present rather than building them. A
first-ever run on this machine pays for all three, and the sandbox image alone is 1.21 GB.
45 seconds is the re-clone figure, not the cold figure, and the checklist entry says so.

## Two things a clean clone found that the original checkout could not

**Snap Docker cannot read a checkout outside `$HOME`.** The first attempt cloned into
`/tmp` and infrastructure failed immediately. The script diagnosed it exactly — named snap
confinement as the cause, said to move the checkout under `$HOME`, and stopped rather than
continuing past a broken step. That is the failure working as designed, and the four
seconds it took to say so are in the table above as a false start that is not counted in
the 45.

**`bringup.sh` hardcoded `:8000` for the API while compose already honoured `API_PORT`.**
Seven places: where it starts uvicorn, where it waits for health, both console checks, the
Ready banner and two lines of `status`. A developer who set `API_PORT=8001` got compose
publishing on 8001 and this script probing 8000, so it would report *"the API never became
healthy"* about an API that was up. Fixed by reading the port the same way postgres and
redis are read; verified by bringing this clone up on 8001 and watching every line follow
it.

Only a second checkout could surface this, because one checkout on the default port is
correct by coincidence. That is the argument for the clean-clone test being a real test
rather than a formality.

## And the other half: a green PR from that clone

| | |
|---|---|
| Run | `a605d5fa-7ca5-4397-be82-daa88f198c22`, created from the clean clone |
| Wall clock | **8 min 07 s** — `analyze → plan → code → review → security → pr → done` |
| Cost | $0.00 (`gemini-3.1-flash-lite`, free endpoint) |
| Pull request | [autoswe-fixture-python#11](https://github.com/Vatsalya001/autoswe-fixture-python/pull/11) |
| Verified | branch checked out, `pytest -q` → **4 passed, exit 0** |

Green means the tests were run on a checkout of the branch the agent pushed, not that the
model said it was finished.

**The first attempt got to `pr` and failed there**, on `CA_BUNDLE`: this network runs a
TLS-inspecting proxy, git trusts its root CA and Python's bundled certifi does not. The
error named the cause and the fix exactly. `.env.example` documents the setting correctly,
commented out — so this is not a documentation gap, it is a *timing* gap: you learned it
after ten minutes of planning, coding, testing, reviewing and scanning. `bringup.sh` now
checks GitHub's certificate at configuration time, with the same bundle a run will use,
and says so before you spend anything.

**The check had to be written twice.** The first version called
`ssl.create_default_context()` with no `cafile`, which reads the OS trust store — trusts
the proxy CA, prints "ok", and a run still fails. PyGithub goes through `requests`, which
trusts **certifi**. A check that is green where the real thing is red is worse than no
check; it now uses `certifi.where()` when `CA_BUNDLE` is unset, and both outcomes were
verified by removing the setting and putting it back.

## What the agent did that it was not asked to do, correctly

The goal asked for `multiply` alone. The diff also adds `subtract` and `slugify`.

That is not scope creep — **the fixture's `main` does not collect**. Its `tests/test_ops.py`
imports `slugify` and `subtract`, neither of which exists in `fixture/ops.py`; `pytest` on
`main` exits 2 with a collection error. The task's verification is the whole suite, so the
only way to a green suite was to implement the two missing functions.

The interesting part is what it did **not** do: delete the failing imports, or narrow the
test run to one file. Both would have been faster and both are the failure mode
`docs/evals.md` §2 warns about. It wrote the implementations and left every existing test
in place.

---

# Phase 6 — a local open model, measured

**2026-09-23 · this machine**: 13th Gen Intel i7-1355U, 12 cores, 38 GB RAM,
**Intel integrated graphics — no CUDA device**. Ollama 0.34.0 (snap).

Criterion 3 asks for the M1 fixture end to end "on Qwen3-Coder via vLLM, with
`llm_calls.provider` all `openai_compat`". It had been `[~]` with *"that needs a GPU"* as
an assertion. This is the same conclusion with numbers behind it, and one part of it is
now measured rather than reasoned.

## Local inference works through the same code path

A run was created against Ollama serving `qwen2.5-7b-32k` — **no API key, no network, no
vendor of any kind** — and it ran, through exactly the `openai_compat` provider the
criterion names. `analyze` and `plan` completed. That half of the claim is not in doubt.

## What the throughput says, from the ledger

Same database, same fixture repository, same shape of task:

| Run | Model | Calls | Output tokens | Model time | **tok/s** | Status |
|---|---|---|---|---|---|---|
| `a605d5fa` | `gemini-3.1-flash-lite` (hosted, free) | 67 | 4 200 | 7.1 min | **9.88** | done |
| `574bffb9` | `gemini-3.1-flash-lite` | 67 | 3 400 | 8.5 min | 6.67 | failed at PR |
| `7c18e262` | `qwen2.5-7b-32k` (local, CPU) | 10 | 1 874 | 22.4 min | **1.40** | still running |

**7× slower**, and the completed run needed **67 calls**. Ten calls cost 22.4 minutes of
model time, so sixty-seven is upward of two and a half hours of pure generation before
counting the context growth that makes later calls slower. One planner call took **677
seconds** to produce 262 tokens — 0.39 tok/s — on a prompt that was fully cached
(`cache_hit_rate: 1.0`), so prompt caching is not the lever here. Generation is.

**This is a statement about the hardware, not the model or the code.** A 7B on twelve CPU
cores is the whole explanation. The same binary against a GPU is what the criterion asks
for and what this box cannot supply.

## Why not Qwen3-Coder, specifically

Two attempts, neither a code problem:

- **`qwen3-coder:30b` (18.6 GB)** downloads all twenty chunks and then hangs without
  committing them — the process sits in a futex wait at 0.7 % CPU with the partials never
  renamed. Two runs, fifteen minutes each, same result.
- **`qwen2.5-coder:7b` (4.7 GB)** fails with `digest mismatch, file must be downloaded
  again`.

Three corrupted or stalled pulls of two different models points at the transfer rather
than the models. This network runs a TLS-inspecting proxy — the same one that made the
first clean-clone run fail its GitHub certificate check — which is a **plausible** cause
and not a demonstrated one. It is recorded as an observation, not a diagnosis.

So the criterion stays `[~]`, and the reason is sharper than it was: vLLM needs a CUDA
device this machine does not have, and the Ollama fallback cannot fetch a coder model on
this network. What *is* settled is that the provider path itself is vendor-free and works.

## What actually stopped the local run: the contract, not the clock

The run above did not run out of time. It **failed in DECOMPOSE**, 40 minutes in:

```
ProviderError: structured output failed validation twice: 2 validation errors for TaskGraphSpec
test_command    Extra inputs are not permitted  [input: 'uv run --no-sync pytest -q']
toolbench_root  Extra inputs are not permitted  [input: '/path/to/repo']
```

Asked for a `TaskGraphSpec`, `qwen2.5:7b` returned every declared field correctly and
added two it invented. `extra="forbid"` refused the object, and the whole run died.

**The retry was not the problem.** `parse()` feeds the validation error back as a tool
result and asks again; the second attempt produced the same two fields. That is the same
lesson the one-item-list repair already carries in its comment: *the prompt that reaches
for a field is the prompt that reaches for it again.*

**So this is a repair, and it is the one that was missing.** `_repair_once` already
handles four shapes a small model gets wrong — a list written as a `{"items": …}` wrapper,
a one-element list written as the element, an explicit `null` where the schema has a
default, a string written as a single-key object. `extra_forbidden` was the fifth of the
same kind and was not there.

Dropping an undeclared key invents nothing. The schema is the code's decision, not the
model's, so there is no reading under which keeping the field is right. It is logged
rather than silent — a model that keeps reaching for the same field is usually telling you
the contract is missing something.

**What this changes about the criterion.** The local-model result is no longer "too slow to
finish". It was too slow *and* it hit a real robustness gap, and the gap was in this
project's code rather than in the model. The throughput numbers above stand; the failure
they were attached to has a different cause than the clock.

### The re-run, and what it did not prove

The same goal was run again on the same local model with the repair in place — run
`bdb8a3d9`. It is worth being exact about what happened, because it is not what the fix was
written to demonstrate.

| | |
|---|---|
| Outcome | **failed** after 39.6 minutes, `task t1 failed 3 times after a replan` |
| Steps | analyzer 1, planner 1, decomposer 2, **coder 3** |
| Calls | 29, 2 657 output tokens, 35.6 min of model time, 1.25 tok/s |
| `structured_output_extra_field_dropped` events | **0** |

**The repair never fired.** DECOMPOSE succeeded on its own this time — the model simply did
not invent an extra field on this attempt. So this run is *not* evidence that the repair
works; that rests on the unit tests and on three mutations, each verified to have applied
against a green baseline. Saying otherwise would be crediting a fix for an outcome it had
no part in.

**And the schema issue was not the only thing in the way.** With DECOMPOSE past, the run
got to the Coder and failed there: three attempts and a replan, on a task
(`divide` with a zero guard) that the hosted model completed in eight minutes. That is a
capability limit, and no repair addresses it.

So the corrected reading of both runs together: a local 7B on this hardware hits **two**
independent walls — one that was this project's bug and is fixed, and one that is the model
being a general 7B rather than a coder model, which is not. The open-model criterion needs
better hardware *and* a better model, and the first run's failure had made only the first
of those visible.
