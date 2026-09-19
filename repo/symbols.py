"""The symbol index: what each file defines, and what each definition reaches for.

Phase 5 exists because the repo map ranks files by where they sit in the tree, which is a
guess. A goal that names `paginate` should put `paginate`'s file first, and that needs to
know where `paginate` is defined and who calls it. This is the half that knows.

## What is stored, and why `refs` is per symbol

One row per definition: its kind, its name, the first line of it as a signature, the range,
and **the identifiers used inside it**. Scoping the references to the enclosing definition
rather than to the file is what makes the table a call graph rather than a word list — "who
calls `paginate`" is a query, and ranking in Step 5.2 is the reason to ask.

A reference is assigned to the *innermost* definition containing it, so a call inside a
method belongs to the method and not also to its class.

## Parsed in processes, not threads

tree-sitter releases the GIL unevenly across grammars, so threads give back much less than
the core count suggests. Processes are simpler to reason about and the work is pure: a path
in, a list of symbols out, no shared state.

## A file that does not parse is a file with no symbols

Not an exception. tree-sitter is error-tolerant and returns a tree with `ERROR` nodes for a
syntax error, so a half-written file yields whatever it can — and a file the grammar cannot
handle at all yields nothing and a warning. An index that refuses to build because one file
in three thousand is broken is an index nobody can use.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from observability.logging import get_logger
from repo.languages import Language, for_path
from repo.walk import SKIP_DIRS

if TYPE_CHECKING:  # the grammars are a worker dependency; the types are free
    from tree_sitter import Node

log = get_logger(__name__)

MAX_SIGNATURE = 120
# A file past this is generated, vendored, or minified. Parsing it costs seconds and the
# symbols it yields are nobody's to edit.
MAX_FILE_BYTES = 1_000_000
# Beyond this the row is mostly noise: a definition that touches two hundred distinct
# identifiers tells a ranking pass nothing it did not already know from the file's size.
MAX_REFS = 64


@dataclass(frozen=True)
class Symbol:
    """One definition, and the identifiers it reaches for."""

    path: str
    kind: str
    name: str
    signature: str
    start_line: int
    end_line: int
    refs: list[str] = field(default_factory=list)


@dataclass
class IndexStats:
    """What an indexing pass did, for the log line and the timing assertion."""

    files: int = 0
    symbols: int = 0
    skipped: int = 0
    failed: int = 0
    duration_s: float = 0.0


def _text(node: Node) -> str:
    """A node's source text. `text` is `bytes | None` — None only for a node built without
    a source buffer, which cannot happen here, but the type says otherwise."""
    return node.text.decode("utf-8", errors="replace") if node.text is not None else ""


def _kind(node: Node, language: Language) -> str:
    """What to call this definition.

    Python is the awkward one: a method *is* a `function_definition` inside a class body,
    and tree-sitter's query language cannot say "inside a class". So the ancestors decide,
    which is also the only place this distinction can be made correctly — a nested function
    inside a method is a function, not a method, and walking up finds that.
    """
    base = language.kinds.get(node.type, "symbol")
    if language.name != "python" or base != "function":
        return base
    parent = node.parent
    while parent is not None:
        if parent.type == "class_definition":
            return "method"
        if parent.type == "function_definition":
            return "function"  # nested in a function: a closure, not a method
        parent = parent.parent
    return "function"


def _signature(source: bytes, node: Node) -> str:
    """The first line of the definition, trimmed.

    The first line is what a reader scans for, and for every language here it carries the
    name and the parameters. Truncated rather than wrapped because this is rendered into a
    prompt with a token budget.
    """
    start = node.start_point[0]
    lines = source.split(b"\n")
    if start >= len(lines):
        return ""
    text = lines[start].decode("utf-8", errors="replace").strip()
    return text[:MAX_SIGNATURE]


def parse_source(source: bytes, rel_path: str, language: Language) -> list[Symbol]:
    """Definitions in one file, each carrying the references made inside it.

    Importable on its own so the tests can exercise the whole of it without a database or
    a worktree — this function is where every language-specific decision lands.
    """
    import tree_sitter as ts
    from tree_sitter_language_pack import get_language, get_parser

    grammar = get_language(language.name)
    tree = get_parser(language.name).parse(source)

    found: list[tuple[Node, Symbol]] = []
    cursor = ts.QueryCursor(ts.Query(grammar, language.definitions))
    for _pattern, captures in cursor.matches(tree.root_node):
        nodes = captures.get("def") or []
        names = captures.get("name") or []
        if not nodes or not names:
            continue
        node, name = nodes[0], names[0]
        found.append(
            (
                node,
                Symbol(
                    path=rel_path,
                    kind=_kind(node, language),
                    name=_text(name),
                    signature=_signature(source, node),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                ),
            )
        )

    if not found:
        return []

    # Innermost wins: a call inside a method belongs to the method, not also to its class.
    # Sorting by span puts the tightest enclosing definition first for any given position.
    by_span = sorted(found, key=lambda pair: pair[0].end_byte - pair[0].start_byte)
    refs: dict[int, list[str]] = {id(symbol): [] for _node, symbol in found}
    seen: dict[int, set[str]] = {id(symbol): set() for _node, symbol in found}

    ref_cursor = ts.QueryCursor(ts.Query(grammar, language.references))
    for ref in ref_cursor.captures(tree.root_node).get("ref", []):
        for node, symbol in by_span:
            if node.start_byte <= ref.start_byte and ref.end_byte <= node.end_byte:
                text = _text(ref)
                bucket = seen[id(symbol)]
                if text not in bucket and len(bucket) < MAX_REFS:
                    bucket.add(text)
                    refs[id(symbol)].append(text)
                break

    return [
        Symbol(
            path=symbol.path,
            kind=symbol.kind,
            name=symbol.name,
            signature=symbol.signature,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            refs=refs[id(symbol)],
        )
        for _node, symbol in found
    ]


def parse_file(root: Path, rel_path: str) -> list[Symbol]:
    """One file from disk. Returns nothing rather than raising, whatever is wrong with it."""
    language = for_path(Path(rel_path).suffix)
    if language is None:
        return []
    try:
        source = (root / rel_path).read_bytes()
    except OSError as e:
        log.warning("symbol_file_unreadable", path=rel_path, error=str(e))
        return []
    if len(source) > MAX_FILE_BYTES:
        return []
    try:
        return parse_source(source, rel_path, language)
    except Exception as e:
        # A grammar that cannot handle a file is a file with no symbols, not a failed
        # index. Logged because coverage smaller than it looks is worth saying.
        log.warning("symbol_parse_failed", path=rel_path, error=f"{type(e).__name__}: {e}")
        return []


def source_files(root: Path) -> list[str]:
    """Every file the index covers, as repository-relative paths.

    The same exclusions as the repo map, imported rather than restated: a directory worth
    skipping in one is worth skipping in the other, and two copies of that list would drift.
    """
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if for_path(path.suffix) is None:
            continue
        out.append(str(rel))
    return out


def _parse_one(args: tuple[str, str]) -> list[Symbol]:
    """Module-level so it can be pickled into a worker process."""
    root, rel = args
    return parse_file(Path(root), rel)


def parse_repo(root: Path, *, max_workers: int | None = None) -> tuple[list[Symbol], IndexStats]:
    """Every symbol in the tree, parsed across processes.

    Falls back to this process when there is one file or one core: a pool costs more to
    start than it saves on a fixture, and the tests are mostly fixtures.
    """
    import time

    started = time.monotonic()
    files = source_files(root)
    stats = IndexStats(files=len(files))
    symbols: list[Symbol] = []

    workers = max_workers if max_workers is not None else min(8, (os.cpu_count() or 2))
    if workers <= 1 or len(files) <= 4:
        for rel in files:
            symbols.extend(parse_file(root, rel))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(_parse_one, [(str(root), rel) for rel in files], chunksize=8):
                symbols.extend(result)

    stats.symbols = len(symbols)
    stats.duration_s = time.monotonic() - started
    log.info(
        "symbols_parsed",
        files=stats.files,
        symbols=stats.symbols,
        duration_s=round(stats.duration_s, 2),
        workers=workers,
    )
    return symbols, stats


async def index_repo(engine: object, worktree: Path, repo_sha: str) -> IndexStats:
    """Parse the tree and store it, unless this SHA is already indexed.

    Host-side, called from `setup_node` after the clone and before the sandbox starts: it
    reads files and writes rows, and needs no container for either.

    The skip is the point. The index describes a commit, so the second run against a base
    pays nothing — which is what makes indexing a three-thousand-file repository something
    you can afford to do on every run rather than something you schedule.
    """
    from storage import repo as db
    from storage.db import session

    async with session(engine) as s:  # type: ignore[arg-type]
        if await db.symbols_indexed(s, repo_sha):
            log.info("symbol_index_reused", repo_sha=repo_sha[:12])
            return IndexStats(skipped=1)

    symbols, stats = parse_repo(worktree)
    rows = [
        {
            "path": sym.path,
            "kind": sym.kind,
            "name": sym.name,
            "signature": sym.signature,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
            "refs": sym.refs,
        }
        for sym in symbols
    ]
    async with session(engine) as s:  # type: ignore[arg-type]
        await db.insert_symbols(s, repo_sha, rows)
    log.info(
        "symbol_index_built",
        repo_sha=repo_sha[:12],
        files=stats.files,
        symbols=stats.symbols,
        duration_s=round(stats.duration_s, 2),
    )
    return stats
