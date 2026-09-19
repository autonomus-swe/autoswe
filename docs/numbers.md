# Numbers

What this project costs, measured rather than estimated. Every figure here came from
`evals/scale.py` or from a run that wrote a row to `evals/results/`.

Where a number is missing the row says so, and where a criterion is **not** met this file
says that too. A previous version of this document reported the map as meeting its budget
when it did not — see "the calibration was wrong" below.

## Repository intelligence, measured

`uv run python -m evals.scale <checkout>` — no model, no key, no network.

| | sympy | pydantic |
|---|---|---|
| Files in the checkout | 2 122 | 879 |
| Files with symbols (what the map ranks) | 1 393 | 427 |
| Symbols | 40 595 | 14 161 |
| **Parse** | **22.8–25.4 s** | **6.7 s** |
| Graph + PageRank | 10.2–15.3 s | 1.5 s |
| **Map tokens (v2)** | **3 490** | **3 484** |
| Map tokens (v1 tree) | 1 275 | 1 258 |
| Embedding chunks | 47 755 | 14 675 |
| Embeddings (hash provider) | 31.9–41.4 s | 6.8 s |

Machine: 12 CPUs, 38 GiB, 8 parse workers. Two sympy rows are recorded because the rate
moves a lot with load — 55 and 61 files/s here, against 115–120 on the same machine when
idle. That spread is the reason the extrapolation below is stated as a range.

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
| Index a 3 000-file repo under 60 s; same-SHA re-run is a no-op | **partly** — see below |
| Repo map ranks the named function's file first; under 4 000 tokens | **met**, after the calibration fix |
| `search_code(semantic=True)` on a fixture auth service (e2e) | **not met** — no such fixture, no e2e |
| Cache hit rate above 60 % on a real run | **not met** — needs quota |
| Compaction blocks preserved (`provider.compacted`) | **not met** — needs the Anthropic provider |
| Budget downgrade table | **partly** — table correct, no second tier to downgrade *to* |
| OTel spans and `/metrics` | **met** |
| Node/Go repos end to end; egress allow/deny | **met** — both halves, tested in the real images |
| Scale run, PR opened, under $5 | **not met** — needs quota |

### Indexing: partly

The no-op half is met and now actually tested — the test named for it only exercised the
storage predicate, so deleting the reuse check left the suite green. There is now a test
that drives `index_repo` twice and checks both the skip and the row count, verified to fail
when the check is removed. (`repo_symbols` has no unique constraint, so a regression there
would silently double the table rather than error.)

The 60-second half is an **extrapolation, not a measurement**. The largest repository
measured is sympy at 2 122 files. At the rates above, 3 000 files is 25–55 s loaded and
~25 s idle — inside 60 s, but the loaded margin is not large, and nobody has run a
3 000-file repository.

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

## The model half: not measured

Blocked on quota, not on code. The OpenRouter key here is free tier — **50 requests a
day** — and one end-to-end run against a repository this size spends several hundred model
calls.

| Number | Where it will come from |
|---|---|
| Tasks, attempts, review rounds | `tests/e2e/test_m3.py`, `m4.py` |
| Per-role cost, cost per solved task | `runs` + `llm_calls`, PR footer |
| Cache hit rate | `tests/e2e/test_m5_cache.py` |
| Wall clock and total cost for a scale run | a real run |

`evals/results/` takes a row per run, so these populate themselves the moment there is
quota. **$10 of OpenRouter credit** raises the allowance from 50 to 1 000 requests a day
and is the whole dependency.

## Cost per solved task

The metric, when there is one — not cost per run. A run that spent half as much and
finished one task instead of three cost more per unit of work. It is in the PR footer as
`$X.XX ($Y.YY/task)` and in `autoswe_tokens_per_solved_task`, divided by tasks **done**.
