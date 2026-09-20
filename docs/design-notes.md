# Design notes

Phase 5's checklist asks for three explanations before Phase 6 begins: where the cache
breakpoints go, why the Coder and Debugger never downgrade, and how the repo map score is
computed. They are written down here rather than left as something someone can recite,
because the interesting part of each is the *reasoning*, and reasoning is what gets lost
when the person who did it moves on.

Every number below is asserted against the code by `tests/unit/test_design_notes.py`. That
is not ceremony: this project has already shipped two documentation bugs of exactly this
kind — a downgrade table documented as `"decompose"` when the role is `"decomposer"`, and a
budget field documented as `["usd"]` when it is `["budget_usd"]`. Both read fine. Both were
wrong. A number in prose has no way of failing when the code moves out from under it, so
these ones are tested.

---

## 1. Where the cache breakpoints go, and why there

A prompt cache is a **prefix** cache. The provider matches the longest identical run of
bytes from the *start* of the request; the first byte that differs discards everything after
it. So the only question that matters is what order things go in, and the answer follows
from how often each part changes.

The request is assembled most-stable-first:

```
[ tools ]              identical for a role, for the life of the process
[ system: role prompt ]  static per role — see the note in gateway/provider.py
[ system: run block ]    repo facts, conventions, repo map — fixed for a whole run
[ messages … ]           the only part that grows
```

**The run block is the subtle one.** Repository facts and the rendered map are a large
block of text — often the biggest single piece of the request — and they are *per run*, not
per turn. Putting them in the messages, which is where they lived originally, means
re-sending several thousand tokens uncached on every call of every step. Putting them at the
end of the cached prefix means they are written once per run and read for free thereafter.
`tests/live/test_cache_hits.py` measures exactly this: turn 1 reads 5 tokens of 2 678, turns
2 and 3 read 2 682.

**What must never go in front.** Anything carrying a timestamp, a run id, a turn counter, or
anything else that varies. This is the failure the design exists to prevent, and it is
silent: the request still works, the answer is still correct, and the bill quietly goes up
several-fold. The control test puts `Run 7f3a started at 12:04. ` at the *front* of an
otherwise identical prompt and asserts the hit rate collapses — because a caching test
without an invalidation control passes just as well against a build with no caching at all.

**Moving breakpoints.** Only a small number of cache marks are allowed per request, so they
cannot simply accumulate. `moving_breakpoints` keeps the two most recent marks and drops
older ones: the prefix behind them is already cached by the longest-match rule, so the mark
is only needed where the cacheable region currently *ends*. A tool result is marked every
`TOOL_RESULT_EVERY` turns rather than every turn, because each new mark costs a cache write.

**Two things that look like breakpoint bugs and are not.** A downgrade starts that role's
cache over — the cached prefix is keyed per model — so the first call after one writes
rather than reads. And clearing old tool results also moves the prefix. Both are accepted
and both are logged, because a hit rate that falls off a cliff mid-run otherwise looks like
the caching broke.

Below `MIN_CACHEABLE_CHARS` nothing is marked at all: the write costs more than the read
saves.

---

## 2. Why the Coder and Debugger never downgrade

Past 90 % of the **dollar** budget, three roles drop a tier: `planner`, `decomposer`,
`review`. Two roles never do: `coder`, `debugger`. The rest are already on the cheapest
tier.

The rule is not "protect the important roles" — every role is important. It is a claim about
**where a weaker model costs less than it saves**, and the two groups differ in a specific
way:

- **Planner, Decomposer and Review each produce one document in one or two calls.** A weaker
  model writes a less elegant plan. The work still happens, the cost is bounded by the
  length of the document, and a mediocre plan is still a plan.
- **The Coder and Debugger run loops.** A cheaper Coder that needs three attempts is not
  cheaper — it is the same money spent on three times as many tool calls, plus a debug loop
  that would not otherwise have run, plus a review round on worse code. The spend is
  proportional to how *well* the model works, so downgrading the roles that loop can
  increase total cost while appearing to reduce per-call cost.

**Only dollars trigger it.** A run near its wall-clock limit is not helped by a cheaper
model, which is not a faster one and on a hard task is slower — it will still be looping when
the clock runs out, having produced less.

**The honest caveat.** On a deployment with one model configured, every tier resolves to it
and the downgrade is real policy with no effect. `_downgraded_roles` reports that as an
empty list rather than claiming a saving nobody made. `tests/live/test_routing.py` proves
the other case against a server that echoes which model answered: with two tiers configured,
a downgraded Planner really is served by the smaller model, and the Coder and Debugger
really are not.

---

## 3. How the repo map score is computed

Two independent signals, normalised to 0–1 and mixed:

```
score = 0.6 * centrality + 0.4 * lexical
```

- **Centrality** — PageRank over the import/reference graph, by power iteration. How much
  of the tree leans on this file. It is *steady across goals*, and it is why a settings
  module ranks above a leaf even when the goal names neither.
- **Lexical** — BM25 over each file's bag of words: its path tokens plus the names of every
  symbol it defines. It is *volatile*, and it is why a goal naming `paginate` finds
  `paginate`'s file whatever the graph thinks.

**Neither alone works**, which is the whole reason for the mix. Centrality alone returns the
same ranking for every question asked about the repository. BM25 alone ranks a file that
merely *mentions* a word above the file that defines the thing.

Both are normalised before mixing, because they are not on comparable scales — a raw BM25
score and a raw PageRank value differ by orders of magnitude, so mixing them unnormalised
would make the weights meaningless. A flat input maps to all zeros rather than dividing by a
zero range.

**Tokenisation is case- and separator-aware.** `parse_json_report` and `parseJSONReport`
both have to match a goal that says "parse report", or the lexical half only works for code
written in one house style.

Then two adjustments:

- **Tests are multiplied by `TEST_PENALTY` (0.7)** — unless the goal itself is about tests,
  detected by the words in `TEST_WORDS`. A test file is worth showing and rarely worth
  showing *first*.
- **Pinned files get `+10.0`** — an increment far larger than any possible score, so it is a
  pin and not a boost. The plan named this file; a reader who does not see it will wonder
  whether the map knew about it. Root-level manifests are pinned the same way.

Ties break on the path, so the ranking is deterministic for a given input.

**Rendering is budgeted separately from ranking.** The ranked list is rendered as an outline
— path plus signatures with line ranges, no bodies — until the token budget is spent. Two
escapes were fixed here: the "other files" tail was once appended *after* the loop that
watched the budget, and the top-ranked file's block was emitted whole however large, which
overshot fiftyfold at small budgets — in exactly the case where a caller sets a small budget
on purpose.

**The token estimate is calibrated for code, not prose.** `CHARS_PER_TOKEN` is 2.6.
It was 4 — the English rule of thumb — and a map reported as "inside its 3 500-token
budget" was really 5 193 tokens, missing the 4 000-token criterion by a third while the
documentation claimed it was met. Measured with a BPE tokenizer: 2.69 chars/token on sympy,
2.90 on pydantic. Rounded down, because overestimating tokens is the safe direction.
