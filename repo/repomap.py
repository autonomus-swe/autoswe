"""The repo map: which files matter for this goal, and what they define.

Two renderers, and which one runs depends on whether the symbol index is available.

**v2** ranks by two independent signals and shows definitions rather than file sizes:

- *Centrality* (`repo/graph.py`) — how much of the tree leans on this file. Steady across
  goals, and the reason a settings module ranks above a leaf even when neither is named.
- *Lexical relevance* (BM25 over symbol names and path tokens) — how much this file's
  vocabulary matches the goal. Volatile, and the reason a goal naming `paginate` finds
  `paginate`'s file whatever the graph says.

Neither alone is enough: centrality alone returns the same answer to every question, and
BM25 alone ranks a file that merely mentions a word above the one that defines the thing.
The score is `0.6 * centrality + 0.4 * lexical`, each normalised, and the weighting is the
phase document's.

**v1** is the fallback: an indented tree ordered by where files sit. It runs when there is
no index — an unsupported language, or a tree nothing parsed — and is a guess about
importance dressed as an answer, which is why v2 exists.

`render_map`'s signature only grew optional arguments, so the three callers that pass a
worktree and nothing else still work and simply get v1.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from repo import graph as graphmod
from repo.symbols import Symbol
from repo.walk import SKIP_DIRS

MAX_FILE_BYTES = 1_000_000
BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".whl",
        ".so",
        ".dylib",
        ".dll",
        ".bin",
        ".pyc",
        ".lock",
    }
)
MANIFESTS = frozenset(
    {
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "Makefile",
        "requirements.txt",
        "setup.py",
        "Dockerfile",
        "README.md",
    }
)
SOURCE_DIRS = ("src", "app", "lib", "internal", "cmd", "pkg")


SOURCE_SUFFIXES = frozenset({".py", ".js", ".ts", ".tsx", ".go", ".rs", ".rb", ".java", ".php"})


def _source_dirs(root: Path) -> set[str]:
    """Top-level directories that actually hold source, whatever they are called.

    Naming conventions vary — src/, app/, or flat packages like core/ and gateway/ — so
    the map ranks a directory by what is inside it rather than by its name.
    """
    found: set[str] = set()
    for path in root.rglob("*"):
        if path.suffix.lower() not in SOURCE_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts) or len(rel.parts) < 2:
            continue
        found.add(rel.parts[0])
    return found


def _rank(rel: Path, is_dir: bool, source_dirs: set[str]) -> tuple[int, str]:
    """Lower sorts first: manifests, source, other, tests, docs, dotfiles."""
    name = rel.name
    top = rel.parts[0] if rel.parts else name
    if not is_dir and rel.parent == Path(".") and name in MANIFESTS:
        return (0, name)
    if top in ("tests", "test", "spec"):
        return (3, str(rel))
    if top in ("docs", "doc", "examples"):
        return (4, str(rel))
    if top.startswith("."):  # config and tooling: real, but not what you read first
        return (5, str(rel))
    if top in source_dirs or top in SOURCE_DIRS:
        return (1, str(rel))
    return (2, str(rel))


def _keep(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        return path.stat().st_size <= MAX_FILE_BYTES
    except OSError:
        return False


def _human(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size // 1024}K"
    return f"{size // (1024 * 1024)}M"


def render_map(worktree: Path, max_lines: int = 150) -> str:
    """An indented tree of the repository, most relevant first, within ``max_lines``."""
    root = Path(worktree)
    if not root.is_dir():
        return "(no worktree)"

    source_dirs = _source_dirs(root)
    entries: list[tuple[tuple[int, str], Path, bool, int]] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        if path.is_dir():
            entries.append((_rank(rel, True, source_dirs), rel, True, 0))
        elif _keep(path):
            entries.append((_rank(rel, False, source_dirs), rel, False, path.stat().st_size))

    if not entries:
        return "(empty repository)"

    entries.sort(key=lambda e: e[0])
    shown: list[str] = []
    per_dir_shown: dict[Path, int] = {}
    per_dir_total: dict[Path, int] = {}
    for _, rel, is_dir, _size in entries:
        if not is_dir:
            per_dir_total[rel.parent] = per_dir_total.get(rel.parent, 0) + 1

    for _rk, rel, is_dir, size in entries:
        if len(shown) >= max_lines:
            break
        indent = "  " * (len(rel.parts) - 1)
        if is_dir:
            shown.append(f"{indent}{rel.name}/")
            continue
        parent = rel.parent
        per_dir_shown[parent] = per_dir_shown.get(parent, 0) + 1
        shown.append(f"{indent}{rel.name}  {_human(size)}")

    omitted = sum(per_dir_total.values()) - sum(per_dir_shown.values())
    if omitted > 0:
        shown.append(f"… (+{omitted} more files)")
    return "\n".join(shown)


# ---- v2: ranked by centrality and lexical relevance -------------------------------------

# Characters per token, measured rather than assumed.
#
# This was 4 — the rule of thumb for English prose — and for code it is badly wrong.
# Measured with a real BPE tokenizer (`cl100k_base`) over the rendered map of two large
# repositories: 2.69 chars/token on sympy, 2.90 on pydantic. Identifiers, punctuation and
# the `[L12-34]` line ranges all tokenize densely.
#
# The consequence was not academic: a map budgeted at 3 500 "tokens" rendered 5 193 real
# ones on sympy, so the Phase 5 criterion of "under 4 000 tokens" was being reported as met
# while it was missed by a third. The phase document asks for exactly this calibration and
# says it needs a funded key for `count_tokens`; it does not — a local tokenizer settles it.
#
# 2.6 rather than the measured 2.69, because the safe direction is to overestimate tokens:
# that makes the map slightly shorter than it could be, where the other direction makes
# every budget a lie.
CHARS_PER_TOKEN = 2.6
DEFAULT_TOKEN_BUDGET = 3_500
# Held back from the block budget for the "other files" header and its "… and N more"
# line. Generous: the two together are under a hundred characters, and spending a few more
# on a map is cheaper than a map that quietly exceeds the budget it advertises.
TAIL_RESERVE_CHARS = 160
# A test is worth showing and rarely worth showing first, unless the goal is about tests.
TEST_PENALTY = 0.7
TEST_WORDS = ("test", "tests", "spec", "pytest", "coverage", "fixture")

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize(text: str) -> list[str]:
    """Words from an identifier or a path: split on case, underscores and separators.

    `parse_json_report` and `parseJSONReport` both have to match a goal that says "parse
    report", or the lexical half only works for code written in one house style.
    """
    spaced = _CAMEL.sub(" ", text)
    parts = re.split(r"[^A-Za-z0-9]+", spaced)
    return [p.lower() for p in parts if len(p) > 1]


def _documents(files: list[str], symbols: list[Symbol]) -> dict[str, list[str]]:
    """One bag of words per file: its path, and the names of everything it defines."""
    docs: dict[str, list[str]] = {f: tokenize(f) for f in files}
    for sym in symbols:
        if sym.path in docs:
            docs[sym.path].extend(tokenize(sym.name))
    return docs


def _normalise(scores: dict[str, float]) -> dict[str, float]:
    """To 0..1. A flat input maps to zeros rather than dividing by nothing."""
    if not scores:
        return {}
    low, high = min(scores.values()), max(scores.values())
    if high - low <= 0:
        return dict.fromkeys(scores, 0.0)
    return {k: (v - low) / (high - low) for k, v in scores.items()}


def rank_files(
    files: list[str],
    symbols: list[Symbol],
    ranks: dict[str, float],
    *,
    goal: str = "",
    pinned: Sequence[str] = (),
) -> list[tuple[str, float]]:
    """Files in the order a reader should see them, best first."""
    from rank_bm25 import BM25Okapi

    docs = _documents(files, symbols)
    corpus = [docs[f] for f in files]
    query = tokenize(goal) + [tokenize(p)[-1] if tokenize(p) else p for p in pinned]
    lexical: dict[str, float] = dict.fromkeys(files, 0.0)
    if query and any(corpus):
        scores = BM25Okapi(corpus).get_scores(query)
        lexical = dict(zip(files, (float(s) for s in scores), strict=True))

    central = _normalise({f: ranks.get(f, 0.0) for f in files})
    lexical = _normalise(lexical)
    goal_wants_tests = any(word in goal.lower() for word in TEST_WORDS)
    pinned_set = set(pinned)

    scored: list[tuple[str, float]] = []
    for f in files:
        score = 0.6 * central[f] + 0.4 * lexical[f]
        if graphmod.is_test(f) and not goal_wants_tests:
            score *= TEST_PENALTY
        if f in pinned_set or (PurePosixPath(f).parent == PurePosixPath(".") and f in MANIFESTS):
            # Pinned to the top rather than boosted: the plan named this file, so a reader
            # who does not see it will wonder whether the map knew about it.
            score += 10.0
        scored.append((f, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def _fit_block(block: list[str], budget: int) -> list[str]:
    """As much of one file's block as fits, keeping the path and saying what was dropped."""
    kept = [block[0]]
    used = len(block[0]) + 1
    for line in block[1:]:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    dropped = len(block) - len(kept)
    while dropped:
        note = f"  … {dropped} more definitions"
        if used + len(note) + 1 <= budget:
            kept.append(note)
            break
        if len(kept) == 1:
            # Not even the path and a note fit. The path alone is the floor — a map cannot
            # say less than which file it would have shown — so the note goes rather than
            # the budget.
            break
        used -= len(kept.pop()) + 1
        dropped = len(block) - len(kept)
    return kept


def render_symbol_map(
    worktree: Path,
    symbols: list[Symbol],
    ranks: dict[str, float],
    *,
    goal: str = "",
    pinned: Sequence[str] = (),
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> str:
    """The v2 map: ranked files, each with what it defines, inside a token budget.

    Files that did not fit are listed by path alone under "other files", because "this file
    exists and I ran out of room" and "this file does not exist" are different facts and a
    truncated map should not conflate them.

    That tail is inside the budget too. It used to be appended after the loop had finished,
    which made `token_budget` a budget for the ranked half and not for the map — on a two
    thousand file repository the overshoot was half as much again.
    """
    files = sorted({s.path for s in symbols})
    if not files:
        return ""
    by_file: dict[str, list[Symbol]] = {}
    for sym in symbols:
        by_file.setdefault(sym.path, []).append(sym)

    order = rank_files(files, symbols, ranks, goal=goal, pinned=pinned)
    budget = int(token_budget * CHARS_PER_TOKEN)
    # Room held back for the "other files" tail, which is only known to be needed once the
    # blocks have been laid out. Without it the blocks fill the budget exactly and the tail
    # goes over — measured on sympy at forty characters, entirely header. The reserve is
    # returned when nothing is omitted.
    block_budget = max(budget - TAIL_RESERVE_CHARS, budget // 2)
    lines: list[str] = []
    used = 0
    omitted: list[str] = []

    for path, _score in order:
        block = [path]
        for sym in sorted(by_file.get(path, []), key=lambda s: s.start_line):
            indent = "    " if sym.kind == "method" else "  "
            label = sym.signature or f"{sym.kind} {sym.name}"
            block.append(f"{indent}{label}  [L{sym.start_line}-{sym.end_line}]")
        cost = sum(len(line) + 1 for line in block)
        if used + cost > block_budget:
            if lines:
                omitted.append(path)
                continue
            # The first block, and it does not fit on its own. It still goes in — a map
            # with nothing in it answers no question — but truncated to the allowance
            # rather than whole. Emitting it whole is what `and lines` used to do, and on a
            # small budget against a large file that was a fifty-fold overshoot: the
            # budget stopped being a bound in exactly the case a caller set a small one for.
            block = _fit_block(block, block_budget)
            cost = sum(len(line) + 1 for line in block)
        lines.extend(block)
        used += cost

    if omitted:
        # Inside the budget, not after it. This tail used to be a flat `omitted[:200]`
        # appended once the loop had finished, which meant the map overshot its own budget
        # by however long the tail was — measured on sympy: a 3 500-token budget rendering
        # a 5 300-token map, because 1 200 omitted paths capped at 200 is still ~1 800
        # tokens of trailing list.
        header = f"\nother files ({len(omitted)}):"
        if used + len(header) + 1 > budget:
            # No room even to say how many were left out. Saying it anyway is how the
            # budget became advisory in the first place.
            return "\n".join(lines)
        used += len(header) + 1
        listed = 0
        for path in omitted:
            line = f"  {path}"
            if used + len(line) + 1 > budget:
                break
            used += len(line) + 1
            listed += 1
        lines.append(header)
        if listed < len(omitted):
            # The count in the header is the real one; this says how much of it is shown,
            # so a reader can tell a short list from a complete one. Its own length is
            # charged too, dropping listed paths until it fits — the first version of this
            # fix appended it after the check and put the map forty characters over, which
            # is the same mistake it was written to correct, just smaller.
            suffix = f"  … and {len(omitted) - listed} more not shown"
            while listed > 0 and used + len(suffix) + 1 > budget:
                listed -= 1
                used -= len(f"  {omitted[listed]}") + 1
                suffix = f"  … and {len(omitted) - listed} more not shown"
            lines.extend(f"  {p}" for p in omitted[:listed])
            if used + len(suffix) + 1 <= budget:
                lines.append(suffix)
        else:
            lines.extend(f"  {p}" for p in omitted[:listed])
    return "\n".join(lines)
