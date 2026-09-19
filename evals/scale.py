"""Measure what the repository intelligence costs on a large repository.

Phase 5 makes several claims with numbers in them — indexing under sixty seconds, a map
under four thousand tokens — and this is what produces those numbers rather than anyone's
estimate. It runs against a real checkout and writes a row to `evals/results/m5.jsonl`.

## Why this is separate from the end-to-end scale run

The phase document's step 5.10 is one procedure: drive a whole run against a large
repository and record everything, cost and cache hit rate included. Half of that needs a
model with quota. This half does not:

    indexed files, symbols, index seconds, embedding seconds, map tokens

All of it is deterministic host-side work — tree-sitter, a graph, BM25, a hash — so it can
be measured today and re-measured on any machine, which is the half of the claim that
should not depend on anyone's billing.

The model-driven half (tasks, attempts, per-role cost, cache hit rate, wall clock) is
recorded by `tests/e2e/` and is blocked on quota. `docs/numbers.md` says which is which
rather than presenting a half-full table as a full one.

## The ablation is the point of the second run

Ranking is only worth its cost if it beats the v1 tree, so the map is rendered both ways
and both token counts are recorded. A ranked map that is not smaller, or not more
relevant, is a lot of machinery for nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from evals import record
from observability.logging import configure_logging, get_logger
from repo import embeddings, graph, repomap, symbols
from repo.repomap import render_map

log = get_logger(__name__)


@dataclass
class ScaleRow:
    """One measurement of one repository. Every field is measured, none is estimated."""

    repo: str
    files_total: int
    files_indexed: int
    symbols: int
    index_s: float
    graph_s: float
    map_v2_s: float
    map_v2_tokens: int
    map_v1_tokens: int
    chunks: int
    embed_s: float
    goal: str


def _tokens(text: str) -> int:
    """The same divisor the map's own budget uses, so the number means the same thing."""
    return len(text) // repomap.CHARS_PER_TOKEN


def _count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())


async def measure(root: Path, goal: str) -> ScaleRow:
    """Index, rank and render a repository, timing each part."""
    files_total = await asyncio.to_thread(_count_files, root)

    started = time.monotonic()
    parsed, _stats = symbols.parse_repo(root)
    index_s = time.monotonic() - started

    files = sorted({s.path for s in parsed})
    started = time.monotonic()
    ranks = graph.build(root, files, parsed).rank
    graph_s = time.monotonic() - started

    started = time.monotonic()
    v2 = repomap.render_symbol_map(root, parsed, ranks, goal=goal)
    map_v2_s = time.monotonic() - started
    v1 = render_map(root)

    # The hash provider, deliberately: it is the one that needs no key and no download, so
    # this number is the chunking and the storage rather than somebody's API latency.
    started = time.monotonic()
    chunks = embeddings.chunk_symbols(root, parsed)
    vectors = await embeddings.HashProvider().embed([c.text for c in chunks])
    embed_s = time.monotonic() - started
    assert len(vectors) == len(chunks)

    return ScaleRow(
        repo=root.name,
        files_total=files_total,
        files_indexed=len(files),
        symbols=len(parsed),
        index_s=round(index_s, 2),
        graph_s=round(graph_s, 2),
        map_v2_s=round(map_v2_s, 2),
        map_v2_tokens=_tokens(v2),
        map_v1_tokens=_tokens(v1),
        chunks=len(chunks),
        embed_s=round(embed_s, 2),
        goal=goal,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, help="a checkout to measure")
    parser.add_argument(
        "--goal",
        default="Add a __repr__ to the public class showing its fields, with a unit test.",
        help="the goal the map is ranked against; ranking is goal-dependent",
    )
    args = parser.parse_args()
    configure_logging("INFO")

    row = await measure(args.repo.expanduser().resolve(), args.goal)
    path = record.append("m5", asdict(row))

    print(f"\n{row.repo}: {row.files_total} files, {row.symbols} symbols")
    print(f"  index      {row.index_s:6.2f}s   ({stats_rate(row)} files/s)")
    print(f"  graph      {row.graph_s:6.2f}s")
    print(f"  map v2     {row.map_v2_s:6.2f}s   {row.map_v2_tokens} tokens")
    print(f"  map v1                 {row.map_v1_tokens} tokens")
    print(f"  embeddings {row.embed_s:6.2f}s   {row.chunks} chunks")
    print(f"\n-> {path}")


def stats_rate(row: ScaleRow) -> str:
    return f"{row.files_indexed / row.index_s:.0f}" if row.index_s else "-"


if __name__ == "__main__":
    asyncio.run(main())
