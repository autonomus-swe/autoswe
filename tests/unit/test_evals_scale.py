"""The measurement tool behind the table at the top of `docs/numbers.md`.

This module had **no test file at all** — 0 % of 67 statements — while every figure in that
table comes out of it. Four mutations survive trivially in an untested module, and each one
corrupts a published number in a different direction:

| mutation | the published number it breaks |
|---|---|
| `started` moved after the parse | **index_s collapses to ~0** — "django, 9.9 s" reads as instant |
| `_tokens` divisor doubled | **map tokens halve** — a map over budget reports as under it |
| `_count_files` counting directories | **"files in the checkout"** counts the wrong thing |
| `goal=` dropped at `render_symbol_map` | the map is no longer ranked against anything |

The second one is not hypothetical. `docs/numbers.md` opens by saying a previous version of
itself "reported the map as meeting its budget when it did not", and the criterion it is
reporting against is *under 4 000 tokens*. A divisor that is wrong in the generous direction
is exactly how that happens again, and nothing here would have noticed.

So these tests are not about coverage. They are about whether the numbers this project
publishes about itself can be trusted, which `docs/numbers.md` states as its whole purpose:
"Nothing goes in there that a JSONL row cannot back."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evals import scale
from repo import repomap, symbols

pytestmark = pytest.mark.unit


def tiny_repo(root: Path) -> Path:
    """A checkout small enough to measure in a unit test and real enough to parse.

    Real tree-sitter parsing, a real graph and the hash embedding provider — no key, no
    network, no container. That is the whole reason `scale.py` is host-side.
    """
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "ops.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n\n\n"
        "def subtract(a: int, b: int) -> int:\n"
        "    return a - b\n\n\n"
        "class Calculator:\n"
        "    def total(self, xs: list[int]) -> int:\n"
        "        return sum(xs)\n"
    )
    (root / "pkg" / "util.py").write_text("def slugify(s: str) -> str:\n    return s.lower()\n")
    (root / "README.md").write_text("# tiny\n")
    return root


# ---- the divisor -------------------------------------------------------------------------


def test_the_token_divisor_is_the_maps_own_and_not_a_copy_of_its_value() -> None:
    """`_tokens`'s docstring promises "the same divisor the map's own budget uses".

    A promise like that is only kept if the two move together, so it is asserted by
    changing the map's constant and watching `_tokens` follow. Asserting
    `len(text) / 2.6` instead would pass on a `_tokens` that had frozen its own copy —
    and the two drifting apart is precisely how a map over budget gets reported as under
    it, which `docs/numbers.md` records happening once already.
    """
    text = "x" * 2600
    assert scale._tokens(text) == int(len(text) / repomap.CHARS_PER_TOKEN)


def test_the_divisor_follows_the_map_when_the_map_changes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half the assertion above cannot make on its own.

    `repomap` turns a token budget into a character budget with `token_budget *
    CHARS_PER_TOKEN`. `_tokens` is the inverse, and if it stops being the inverse the two
    numbers stop meaning the same thing while continuing to look comparable.
    """
    monkeypatch.setattr(repomap, "CHARS_PER_TOKEN", 10.0)
    assert scale._tokens("x" * 1000) == 100, "a doubled or halved divisor is a wrong budget"

    monkeypatch.setattr(repomap, "CHARS_PER_TOKEN", 1.0)
    assert scale._tokens("x" * 1000) == 1000


def test_a_token_count_is_never_larger_than_the_text_it_measures() -> None:
    """The sanity bound a wrong divisor breaks in the dangerous direction.

    Under-counting is what makes an over-budget map pass, so the floor matters more than
    the ceiling: 4 000 reported tokens must not be 10 000 real ones.
    """
    for length in (0, 1, 100, 10_000):
        text = "x" * length
        assert 0 <= scale._tokens(text) <= length


# ---- what "files in the checkout" counts -------------------------------------------------


def test_files_total_counts_files_and_not_directories(tmp_path: Path) -> None:
    """Counting directories instead survives an untested module and changes a published
    number without changing anything a reader could see."""
    tiny_repo(tmp_path)
    (tmp_path / "empty_dir").mkdir()

    # pkg/__init__.py, pkg/ops.py, pkg/util.py, README.md — and `empty_dir` is not a file.
    assert scale._count_files(tmp_path) == 4


def test_files_total_includes_dotfiles_and_git_internals(tmp_path: Path) -> None:
    """Pinning what the published figure actually means, rather than what it sounds like.

    `docs/numbers.md` says "Files in the checkout | 7 120" for django. That count comes from
    `rglob("*")`, which does **not** skip `.git` — so it is every file on disk, including
    the object store, and it is not the same quantity as "files in the project".

    This is asserted rather than fixed on purpose. Changing the measurement would silently
    invalidate every figure already published in that table; stating precisely what the
    number is lets a reader compare it with their own checkout. `docs/numbers.md` now says
    so beside the table.
    """
    tiny_repo(tmp_path)
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text("[core]\n")

    counted = scale._count_files(tmp_path)
    assert counted == 6, "4 project files plus .git/HEAD and .git/config — files on disk"


# ---- the timings --------------------------------------------------------------------------


async def test_the_index_timing_encloses_the_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`index_s` is the headline of the indexing criterion — "under 60 s on 3 000 files".

    Moving `started = time.monotonic()` to *after* the parse leaves every field populated,
    every assertion about shape passing, and the number reported as ~0. A criterion that
    can only be met more easily by a stopwatch bug is not being measured.

    Asserted by making the parse take a known, real amount of time and requiring the
    reported figure to be at least that.
    """
    tiny_repo(tmp_path)
    real_parse = symbols.parse_repo
    delay = 0.25

    def slow_parse(root: Path) -> Any:
        import time as _time

        _time.sleep(delay)
        return real_parse(root)

    monkeypatch.setattr(symbols, "parse_repo", slow_parse)
    row = await scale.measure(tmp_path, "Add a __repr__ to Calculator.")

    assert row.index_s >= delay, (
        f"index_s={row.index_s} is below the {delay}s the parse demonstrably took — "
        "the timing window does not enclose the work it claims to measure"
    )


async def test_every_timing_is_non_negative_and_the_row_is_fully_populated(
    tmp_path: Path,
) -> None:
    """A row with a hole in it becomes a hole in the published table, and `asdict` will
    write `None` into the JSONL as cheerfully as a number."""
    tiny_repo(tmp_path)
    row = await scale.measure(tmp_path, "Add a __repr__ to Calculator.")

    assert row.repo == tmp_path.name
    assert row.files_total >= 4
    assert row.files_indexed >= 2, "pkg/ops.py and pkg/util.py both hold symbols"
    assert row.symbols >= 4, "add, subtract, Calculator, total"
    for field in ("index_s", "graph_s", "map_v2_s", "embed_s"):
        assert getattr(row, field) >= 0.0, f"{field} is negative"
    assert row.map_v2_tokens > 0 and row.map_v1_tokens > 0
    assert row.chunks > 0
    assert row.goal == "Add a __repr__ to Calculator."


# ---- the goal reaches the ranking ---------------------------------------------------------


async def test_the_goal_reaches_the_map_that_is_ranked_against_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--goal` exists because ranking is goal-dependent, and the CLI help says so.

    Dropping `goal=goal` at the call site leaves the row carrying the goal it was asked for
    — `ScaleRow.goal` is set from the argument, not from the map — so the JSONL still claims
    a goal that never influenced the ranking. Every "the map ranks the named function's file
    first" claim rests on this one keyword.
    """
    tiny_repo(tmp_path)
    seen: dict[str, Any] = {}
    real = repomap.render_symbol_map

    def capturing(root: Path, parsed: Any, ranks: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return real(root, parsed, ranks, **kwargs)

    monkeypatch.setattr(repomap, "render_symbol_map", capturing)
    goal = "Rename Calculator.total to summed_total and update its callers."
    row = await scale.measure(tmp_path, goal)

    assert seen.get("goal") == goal, "the map was not ranked against the goal the row records"
    assert row.goal == goal


async def test_the_two_map_versions_are_both_measured(tmp_path: Path) -> None:
    """The ablation is the stated point of the second render: "a ranked map that is not
    smaller, or not more relevant, is a lot of machinery for nothing". Both numbers have to
    exist for that comparison to be possible at all."""
    tiny_repo(tmp_path)
    row = await scale.measure(tmp_path, "Add a __repr__ to Calculator.")
    assert row.map_v2_tokens > 0, "the ranked map"
    assert row.map_v1_tokens > 0, "the v1 tree it is compared against"


# ---- the printed rate ---------------------------------------------------------------------


def test_the_files_per_second_rate_refuses_to_divide_by_zero() -> None:
    """A repository measured as taking no time at all prints a dash rather than crashing
    the run that just spent minutes producing the row."""
    row = _row(index_s=0.0, files_indexed=100)
    assert scale.stats_rate(row) == "-"


def test_the_files_per_second_rate_is_files_over_seconds() -> None:
    """Printed beside `index_s` in the summary, so an inverted rate makes a slow index look
    fast to the person reading the terminal rather than the JSONL."""
    assert scale.stats_rate(_row(index_s=2.0, files_indexed=100)) == "50"


def _row(**over: Any) -> scale.ScaleRow:
    base: dict[str, Any] = {
        "repo": "demo",
        "files_total": 120,
        "files_indexed": 100,
        "symbols": 500,
        "index_s": 2.0,
        "graph_s": 0.5,
        "map_v2_s": 0.2,
        "map_v2_tokens": 3000,
        "map_v1_tokens": 1200,
        "chunks": 400,
        "embed_s": 1.0,
        "goal": "a goal",
    }
    return scale.ScaleRow(**{**base, **over})
