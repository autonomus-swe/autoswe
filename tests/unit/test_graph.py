"""The file graph: edges, and the centrality computed from them.

Two things are worth testing here and the rest is plumbing.

**PageRank is implemented rather than imported**, so it needs pinning against known values.
The expected numbers below were checked directly against `networkx`'s own implementation
before that dependency was dropped — three graphs including dangling nodes and a weighted
duplicate edge, agreeing to within 1e-7.

**Edges must point the right way and stop at the right places.** An edge into a test file
from production code is wrong, and it is not hypothetical: without that rule
`tests/unit/test_cli.py` was the single most central file in this repository, above
`cli/main.py`, because it happens to define `Client` and `Response`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repo import graph
from repo.symbols import Symbol

pytestmark = pytest.mark.unit


def sym(path: str, name: str, refs: list[str] | None = None, kind: str = "function") -> Symbol:
    return Symbol(
        path=path,
        kind=kind,
        name=name,
        signature=f"def {name}():",
        start_line=1,
        end_line=2,
        refs=refs or [],
    )


# ---- pagerank ---------------------------------------------------------------------------


def test_pagerank_matches_the_implementation_it_replaced() -> None:
    """Checked against networkx before dropping it. A cycle with one extra edge."""
    rank = graph.pagerank(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a"), ("a", "c")])

    assert rank["a"] == pytest.approx(0.3878, abs=1e-3)
    assert rank["b"] == pytest.approx(0.2148, abs=1e-3)
    assert rank["c"] == pytest.approx(0.3974, abs=1e-3)
    assert sum(rank.values()) == pytest.approx(1.0)


def test_a_file_nothing_points_at_still_holds_mass() -> None:
    """Dangling nodes redistribute rather than leak.

    `d` is isolated and `c` has no incoming edges. If their mass were dropped the scores
    would stop summing to one, and comparing two of them would stop meaning anything.
    """
    rank = graph.pagerank(["a", "b", "c", "d"], [("a", "b"), ("b", "a"), ("c", "a")])

    assert sum(rank.values()) == pytest.approx(1.0)
    assert rank["c"] == pytest.approx(0.0476, abs=1e-3)
    assert rank["d"] == pytest.approx(0.0476, abs=1e-3), "isolated, but still in the graph"


def test_using_a_file_twice_counts_for_more_than_using_it_once() -> None:
    """Repeated edges raise the weight. A file called into twenty times is leaned on
    harder than one called once, and the graph should say so."""
    once = graph.pagerank(["a", "b"], [("a", "b")])
    twice = graph.pagerank(["a", "b"], [("a", "b"), ("a", "b")])

    assert twice["b"] == pytest.approx(0.6491, abs=1e-3)
    assert twice["b"] >= once["b"]


def test_an_empty_graph_is_empty_rather_than_an_error() -> None:
    assert graph.pagerank([], []) == {}


# ---- import edges -------------------------------------------------------------------------


def test_python_imports_resolve_to_files_that_exist(tmp_path: Path) -> None:
    """`from a import b` may mean `a/b.py` or `a/__init__.py`, and only the tree can say.
    An edge to a file that does not exist ranks something that is not there."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "core.py").write_text("def core(): pass\n")
    (tmp_path / "app.py").write_text("from pkg.core import core\nimport pkg\n")

    edges = graph.import_edges(tmp_path, ["app.py", "pkg/__init__.py", "pkg/core.py"])

    assert ("app.py", "pkg/core.py") in edges
    assert ("app.py", "pkg/__init__.py") in edges
    assert all(target in {"pkg/core.py", "pkg/__init__.py"} for _s, target in edges)


def test_an_import_of_something_outside_the_tree_makes_no_edge(tmp_path: Path) -> None:
    """Most imports in any real file are third-party. Pointing at them would rank
    dependencies the agent cannot edit."""
    (tmp_path / "app.py").write_text("import os\nfrom fastapi import FastAPI\n")

    assert graph.import_edges(tmp_path, ["app.py"]) == []


def test_relative_javascript_imports_resolve_through_their_directory(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "util.ts").write_text("export const x = 1;\n")
    (tmp_path / "src" / "app.ts").write_text("import { x } from './util';\n")

    edges = graph.import_edges(tmp_path, ["src/app.ts", "src/util.ts"])

    assert edges == [("src/app.ts", "src/util.ts")]


# ---- reference edges ----------------------------------------------------------------------


def test_using_a_name_draws_an_edge_to_where_it_is_defined() -> None:
    """The half an import list cannot give: which of the imported things is actually used."""
    symbols = [
        sym("app/handler.py", "handle", refs=["paginate"]),
        sym("app/pages.py", "paginate"),
    ]

    assert graph.reference_edges(symbols) == [("app/handler.py", "app/pages.py")]


def test_a_name_defined_everywhere_draws_no_edge() -> None:
    """`run`, `get`, `parse`. Resolving them would wire the graph into a mesh where every
    file leans on every other and centrality stops meaning anything."""
    common = [sym(f"pkg/m{i}.py", "run") for i in range(graph.TOO_COMMON + 1)]
    user = sym("app/caller.py", "main", refs=["run"])

    assert graph.reference_edges([*common, user]) == []


def test_a_collision_resolves_to_the_nearest_definition() -> None:
    """Two files defining `handler` is the normal case. The one in the same package is
    nearly always the one meant, and picking both would double-count."""
    symbols = [
        sym("app/api/handler.py", "handle"),
        sym("worker/jobs/handler.py", "handle"),
        sym("app/api/routes.py", "route", refs=["handle"]),
    ]

    assert graph.reference_edges(symbols) == [("app/api/routes.py", "app/api/handler.py")]


def test_production_code_never_points_at_a_test() -> None:
    """Measured, not hypothetical: without this `tests/unit/test_cli.py` was the most
    central file in this repository, because it defines `Client` and `Response`."""
    symbols = [
        sym("tests/unit/test_cli.py", "Client", kind="class"),
        sym("cli/main.py", "run", refs=["Client"]),
    ]

    assert graph.reference_edges(symbols) == []


def test_a_test_may_point_at_the_code_it_tests() -> None:
    """The edge is only wrong in one direction. A test depending on production code is
    exactly what a test is."""
    symbols = [
        sym("cli/main.py", "Client", kind="class"),
        sym("tests/unit/test_cli.py", "test_it", refs=["Client"]),
    ]

    assert graph.reference_edges(symbols) == [("tests/unit/test_cli.py", "cli/main.py")]


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/test_x.py",
        "src/thing_test.go",
        "web/app.test.ts",
        "web/app.spec.ts",
        "conftest.py",
    ],
)
def test_what_counts_as_a_test(path: str) -> None:
    assert graph.is_test(path)


@pytest.mark.parametrize("path", ["src/latest.py", "app/contest.py", "cli/main.py"])
def test_what_does_not_count_as_a_test(path: str) -> None:
    """`latest` and `contest` contain `test`, and are not tests."""
    assert not graph.is_test(path)


# ---- the whole thing -----------------------------------------------------------------------


def test_the_file_everything_leans_on_ranks_highest(tmp_path: Path) -> None:
    """The point of building a graph at all: `settings.py` is imported by everything and
    defines nothing interesting, and it should still come first."""
    (tmp_path / "core.py").write_text("SETTINGS = 1\n")
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("from core import SETTINGS\n")
    files = ["core.py", "a.py", "b.py", "c.py"]

    built = graph.build(tmp_path, files, [])

    assert built.nodes == 4 and built.edges == 3
    assert max(built.rank, key=lambda f: built.rank[f]) == "core.py"
