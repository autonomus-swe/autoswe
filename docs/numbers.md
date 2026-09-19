# Numbers

What this project costs, measured rather than estimated. Every figure here was produced by
`evals/scale.py` or by a run that wrote a row to `evals/results/`; nothing in this file is
somebody's guess.

Where a number is missing, the row says so. A half-full table presented as a full one is
worse than an empty one.

## Repository intelligence, measured

`uv run python -m evals.scale <checkout>` — no model, no key, no network. Two repositories,
so the per-file rates have a second point rather than one.

| | sympy | pydantic |
|---|---|---|
| Files in the checkout | 2 122 | 879 |
| Files indexed | 1 608 | 453 |
| Symbols | 40 595 | 14 161 |
| **Index** | **12.1 s** | **3.9 s** |
| Graph + PageRank | 7.5 s | 1.3 s |
| Repo map v2 render | 0.6 s | 0.3 s |
| **Map tokens (v2)** | **3 499** | **3 499** |
| Map tokens (v1 tree) | 829 | 817 |
| Embedding chunks | 47 755 | 14 675 |
| Embeddings (hash provider) | 23.6 s | 6.0 s |

Machine: 12 CPUs, 38 GiB. Index and embedding are parallel across 8 workers.

### Against the phase's exit criteria

- **"Indexing a 3 000-file repository takes under 60 s."** Met, with room. 1 608 files in
  12.1 s is ~120 files/s, so 3 000 files extrapolates to ~25 s. The rate is stable across
  both repositories (115 and 111 files/s), which is what makes the extrapolation worth
  making.
- **"The rendered map is under 4 000 tokens for the scale repo."** Met — **after fixing the
  bug this measurement found.** It did not hold before: see below.

### The bug this found

The first measurement produced a **5 300-token map from a 3 500-token budget**.

`render_symbol_map` watched its budget while laying out ranked file blocks, then appended
an `other files (…)` tail of up to 200 paths *after* the loop. On a repository where most
files do not fit, that tail was ~1 800 tokens of pure overshoot, and `token_budget` was a
budget for the ranked half rather than for the map.

Fixing it took two passes, and the second is the instructive one. Charging the tail to the
budget left the map 40 characters over at exactly the budget where the blocks filled the
allowance completely — the overshoot was then the tail's *header*, added when there was no
room for anything. The fix is a reserve held back before the blocks are laid out.
`tests/unit/test_repomap.py` now asserts the invariant across four budgets, because the
first fix passed at one and failed at another.

Both repositories now render at 3 499 tokens against a 3 500 budget.

### The ablation

The v1 tree is much smaller — 829 tokens against 3 499 — because it is a list of paths and
the v2 map carries every signature with line ranges. The two are not substitutes and the
token count alone does not say which is better: the question the phase document asks is
whether ranking puts the right file first, which `tests/unit/test_repomap.py` answers on a
fixture and a real run would answer on a real goal.

Running the ablation properly — the same goal with `repomap=off`, comparing tasks,
attempts and cost — needs the model half below.

## The model half: not measured

Blocked on quota, not on code. The OpenRouter key on this machine is still free tier —
**50 requests a day** — and one end-to-end run against a repository this size spends several
hundred model calls across analyzer, planner, decomposer, per-task coder loops, tester,
reviewer, security and the PR writer.

| Number | Where it will come from | State |
|---|---|---|
| Tasks, attempts, review rounds | `tests/e2e/test_m3.py`, `m4.py` | blocked on quota |
| Per-role cost, cost per solved task | `runs` + `llm_calls`, PR footer | blocked on quota |
| Cache hit rate | `tests/e2e/test_m5_cache.py` | blocked on quota |
| Wall clock, total cost for a scale run | `evals/scale.py` + a real run | blocked on quota |
| Compaction on a long session | needs the Anthropic provider | Phase 6 |

`evals/results/` holds a row per run, so these populate themselves the moment there is
quota — no further code is needed for any of them except the last.

**What unblocks it:** $10 of OpenRouter credit, which raises the free-model allowance from
50 to 1 000 requests a day. That is the whole dependency.

## Cost per solved task

The metric, when there is one — not cost per run. A run that spent half as much and
finished one task instead of three cost more per unit of work, and a total alone hides that
completely. It is in the PR footer as `$X.XX ($Y.YY/task)` and in
`autoswe_tokens_per_solved_task`, divided by tasks **done** rather than tasks planned.
