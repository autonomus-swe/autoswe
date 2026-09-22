"""`docs/security.md` points at a test for every claim. This checks the pointers resolve.

A security document is a list of promises. Each one is only worth the test beside it, and
a reference that has quietly stopped resolving — a renamed test, a deleted file — turns a
promise into a claim nobody is checking. That decay is silent and it is exactly the kind
this project keeps finding.

So the references are parsed out of the markdown and looked up in the source. Not run:
running them is the suite's job. What this asserts is that the thing named exists.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "security.md"
# `tests/unit/test_x.py::test_name`, or a bare `::test_name` continuing the file above it
# (the table uses `…::test_name` to avoid repeating a long path on every row).
REFERENCE = re.compile(r"(tests/[\w/]+\.py)?::(\w+)")
PATH_ONLY = re.compile(r"`(tests/[\w/]+\.py)`")


def references() -> list[tuple[str, str]]:
    """`(file, test)` pairs in document order, resolving `…::name` to the file above it."""
    found: list[tuple[str, str]] = []
    current = ""
    for line in DOC.read_text().splitlines():
        for match in REFERENCE.finditer(line):
            path, name = match.group(1), match.group(2)
            if path:
                current = path
            if not current:
                raise AssertionError(f"`::{name}` with no file named before it: {line}")
            found.append((current, name))
    return found


def test_the_document_cites_tests_at_all() -> None:
    """A guard on the guard: a parser that silently matched nothing would make every
    assertion below vacuously true."""
    assert len(references()) >= 25


@pytest.mark.parametrize(("path", "name"), references(), ids=lambda v: str(v))
def test_every_cited_test_exists(path: str, name: str) -> None:
    file = ROOT / path
    assert file.is_file(), f"{path} does not exist"
    tree = ast.parse(file.read_text())
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert name in defined, f"{path} has no {name}"


def test_every_cited_file_exists() -> None:
    """Rows that name a file without a test, e.g. a whole module of them."""
    for path in PATH_ONLY.findall(DOC.read_text()):
        assert (ROOT / path).is_file(), f"{path} does not exist"


def test_the_document_says_what_it_does_not_guarantee() -> None:
    """The section worth reading twice. A security document that only lists its wins is
    marketing, and the limits are what someone deciding whether to run this needs."""
    text = DOC.read_text()
    assert "does **not** guarantee" in text
    for limit in ("gVisor", "Prompt injection is mitigated, not solved", "shared secret"):
        assert limit in text, f"the limits section stopped mentioning {limit!r}"


INDEX = ROOT / "docs" / "README.md"
LINK = re.compile(r"\[`([^`]+)`\]\(([^)]+)\)")


def test_every_link_in_the_docs_index_resolves() -> None:
    """The index is the first thing anyone reads and the easiest thing to leave stale.

    A broken link in it is worse than a missing page, because it says the page exists.
    """
    broken = [
        target
        for _, target in LINK.findall(INDEX.read_text())
        if not (INDEX.parent / target).resolve().is_file()
    ]
    assert broken == []


def test_the_index_lists_every_document_in_docs() -> None:
    """A page nobody links to is a page nobody reads. This is what stops the next document
    from being written and then quietly orphaned."""
    listed = {(INDEX.parent / target).resolve() for _, target in LINK.findall(INDEX.read_text())}
    on_disk = {p.resolve() for p in INDEX.parent.glob("*.md") if p.name != "README.md"}
    assert on_disk - listed == set()
