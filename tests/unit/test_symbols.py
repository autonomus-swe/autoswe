"""The symbol index: what each language yields, and what a broken file does not.

The parser is the whole of the language-specific judgement in this phase, so the tests are
mostly "given this source, exactly these definitions" — a query that silently stops
matching arrow functions or Go methods would otherwise cost the repo map its ranking with
nothing failing.

Two properties are worth more than the rest, and both are about not lying to a ranking
pass:

- **Refs belong to the innermost definition.** A call inside a method is the method's, not
  also its class's, or every class would appear to reference everything its methods do.
- **A file that does not parse yields no symbols and no exception.** An index that refuses
  to build because one file in three thousand is broken is an index nobody can use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repo import symbols
from repo.languages import BY_SUFFIX, LANGUAGES, for_path

pytestmark = pytest.mark.unit

PYTHON = b'''"""A module."""
import os

VERSION = "1"


def paginate(items, size):
    """Split into pages."""
    return chunk(items, size)


class Pager:
    def __init__(self, size):
        self.size = size

    def run(self, items):
        return os.path.join(paginate(items, self.size))

    def outer(self):
        def inner():
            return 1

        return inner()
'''

JAVASCRIPT = b"""export const LIMIT = 5;

export const handler = (req, res) => {
  return fetch(req.url);
};

function parse(text) {
  return JSON.parse(text);
}

class Server {
  listen(port) {
    return this.app.listen(port);
  }
}
"""

TYPESCRIPT = b"""export interface Options {
  retries: number;
}

export type Handler = (req: Request) => Response;

export function parse(text: string): number {
  return Number(text);
}

export class Api {
  get(url: string) {
    return this.http.get(url);
  }
}
"""

GO = b"""package server

import "net/http"

type Server struct {
	addr string
}

func New(addr string) *Server {
	return &Server{addr: addr}
}

func (s *Server) Listen() error {
	return http.ListenAndServe(s.addr, nil)
}
"""


def parse(source: bytes, path: str) -> list[symbols.Symbol]:
    language = for_path(Path(path).suffix)
    assert language is not None, path
    return symbols.parse_source(source, path, language)


def named(found: list[symbols.Symbol], name: str) -> symbols.Symbol:
    matches = [s for s in found if s.name == name]
    assert len(matches) == 1, f"{name}: {[(s.kind, s.name) for s in found]}"
    return matches[0]


# ---- per language ----------------------------------------------------------------------


def test_python_functions_classes_and_methods() -> None:
    found = parse(PYTHON, "src/pages.py")

    assert {(s.kind, s.name) for s in found} == {
        ("function", "paginate"),
        ("class", "Pager"),
        ("method", "__init__"),
        ("method", "run"),
        ("method", "outer"),
        ("function", "inner"),
    }
    assert named(found, "paginate").signature == "def paginate(items, size):"
    assert named(found, "paginate").start_line == 7


def test_a_function_nested_in_a_method_is_a_function_not_a_method() -> None:
    """The distinction tree-sitter's query language cannot express, so the ancestors decide.

    Calling a closure a method would put it in the class's surface, which is the opposite
    of what a reader asking "what does this class expose" wants.
    """
    assert named(parse(PYTHON, "a.py"), "inner").kind == "function"


def test_javascript_reaches_the_arrow_functions_most_of_it_is_written_as() -> None:
    """`function` declarations alone would miss most of a modern file's surface."""
    found = parse(JAVASCRIPT, "src/server.js")

    kinds = {(s.kind, s.name) for s in found}
    assert ("function", "parse") in kinds
    assert ("class", "Server") in kinds
    assert ("method", "listen") in kinds
    assert ("const", "handler") in kinds, "the arrow function assigned to a const"
    assert ("const", "LIMIT") in kinds, "and the plain exported const"


def test_typescript_indexes_the_types_that_are_the_point_of_using_it() -> None:
    found = parse(TYPESCRIPT, "src/api.ts")

    kinds = {(s.kind, s.name) for s in found}
    assert ("interface", "Options") in kinds
    assert ("type", "Handler") in kinds
    assert ("function", "parse") in kinds
    assert ("class", "Api") in kinds
    assert ("method", "get") in kinds


def test_go_funcs_methods_and_named_types() -> None:
    found = parse(GO, "server/server.go")

    assert {(s.kind, s.name) for s in found} == {
        ("type", "Server"),
        ("function", "New"),
        ("method", "Listen"),
    }
    assert named(found, "Listen").signature.startswith("func (s *Server) Listen()")


def test_tsx_is_indexed_as_typescript() -> None:
    """The definitions are identical; the JSX the two grammars disagree about holds none."""
    assert BY_SUFFIX[".tsx"] == "typescript"
    assert parse(TYPESCRIPT, "src/app.tsx"), "a .tsx file yields the same definitions"


def test_every_language_has_both_queries_and_a_kind_map() -> None:
    """A language added with one query would index definitions and no references, or the
    reverse, and nothing else would notice."""
    for name, language in LANGUAGES.items():
        assert "@def" in language.definitions and "@name" in language.definitions, name
        assert "@ref" in language.references, name
        assert language.kinds, name


# ---- references belong to the innermost definition --------------------------------------


def test_a_call_inside_a_method_belongs_to_the_method_not_its_class() -> None:
    """Otherwise every class references everything its methods do, and a ranking pass
    reading that would rank the largest class first regardless of the goal."""
    found = parse(PYTHON, "src/pages.py")

    assert "join" in named(found, "run").refs
    assert named(found, "Pager").refs == [], "the class itself calls nothing directly"


def test_a_definition_records_what_it_reaches_for() -> None:
    """The reason `refs` is stored per symbol: this is the edge in the call graph that
    Step 5.2 ranks on."""
    assert "chunk" in named(parse(PYTHON, "a.py"), "paginate").refs


def test_references_are_deduplicated_and_bounded() -> None:
    """A definition that touches two hundred distinct identifiers tells a ranking pass
    nothing it did not already know from the file's size."""
    source = b"def wide():\n    return " + b" + ".join(
        f"f{i}()".encode() for i in range(symbols.MAX_REFS * 2)
    )

    refs = named(parse(source, "a.py"), "wide").refs

    assert len(refs) == symbols.MAX_REFS
    assert len(set(refs)) == len(refs), "no duplicates"


# ---- what a broken or uninteresting file does -------------------------------------------


def test_a_syntax_error_yields_symbols_it_can_and_no_exception(tmp_path: Path) -> None:
    """tree-sitter is error-tolerant, which is the behaviour we want: a half-written file
    mid-edit still says what it defines above the break."""
    broken = tmp_path / "broken.py"
    broken.write_text("def ok():\n    return 1\n\ndef bad(:\n    return\n")

    found = symbols.parse_file(tmp_path, "broken.py")

    assert [s.name for s in found if s.kind == "function"][:1] == ["ok"]


def test_a_file_the_grammar_cannot_open_is_no_symbols_not_a_crash(tmp_path: Path) -> None:
    assert symbols.parse_file(tmp_path, "does_not_exist.py") == []


def test_a_file_type_with_no_grammar_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# not source")

    assert symbols.parse_file(tmp_path, "notes.md") == []
    assert for_path(".md") is None


def test_a_file_too_large_to_be_anyones_to_edit_is_skipped(tmp_path: Path) -> None:
    """Generated, vendored or minified. Parsing costs seconds and yields nothing useful."""
    big = tmp_path / "bundle.js"
    big.write_bytes(b"function a(){}\n" * (symbols.MAX_FILE_BYTES // 10))

    assert symbols.parse_file(tmp_path, "bundle.js") == []


def test_a_long_signature_is_trimmed_rather_than_wrapped() -> None:
    """It is rendered into a prompt with a token budget."""
    source = b"def f(" + b", ".join(f"argument_{i}".encode() for i in range(40)) + b"):\n    pass\n"

    assert len(named(parse(source, "a.py"), "f").signature) == symbols.MAX_SIGNATURE


# ---- walking a tree ----------------------------------------------------------------------


def test_the_walk_uses_the_repo_maps_exclusions(tmp_path: Path) -> None:
    """Imported rather than restated: a directory worth skipping in one is worth skipping
    in the other, and two copies of that list would drift."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a(): pass\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "dep.py").write_text("def dep(): pass\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("function x(){}\n")

    assert symbols.source_files(tmp_path) == ["src/a.py"]


def test_parsing_a_tree_reports_what_it_did(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a(): pass\n")
    (tmp_path / "b.go").write_text("package main\nfunc B() {}\n")

    found, stats = symbols.parse_repo(tmp_path, max_workers=1)

    assert stats.files == 2 and stats.symbols == 2
    assert {s.name for s in found} == {"a", "B"}
    assert stats.duration_s >= 0


def test_indexing_is_fast_enough_for_the_scale_criterion() -> None:
    """A throughput floor, measured on this repository because it is the largest real
    corpus to hand.

    Measured 2026-09-18 on the development machine, 203 source files, 1774 symbols:

    | workers | rate | implied 3000 files |
    |---|---|---|
    | 1 | 126 files/s | 24 s |
    | 8 | 239 files/s | 13 s |

    against the phase's "under 60 s for 3000 files". The floor here is deliberately far
    below both — a tenth of the single-process rate — because this is a **regression
    guard**, not a proof about the scale repository: a query change that made parsing an
    order of magnitude slower should fail something, and a shared CI machine should not.
    The phase document asks for a manual check on the real repository, and that is right.
    """
    import time

    root = Path(__file__).resolve().parents[2]
    files = symbols.source_files(root)
    assert len(files) > 100, "this test is meaningless on a tree with nothing in it"

    started = time.monotonic()
    _found, stats = symbols.parse_repo(root, max_workers=1)
    elapsed = time.monotonic() - started

    assert stats.symbols > 500, "a real corpus should yield real symbols"
    rate = len(files) / elapsed
    assert rate > 12, f"{rate:.0f} files/s — ten times slower than when this was written"
