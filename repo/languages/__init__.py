"""Per-language tree-sitter queries for the symbol index.

A language is two queries and a mapping from node type to what we call that thing. Adding
one is a file here, not a build step — `tree-sitter-language-pack` ships the grammars for
all of them in a single wheel, which is why it is a dependency rather than four.

Each module exports:

``DEFINITIONS``
    A query capturing ``@def`` (the whole node, for its range and first line) and ``@name``
    (the identifier). Paired per *match* rather than per capture, because a flat capture
    list cannot say which name belongs to which definition.
``REFERENCES``
    A query capturing ``@ref``: the identifier in a call or an attribute access. These are
    assigned to the innermost definition that contains them, which is what turns the table
    into something a ranking pass can read as a call graph.
``KINDS``
    Node type -> the word we store. `function`, `class`, `method`, `type`, `interface`,
    `const`.
"""

from __future__ import annotations

from dataclasses import dataclass

from repo.languages import go, javascript, python, typescript


@dataclass(frozen=True)
class Language:
    name: str
    definitions: str
    references: str
    kinds: dict[str, str]


LANGUAGES: dict[str, Language] = {
    module.NAME: Language(module.NAME, module.DEFINITIONS, module.REFERENCES, module.KINDS)
    for module in (python, javascript, typescript, go)
}

# Suffix -> language name. `.tsx` parses as typescript here rather than as its own grammar:
# the definitions we index (functions, classes, methods, interfaces) are identical, and the
# JSX the two disagree about contains none of them.
BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
}


def for_path(suffix: str) -> Language | None:
    name = BY_SUFFIX.get(suffix.lower())
    return LANGUAGES[name] if name else None
