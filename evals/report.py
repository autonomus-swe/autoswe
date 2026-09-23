"""Result rows as the markdown tables that go into the README.

Pure functions over rows. Nothing here reads a database, calls an API or knows what a run
is — which is why it can be tested exhaustively, and why a number in the README can be
traced to a line in a JSONL file rather than to a rendering step nobody checked.

## Conditions travel with the number

Every table carries the model, the date, the task count and the cost per task. A resolved
percentage on its own is not a result: 60 % on three fixture tasks with a local 7B model
and 60 % on thirty repository tasks with a frontier one are different claims, and a reader
who cannot tell them apart has been misled by the format rather than by the number.

## Unverifiable is not resolved, and not unresolved

`resolved: null` is counted in its own column. Folding it either way is how a suite starts
reporting a percentage of a denominator it has quietly changed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any

Row = dict[str, Any]


def summarise(rows: Sequence[Row]) -> dict[str, Any]:
    """The aggregate for one set of rows, with the denominator made explicit."""
    verified = [r for r in rows if r.get("resolved") is not None]
    resolved = [r for r in verified if r.get("resolved")]
    costs = [float(r.get("cost_usd") or 0) for r in rows]
    return {
        "tasks": len(rows),
        "verified": len(verified),
        "unverifiable": len(rows) - len(verified),
        "resolved": len(resolved),
        # Against what could be verified, never against everything. A suite with ten tasks
        # and one verify command must not read as 10 %.
        "resolved_pct": round(100 * len(resolved) / len(verified), 1) if verified else None,
        "cost_usd_total": round(sum(costs), 4),
        "cost_usd_mean": round(sum(costs) / len(rows), 4) if rows else 0.0,
        "cost_usd_per_resolved": round(sum(costs) / len(resolved), 4) if resolved else None,
        "wall_clock_s_median": round(_median(rows, "wall_clock_s"), 1),
        "debug_attempts_median": round(_median(rows, "debug_attempts"), 1),
        "review_rounds_median": round(_median(rows, "review_rounds"), 1),
        "cache_hit_rate_mean": round(_mean(rows, "cache_hit_rate"), 4),
    }


def render(rows: Sequence[Row], *, title: str = "Results") -> str:
    """One suite: a row per task, then the aggregate.

    The `why` column appears only when something went wrong, and it does **not** change
    the denominator. A suite whose tasks all died on the same upstream 503 still reads
    `0/3 resolved`, because that is what happened — but a reader can now see that all
    three say the same thing without opening the JSONL, which is the difference between a
    number they can interpret and one they can only quote.
    """
    if not rows:
        return f"## {title}\n\n_no rows_\n"
    why = any(r.get("error") for r in rows)
    repeated = _repeated(rows)
    head = "| task | resolved | status | tasks | debug | review | wall s | cost $ | cache | PR |"
    rule = "|---|---|---|---|---|---|---|---|---|---|"
    if why:
        head += " why |"
        rule += "---|"
    lines = [f"## {title}", "", head, rule]
    for r in rows:
        line = (
            f"| `{r.get('task_id', '')}` | {_mark(r.get('resolved'))} | {r.get('status', '')} "
            f"| {r.get('tasks', 0)} | {r.get('debug_attempts', 0)} "
            f"| {r.get('review_rounds', 0)} | {float(r.get('wall_clock_s') or 0):.0f} "
            f"| {float(r.get('cost_usd') or 0):.4f} "
            f"| {float(r.get('cache_hit_rate') or 0):.3f} | {_link(r.get('pr_url'))} |"
        )
        if why:
            line += f" {_why(r)} |"
        lines.append(line)
    body = [*lines, "", _aggregate(summarise(rows))]
    if repeated:
        body += ["", _mixed(repeated)]
    return "\n".join([*body, ""])


def _repeated(rows: Sequence[Row]) -> list[str]:
    """Task ids appearing more than once, which means more than one attempt is in here."""
    seen: dict[str, int] = {}
    for r in rows:
        seen[str(r.get("task_id") or "")] = seen.get(str(r.get("task_id") or ""), 0) + 1
    return sorted(task for task, n in seen.items() if n > 1)


def _mixed(repeated: Sequence[str]) -> str:
    """Say so, loudly, rather than aggregating two attempts into one percentage.

    Results files are append-only on purpose — "the history of how the agent did on a
    scenario is more useful than its most recent attempt". The cost is that re-running a
    suite into the same `--results` name silently mixes attempts, and the aggregate above
    then describes neither of them. It happened the first time anybody re-ran an arm: one
    file, four rows, three tasks, and a percentage that was a blend.

    A warning rather than a filter, because which attempt you want is your decision. The
    `at` field on every row is how you separate them.
    """
    return (
        f"> ⚠️ **{len(repeated)} task(s) appear more than once**: "
        f"{', '.join(f'`{t}`' for t in repeated)}. This file holds more than one attempt, "
        "so the totals above are a blend of them rather than a result. Results files are "
        "append-only; use a fresh `--results` name per attempt, or split on the `at` field."
    )


def _why(row: Row) -> str:
    """The first line of what went wrong, short enough for a table cell.

    Pipes escaped, because an error message containing one would end the row early and
    silently move every later column left by one — a corrupted table that still renders.
    """
    text = str(row.get("error") or "").strip().splitlines()
    if not text:
        return ""
    return text[0][:90].replace("|", "\\|")


def compare(groups: Mapping[str, Sequence[Row]], *, title: str = "Ablations") -> str:
    """Several arms side by side — with and without the Debugger, one model against another.

    The arm name is a label the caller chose, and it is printed rather than interpreted:
    what an ablation changed is a fact about how it was run, and a renderer that guessed
    would eventually guess wrong in a table someone pasted into a README.
    """
    if not groups:
        return f"## {title}\n\n_no arms_\n"
    head = "| arm | tasks | resolved | % | $ / task | $ / resolved | wall s (med) | debug (med) |"
    rule = "|---|---|---|---|---|---|---|---|"
    lines = [f"## {title}", "", head, rule]
    for name, rows in groups.items():
        s = summarise(rows)
        lines.append(
            f"| {name} | {s['tasks']} | {s['resolved']}/{s['verified']} "
            f"| {_pct(s['resolved_pct'])} | {s['cost_usd_mean']:.4f} "
            f"| {_money(s['cost_usd_per_resolved'])} | {s['wall_clock_s_median']:.0f} "
            f"| {s['debug_attempts_median']:.1f} |"
        )
    return "\n".join([*lines, ""])


def conditions(*, model: str, provider: str, date: str, note: str = "") -> str:
    """The line that has to sit next to every percentage."""
    parts = [f"**model** {model}", f"**provider** {provider}", f"**date** {date}"]
    if note:
        parts.append(note)
    return " · ".join(parts)


def _aggregate(s: dict[str, Any]) -> str:
    lines = [
        f"**{s['resolved']}/{s['verified']} resolved** ({_pct(s['resolved_pct'])})"
        + (f", {s['unverifiable']} unverifiable" if s["unverifiable"] else ""),
        "",
        "| | |",
        "|---|---|",
        f"| Cost, total | ${s['cost_usd_total']:.4f} |",
        f"| Cost per task | ${s['cost_usd_mean']:.4f} |",
        f"| Cost per resolved task | {_money(s['cost_usd_per_resolved'])} |",
        f"| Wall clock, median | {s['wall_clock_s_median']:.0f}s |",
        f"| Debugger attempts, median | {s['debug_attempts_median']:.1f} |",
        f"| Review rounds, median | {s['review_rounds_median']:.1f} |",
        f"| Cache hit rate, mean | {s['cache_hit_rate_mean']:.4f} |",
    ]
    return "\n".join(lines)


def _mark(resolved: object) -> str:
    # Three states, three symbols. An em dash is not a failure and must not read as one.
    if resolved is None:
        return "—"
    return "✅" if resolved else "❌"


def _link(url: object) -> str:
    return f"[pr]({url})" if url else ""


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _median(rows: Sequence[Row], field: str) -> float:
    values = [float(r.get(field) or 0) for r in rows]
    return median(values) if values else 0.0


def _mean(rows: Sequence[Row], field: str) -> float:
    values = [float(r.get(field) or 0) for r in rows]
    return sum(values) / len(values) if values else 0.0


def main() -> int:
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Render an eval results file as markdown.")
    parser.add_argument("results", type=Path, help="a JSONL file written by evals/run.py")
    parser.add_argument("--title", default=None)
    parser.add_argument(
        "--by",
        default=None,
        help="render as a comparison, grouped by this field (e.g. provider)",
    )
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.results.read_text().splitlines() if line.strip()]
    if args.by:
        groups: dict[str, list[Row]] = {}
        for row in rows:
            groups.setdefault(str(row.get(args.by) or "default"), []).append(row)
        print(compare(groups, title=args.title or f"By {args.by}"))
    else:
        print(render(rows, title=args.title or args.results.stem))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
