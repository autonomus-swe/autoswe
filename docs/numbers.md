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
| Cache hit rate above 60 % on a real run | **partly** — reads from turn 2 measured; run-level needs quota |
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

The line here was drawn in the wrong place at first. An earlier version of this document
put everything involving a model behind "needs quota", which was true of the scale run and
false of two criteria that need only a handful of short calls. Caching and the downgrade
were both closed against a local Ollama server once that was questioned.

What a local model genuinely cannot do is *drive a run*. qwen2.5 timed out at 240 s on a
single tool-calling turn, at 7B and at 3B — and a run is hundreds of those turns with a
sandbox between them. So the division is not "model" versus "no model" but **one short call
versus a whole agent loop**:

| Number | Status | Where it comes from |
|---|---|---|
| Cache reads from turn 2 | **measured** | `tests/live/test_cache_hits.py` |
| Downgrade reaches a different model | **measured** | `tests/live/test_routing.py` |
| Semantic search on the fixture | **measured** | real embedding model, lexical control |
| Tasks, attempts, review rounds | needs quota | `tests/e2e/test_m3.py`, `m4.py` |
| Per-role cost, cost per solved task | needs quota | `runs` + `llm_calls`, PR footer |
| **Run-level** cache hit rate | needs quota | `tests/e2e/test_m5_cache.py` |
| Wall clock and total cost for a scale run | needs quota | a real run |

The OpenRouter key here is free tier — **50 requests a day** — and one end-to-end run
against a repository this size spends several hundred model calls. `evals/results/` takes a
row per run, so the bottom four populate themselves the moment there is quota. **$10 of
OpenRouter credit** raises the allowance from 50 to 1 000 requests a day and is the whole
dependency.

## Cost per solved task

The metric, when there is one — not cost per run. A run that spent half as much and
finished one task instead of three cost more per unit of work. It is in the PR footer as
`$X.XX ($Y.YY/task)` and in `autoswe_tokens_per_solved_task`, divided by tasks **done**.
