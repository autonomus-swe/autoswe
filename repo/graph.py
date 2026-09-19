"""The file graph: who imports whom, and who uses whose definitions.

The repo map's v1 ranking sorted by where a file sits in the tree — `src/` before `tests/`,
manifests first. That is a guess about importance dressed as an answer, and on a large
repository it puts three thousand files in an order nobody chose.

This ranks by centrality instead. Two kinds of edge, and they answer different questions:

**Imports** say what a file declares it depends on. Resolved to real paths, because
`from a import b` may mean `a/b.py` or `a/__init__.py` depending on what exists, and an
unresolved edge is worse than no edge — it ranks a file that does not exist.

**References** say what a file actually reaches for, from the symbol index: an edge from
the file that uses a name to the file that defines it. This catches the dependencies an
import list does not, and it is why `repo_symbols.refs` is stored per definition.

Both point *from* the user *to* the used, so PageRank flows importance toward the files
everything else leans on — which is the thing a reader wants at the top of a map.

## PageRank is implemented here rather than imported

The phase document says `networkx.pagerank`. In networkx 3.6 that function requires
**scipy**, which is forty megabytes in the worker image for one call — and the pure-Python
fallback it used to expose is now private (`_pagerank_python`), so depending on it means
depending on an underscore.

Power iteration is fifteen lines. Implementing it drops two dependencies rather than adding
one, and `tests/unit/test_graph.py` pins it against values checked directly against
networkx's own implementation: three graphs, including dangling nodes and a weighted
duplicate edge, agreeing to 1e-7.

## Name collisions are resolved by proximity, not by guessing

Two files defining `handler` is the normal case, not the exception. An edge is drawn to the
definition the using file already imports; failing that, to the nearest one by path. Drawing
edges to every candidate would make common names — `run`, `get`, `parse` — dominate the
graph, which is the opposite of informative.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from observability.logging import get_logger
from repo.symbols import Symbol

log = get_logger(__name__)

PAGERANK_ALPHA = 0.85
# A name shorter than this matches too much to be evidence of anything: `id`, `db`, `ok`.
MIN_REF_NAME = 3
# Past this, a name is a word the language uses rather than a symbol somebody defined.
TOO_COMMON = 12

# A test file is never a dependency of production code. Without this, a reference edge is
# drawn into `tests/unit/test_cli.py` from everything that mentions `Client` or `Response`
# — names it happens to define — and on this repository that made it the single most
# central file, above `cli/main.py`. Measured, not hypothetical.
TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.", "conftest")

# The names group is `[^\n]` and not `[\w*,\s]`: `\s` matches newlines, so
# `from pkg.core import core` swallowed the following `import pkg` line and that import
# was never seen. Found by a test that expected both edges from a two-line file.
PY_IMPORT = re.compile(r"^[ \t]*(?:from\s+([.\w]+)\s+import\s+([^\n]+)|import\s+([.\w]+))", re.M)
JS_IMPORT = re.compile(r"""(?:from|require\()\s*['"]([^'"]+)['"]""")
GO_IMPORT = re.compile(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([^"]+)"', re.M)


@dataclass
class FileGraph:
    """Files and the edges between them, with a centrality score per file."""

    rank: dict[str, float]
    edges: int
    nodes: int

    def score(self, path: str) -> float:
        return self.rank.get(path, 0.0)


def _python_targets(module: str, importer: str, known: set[str]) -> list[str]:
    """Resolve a Python module path against what is actually in the tree.

    `from a import b` can mean `a/b.py`, `a/b/__init__.py`, or a name inside `a.py`, and
    only the file listing can say which. A relative import is resolved against the
    importing file's package, which is why `importer` is a parameter.
    """
    if module.startswith("."):
        base = PurePosixPath(importer).parent
        for _ in range(len(module) - len(module.lstrip("."))):
            base = base.parent if base != PurePosixPath(".") else base
        module = module.lstrip(".")
        prefix = str(base) if str(base) != "." else ""
    else:
        prefix = ""
    parts = [p for p in module.split(".") if p]
    if not parts:
        return []
    stem = "/".join(parts)
    candidates = [f"{stem}.py", f"{stem}/__init__.py"]
    if prefix:
        candidates = [f"{prefix}/{c}" for c in candidates] + candidates
    # `from a import b` where b is a module: try one level deeper as well.
    return [c for c in candidates if c in known]


def _relative_targets(spec: str, importer: str, known: set[str]) -> list[str]:
    """JS/TS relative imports: `./thing` against the importing file's directory."""
    if not spec.startswith("."):
        return []  # a package, not a file in this tree
    base = PurePosixPath(importer).parent
    target = str((base / spec).as_posix())
    # Normalise `a/./b` and `a/../b` without touching the filesystem.
    parts: list[str] = []
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    stem = "/".join(parts)
    suffixes = (".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.js")
    return [c for c in (stem, *(stem + s for s in suffixes)) if c in known]


def import_edges(root: Path, files: list[str]) -> list[tuple[str, str]]:
    """Edges from each file to the files it imports, resolved against the tree."""
    known = set(files)
    edges: list[tuple[str, str]] = []
    for rel in files:
        suffix = PurePosixPath(rel).suffix
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if suffix in (".py", ".pyi"):
            for from_mod, _names, plain_mod in PY_IMPORT.findall(text):
                module = from_mod or plain_mod
                edges.extend((rel, t) for t in _python_targets(module, rel, known))
        elif suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            for spec in JS_IMPORT.findall(text):
                edges.extend((rel, t) for t in _relative_targets(spec, rel, known))
        elif suffix == ".go":
            # Go imports name packages, not files. The last segment usually matches the
            # directory, which is the best a within-module resolution can do.
            for spec in GO_IMPORT.findall(text):
                tail = spec.rsplit("/", 1)[-1]
                edges.extend((rel, f) for f in files if PurePosixPath(f).parent.name == tail)
    return edges


def is_test(path: str) -> bool:
    """Whether this path is a test, by directory or by filename convention."""
    pure = PurePosixPath(path)
    if any(part in ("tests", "test", "__tests__", "spec") for part in pure.parts):
        return True
    return any(marker in pure.name for marker in TEST_MARKERS)


def reference_edges(symbols: list[Symbol]) -> list[tuple[str, str]]:
    """Edges from the file that uses a name to the file that defines it.

    The half an import list cannot give you: a file can import a package and use one
    function from it, and it is the function that says what the dependency is really for.

    A name defined in more than a handful of places is dropped rather than resolved. Common
    verbs — `run`, `get`, `parse` — would otherwise wire the graph into a mesh where every
    file leans on every other and centrality means nothing.
    """
    definitions: dict[str, list[str]] = defaultdict(list)
    for sym in symbols:
        if len(sym.name) >= MIN_REF_NAME:
            definitions[sym.name].append(sym.path)

    edges: list[tuple[str, str]] = []
    for sym in symbols:
        source_is_test = is_test(sym.path)
        for ref in sym.refs:
            targets = definitions.get(ref)
            if not targets or len(targets) > TOO_COMMON:
                continue
            # Production code never depends on a test. A test file that happens to define
            # `Client` would otherwise collect an edge from everything mentioning the name.
            allowed = [t for t in targets if t != sym.path and (source_is_test or not is_test(t))]
            best = _nearest(sym.path, allowed)
            if best is not None:
                edges.append((sym.path, best))
    return edges


def _nearest(source: str, candidates: list[str]) -> str | None:
    """The candidate sharing the longest path prefix with the source.

    Proximity as a stand-in for "the one this file means". Two files defining `handler` is
    normal; the one in the same package is nearly always the intended target, and picking
    all of them would let common names dominate.
    """
    if not candidates:
        return None
    source_parts = PurePosixPath(source).parts

    def shared(candidate: str) -> int:
        parts = PurePosixPath(candidate).parts
        count = 0
        for a, b in zip(source_parts, parts, strict=False):
            if a != b:
                break
            count += 1
        return count

    return max(sorted(candidates), key=shared)


def pagerank(
    nodes: list[str],
    edges: list[tuple[str, str]],
    *,
    alpha: float = PAGERANK_ALPHA,
    tol: float = 1.0e-9,
    max_iter: int = 100,
) -> dict[str, float]:
    """Weighted PageRank by power iteration.

    Repeated edges raise the weight rather than being ignored: a file that calls into
    another twenty times leans on it harder than one that calls once, and the graph should
    say so.

    **Dangling nodes** — files nothing imports out of, which is most leaf modules — have
    their mass redistributed uniformly rather than dropped. Dropping it lets rank leak out
    of the graph until the numbers stop summing to one and comparisons between them stop
    meaning anything.
    """
    total = len(nodes)
    if total == 0:
        return {}

    weight: dict[tuple[str, str], float] = defaultdict(float)
    for source, target in edges:
        if source != target:
            weight[(source, target)] += 1.0
    out_weight: dict[str, float] = defaultdict(float)
    incoming: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (source, target), w in weight.items():
        out_weight[source] += w
        incoming[target].append((source, w))

    rank = dict.fromkeys(nodes, 1.0 / total)
    for _ in range(max_iter):
        dangling = sum(rank[v] for v in nodes if out_weight[v] == 0.0)
        base = (1.0 - alpha) / total + alpha * dangling / total
        nxt = {
            v: base + alpha * sum(rank[u] * w / out_weight[u] for u, w in incoming[v])
            for v in nodes
        }
        delta = sum(abs(nxt[v] - rank[v]) for v in nodes)
        rank = nxt
        if delta < tol * total:
            break
    return rank


def build(root: Path, files: list[str], symbols: list[Symbol]) -> FileGraph:
    """Rank every file by how much the rest of the tree leans on it.

    PageRank rather than in-degree: a file used by three files that everything else uses
    matters more than one used by ten leaves, and that distinction is the whole reason to
    build a graph rather than count references.
    """
    edges = [(s, t) for s, t in import_edges(root, files) + reference_edges(symbols) if s != t]
    rank = pagerank(files, edges)
    distinct = len({(s, t) for s, t in edges})
    log.info("file_graph_built", nodes=len(files), edges=distinct)
    return FileGraph(rank=rank, edges=distinct, nodes=len(files))
