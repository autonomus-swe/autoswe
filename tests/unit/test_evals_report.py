"""Rendering result rows as the tables that go into the README.

Pure functions, so they can be checked exhaustively — and they should be, because these
are the numbers the project quotes about itself. A rendering bug here is a false claim in
a README, which is worse than a bug in a feature: nobody re-derives a number they read.
"""

from __future__ import annotations

from typing import Any

import pytest

from evals import report

pytestmark = pytest.mark.unit


def row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "task_id": "t",
        "resolved": True,
        "status": "done",
        "tasks": 2,
        "debug_attempts": 1,
        "review_rounds": 1,
        "wall_clock_s": 60.0,
        "cost_usd": 0.5,
        "cache_hit_rate": 0.6,
        "pr_url": None,
    }
    return {**base, **over}


def test_the_denominator_is_what_could_be_verified() -> None:
    """A suite with ten tasks and one verify command must not read as 10 %.

    `resolved: null` means nobody could check, which is neither a pass nor a failure. Fold
    it into either and the percentage is of a denominator that silently changed.
    """
    rows = [row(resolved=True), row(resolved=False), row(resolved=None), row(resolved=None)]
    s = report.summarise(rows)
    assert s["tasks"] == 4
    assert s["verified"] == 2 and s["unverifiable"] == 2
    assert s["resolved"] == 1 and s["resolved_pct"] == 50.0


def test_a_suite_with_nothing_verifiable_reports_no_percentage() -> None:
    """Not 0 %, not 100 %. `None` is the only honest answer and the table prints "n/a"."""
    s = report.summarise([row(resolved=None), row(resolved=None)])
    assert s["resolved_pct"] is None
    assert "n/a" in report.render([row(resolved=None)])


def test_cost_per_resolved_task_is_none_when_nothing_resolved() -> None:
    """Rather than the total, which would read as the price of a success nobody got."""
    s = report.summarise([row(resolved=False, cost_usd=2.0), row(resolved=False, cost_usd=3.0)])
    assert s["cost_usd_total"] == 5.0
    assert s["cost_usd_per_resolved"] is None


def test_cost_per_resolved_divides_the_whole_spend_by_the_successes() -> None:
    """Failed attempts cost money too, so they belong in the numerator. Dividing only the
    successful runs' cost would report a price nobody paid."""
    s = report.summarise([row(resolved=True, cost_usd=1.0), row(resolved=False, cost_usd=3.0)])
    assert s["cost_usd_per_resolved"] == 4.0


def test_medians_rather_than_means_for_attempts_and_wall_clock() -> None:
    """One run that hit its timeout drags a mean somewhere no run went."""
    rows = [row(wall_clock_s=10), row(wall_clock_s=20), row(wall_clock_s=3600)]
    assert report.summarise(rows)["wall_clock_s_median"] == 20.0


def test_the_three_outcomes_render_as_three_different_marks() -> None:
    out = report.render([row(resolved=True), row(resolved=False), row(resolved=None)])
    assert "✅" in out and "❌" in out and "—" in out


def test_an_empty_result_set_says_so_rather_than_rendering_a_blank_table() -> None:
    assert "_no rows_" in report.render([])
    assert "_no arms_" in report.compare({})


def test_a_pr_url_becomes_a_link_and_its_absence_becomes_nothing() -> None:
    assert "[pr](https://x/1)" in report.render([row(pr_url="https://x/1")])
    assert "[pr]" not in report.render([row(pr_url=None)])


def test_comparison_prints_the_arm_name_it_was_given() -> None:
    """What an ablation changed is a fact about how it was run. A renderer that inferred
    it would eventually infer wrong, in a table somebody pasted into a README."""
    out = report.compare(
        {"no-debugger": [row(resolved=False)], "baseline": [row(resolved=True)]},
        title="Ablations",
    )
    assert "| no-debugger |" in out and "| baseline |" in out
    assert out.index("no-debugger") < out.index("baseline")  # insertion order, not sorted


def test_conditions_carry_the_model_and_the_date() -> None:
    """A percentage without them is not a result. 60 % on three fixture tasks with a local
    7B and 60 % on thirty repository tasks are different claims."""
    line = report.conditions(model="qwen2.5:7b", provider="openai_compat", date="2026-09-22")
    assert "qwen2.5:7b" in line and "openai_compat" in line and "2026-09-22" in line


def test_every_task_row_appears_in_the_table() -> None:
    out = report.render([row(task_id="alpha"), row(task_id="beta")])
    assert "`alpha`" in out and "`beta`" in out
    assert out.count("\n|") >= 4  # header, rule, two task rows


def test_the_aggregate_reports_both_the_count_and_the_percentage() -> None:
    """The count is what somebody checks; the percentage is what gets quoted. Printing one
    without the other is how "60 %" turns out to have been three tasks."""
    out = report.render([row(resolved=True), row(resolved=True), row(resolved=False)])
    assert "2/3 resolved" in out and "66.7%" in out


def test_the_reason_column_appears_only_when_something_went_wrong() -> None:
    """A column of empty cells on a clean suite is noise, and noise is what makes people
    stop reading a table."""
    assert "why" not in report.render([row(resolved=True)])
    assert "why" in report.render([row(resolved=False, error="ProviderError: 503")])


def test_the_reason_does_not_change_the_denominator() -> None:
    """The point at which this could have become laundering.

    A suite whose tasks all died on the same upstream 503 still reads 0/3. Surfacing the
    reason is adding information; moving those rows into a category that excluded them
    from the count would be improving a number by redefining it.
    """
    rows = [row(resolved=False, error="ProviderError: 503") for _ in range(3)]
    s = report.summarise(rows)
    assert s["resolved"] == 0 and s["verified"] == 3 and s["resolved_pct"] == 0.0
    assert "0/3 resolved" in report.render(rows)


def test_a_pipe_in_an_error_cannot_corrupt_the_table() -> None:
    """An unescaped one ends the row early and silently shifts every later column left —
    a table that still renders and is wrong, which is the worst kind."""
    out = report.render([row(resolved=False, error="boom | 503 | high demand")])
    body = next(line for line in out.splitlines() if line.startswith("| `t`"))
    assert body.count("|") - body.count("\\|") == 12  # ten columns plus why, plus the ends


def test_only_the_first_line_of_an_error_is_shown() -> None:
    """A traceback in a table cell is a table nobody can read."""
    out = report.render([row(resolved=False, error="the reason\nstack frame\nanother frame")])
    assert "the reason" in out and "stack frame" not in out


def test_a_file_holding_two_attempts_says_so() -> None:
    """Results files are append-only, so re-running a suite into the same `--results` name
    mixes attempts and the aggregate describes neither.

    It happened the first time anybody re-ran an arm: one file, four rows, three tasks,
    and a percentage that was a blend. The report now says so rather than presenting the
    blend as a number.
    """
    rows = [row(task_id="a", resolved=False), row(task_id="a", resolved=True), row(task_id="b")]
    out = report.render(rows)
    assert "appear more than once" in out and "`a`" in out
    assert "`b`" not in out.split("appear more than once")[1]


def test_a_clean_file_gets_no_warning() -> None:
    """A banner on every table is a banner nobody reads."""
    assert "appear more than once" not in report.render([row(task_id="a"), row(task_id="b")])


def test_the_warning_does_not_change_the_totals() -> None:
    """Saying the number is a blend is not the same as silently filtering it — which
    attempt you want is the reader's decision, and `at` is how they choose."""
    rows = [row(task_id="a", resolved=False), row(task_id="a", resolved=True)]
    assert report.summarise(rows)["resolved"] == 1
    assert "1/2 resolved" in report.render(rows)


# ---- the four gaps a mutation sweep found in this module -----------------------------------
#
# Each of these survived the whole suite. The tests above are thorough about `summarise` and
# `render`'s arithmetic and never touch the seams below: a mixed suite, the denominator of the
# cost total, `main`'s grouping, and the one optional argument `conditions` takes.


def test_a_clean_row_beside_a_failed_one_does_not_break_the_table() -> None:
    """A mixed suite is the ordinary case and it was untested.

    `render` adds the `why` column as soon as *any* row has an error, and then asks every
    row for one — including the rows that succeeded, whose `error` is empty. Without the
    guard in `_why` that is an `IndexError` on `text[0]`, so one failure in a suite of
    thirty loses the entire report, including the twenty-nine results that are fine.

    The existing tests only ever rendered all-clean or all-failed sets, so the guard was
    never reached.
    """
    out = report.render(
        [
            row(task_id="passed", resolved=True),
            row(task_id="broke", resolved=False, error="ProviderError: 503"),
        ]
    )
    assert "`passed`" in out and "`broke`" in out
    assert "503" in out, "the failure's reason is shown"
    assert "2/2 resolved" not in out, "and the clean row did not absorb the failure"


def test_an_error_that_is_only_whitespace_is_treated_as_no_error() -> None:
    """The boundary the guard actually defends: `error: "   "` splits to an empty list just
    as `error: None` does, and a cell reading `""` is better than a traceback."""
    assert report._why(row(error="   ")) == ""
    assert report._why(row(error=None)) == ""
    assert report._why(row(error="real reason")) == "real reason"


def test_the_cost_total_includes_tasks_that_could_not_be_verified() -> None:
    """Spend is spend. A task that crashed still called the model on its way down, and a
    total that counted only verifiable rows would under-report the bill — quietly, and in
    the direction that flatters.

    Narrowing `costs` to verified rows survived the suite because every existing cost test
    uses rows that are all verifiable.
    """
    rows = [
        row(resolved=True, cost_usd=1.0),
        row(resolved=None, cost_usd=2.0, error="crashed before it could be verified"),
    ]
    s = report.summarise(rows)
    assert s["cost_usd_total"] == 3.0, "the crashed task's spend is part of the bill"
    assert s["verified"] == 1 and s["unverifiable"] == 1
    assert s["cost_usd_per_resolved"] == 3.0, "everything spent, over what it resolved"


def test_the_cost_mean_is_over_every_task_attempted() -> None:
    """The same denominator question one field along, asserted so the two cannot drift."""
    rows = [row(resolved=True, cost_usd=1.0), row(resolved=None, cost_usd=3.0)]
    assert report.summarise(rows)["cost_usd_mean"] == 2.0


def test_a_note_passed_to_conditions_is_printed() -> None:
    """`conditions` is the line that has to sit next to every percentage, and `note` is
    where its caveat goes — "first attempt", "free tier under load", "one instance is not a
    score". Dropped, the number keeps its model and date and loses the sentence that stops
    it being read as more than it is.
    """
    line = report.conditions(
        model="gemini-3.1-flash-lite",
        provider="openai_compat",
        date="2026-09-23",
        note="three fixture tasks, not a capability claim",
    )
    assert "three fixture tasks, not a capability claim" in line
    assert "gemini-3.1-flash-lite" in line and "2026-09-23" in line


def test_conditions_without_a_note_has_no_trailing_separator() -> None:
    """So the fix cannot become "always append", which would leave a dangling ` · ` on
    every line that has nothing to add."""
    line = report.conditions(model="m", provider="p", date="d")
    assert not line.rstrip().endswith("·")
    assert line.count("·") == 2


def test_the_by_flag_groups_by_the_field_it_names(tmp_path: Any, capsys: Any) -> None:
    """The documented ablation comparison, from the command line.

    `docs/evals.md` §5 tells you to run `report.py <file> --by ablation`. Nothing tested the
    line that does the grouping, and collapsing it to a single key survived the suite — the
    table still renders, with every arm blended into one row labelled `default`. A
    comparison that silently compares nothing.
    """
    import json as _json
    import sys

    path = tmp_path / "arms.jsonl"
    path.write_text(
        "\n".join(
            _json.dumps(r)
            for r in [
                row(task_id="a", ablation="no-debugger", resolved=True),
                row(task_id="b", ablation="baseline", resolved=False),
            ]
        )
    )

    argv = sys.argv
    sys.argv = ["report.py", str(path), "--by", "ablation"]
    try:
        assert report.main() == 0
    finally:
        sys.argv = argv

    out = capsys.readouterr().out
    assert "no-debugger" in out and "baseline" in out, "each arm is its own row"
    assert "default" not in out, "nothing fell through to the catch-all label"


def test_a_row_missing_the_grouping_field_falls_back_rather_than_crashing(
    tmp_path: Any, capsys: Any
) -> None:
    """Results files are appended to over time, so an older row may predate the field being
    grouped by. It belongs in the table under a name that says so, not in a traceback."""
    import json as _json
    import sys

    path = tmp_path / "mixed.jsonl"
    path.write_text(_json.dumps(row(task_id="old")) + "\n")

    argv = sys.argv
    sys.argv = ["report.py", str(path), "--by", "ablation"]
    try:
        assert report.main() == 0
    finally:
        sys.argv = argv

    assert "default" in capsys.readouterr().out
