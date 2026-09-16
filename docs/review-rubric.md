# Review severity rubric

The Reviewer assigns a severity to every finding it confirms. The harness then decides
whether the run is blocked — `blocking = any(severity == "blocking")`, recomputed in code
from the severities rather than taken from the model's own boolean. The model judges each
finding; the harness judges the gate.

This file is the source of the rubric. `agents/prompts/review.md` renders the same table,
and they are meant to stay identical: a rubric a reviewer cannot see is a rubric it cannot
apply.

| Severity | Meaning | Examples |
|---|---|---|
| **blocking** | Wrong behaviour for a plausible input; a security hole; an acceptance criterion unmet; a test that cannot fail | An off-by-one that drops the last page. A token whose expiry is never checked. A test asserting `True == True`. The task said "add pagination" and there is none. |
| **major** | Likely bug, missing error handling, data loss on an edge case | A bare `except` swallowing the failure. An unbounded `readlines()` on a file of unknown size. No handling for the empty-list case. |
| **minor** | Misleading naming, dead code, a missing docstring on a public API | A function called `validate` that mutates. A branch that cannot be reached. |
| **nit** | Formatting or preference | Import order. Whether a comprehension would read better. |

## What is not a finding

**Style, unless it hides a bug.** "This would read better as a comprehension" is a nit at
most and usually nothing. "This variable shadows the parameter, so the caller's value is
silently ignored" is a bug wearing style's clothes, and it is `blocking`.

**A concern you cannot write a failure for.** Every finding needs a
`failure_scenario`: concrete inputs or state, and the wrong output or crash that follows.
If you cannot write one, you have a feeling rather than a finding. Say nothing.

**Something the diff does not contain.** The Reviewer sees the change, not the whole
repository. A pre-existing problem on a line the run never touched is not this run's to
answer for — the security scanners tag those `pre_existing` and list them for information.

## Why two passes

The cheap pass (`review_pre`, Sonnet 5) enumerates everything that *could* be wrong, one
finding per concern, and is allowed to be wrong. The expensive pass (`review`, Opus 5) opens
each file and decides `confirmed` or `false_positive` with a one-line reason.

The split exists because the two jobs reward opposite instincts. Enumeration wants
suspicion and costs little; verification wants scepticism and costs a lot. One pass doing
both either misses findings or reports noise, and noise is worse: a reviewer that cries
wolf is one a human learns to skip.

Dropped candidates are kept in the `review` artifact with their reasons, so "why was this
not reported" has an answer.
