"""`search_code(semantic=True)` finding code by meaning, against a real embedding model.

The Phase 5 criterion is specific: *"where is rate limiting handled" returns the right file
on the fixture auth service*. That query is the whole exercise, because the file it should
return — `app/auth/bucket.py` — does not contain the words "rate" or "limit" anywhere. A
grep finds nothing. A lexical index finds nothing. Only something that knows a token bucket
*is* a rate limiter can answer it.

Which is why `tests/integration/test_embeddings.py` does not settle this. It queries with
words that overlap the target ("token bucket capacity refill") and embeds with
`HashProvider`, whose own docstring says it cannot connect `TokenBucket` to rate limiting.
That test proves the plumbing works; this one proves the plumbing carries meaning.

The control is the point. Every test here that asserts the model finds the file is paired
with one asserting the hash provider does *not* — because a semantic test that a lexical
index also passes has measured nothing.

Skipped when Ollama or `nomic-embed-text` is absent. It needs no key and no quota: an
embedding is one forward pass per chunk, not a generation loop, which is why this criterion
is reachable on a laptop while the ones needing a chat model are not.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from repo import embeddings, symbols

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "auth_service"
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = "nomic-embed-text"
SHA = "f" * 40
# The criterion's query, verbatim.
QUERY = "where is rate limiting handled"
TARGET = "app/auth/bucket.py"


def _model_ready() -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
            import json

            names = [m.get("name", "") for m in json.load(r).get("models", [])]
        return any(n.startswith(MODEL) for n in names)
    except Exception:
        return False


requires_model = pytest.mark.skipif(
    not _model_ready(), reason=f"{MODEL} not available at {OLLAMA} (`ollama pull {MODEL}`)"
)


async def index(engine: AsyncEngine, provider: object, sha: str) -> int:
    parsed, _stats = symbols.parse_repo(FIXTURE)
    chunks = embeddings.chunk_symbols(FIXTURE, parsed)
    vectors = await provider.embed([c.text for c in chunks])  # type: ignore[attr-defined]
    from storage import repo as db
    from storage.db import session

    rows = [
        {
            "path": c.path,
            "chunk_id": c.chunk_id,
            "start_line": c.start_line,
            "embedding": v,
            "text": c.text[:8000],
        }
        for c, v in zip(chunks, vectors, strict=True)
    ]
    async with session(engine) as s:
        await db.insert_embeddings(s, sha, rows)
    return len(rows)


# ---- the criterion ---------------------------------------------------------------------


@requires_model
async def test_the_criterion_query_finds_the_file_that_never_says_those_words(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Phase 5 exit criterion, run against a real embedding model.

    `app/auth/bucket.py` implements a token bucket and contains neither "rate" nor "limit";
    the test asserts that separately below, so this cannot quietly become a keyword match.
    """
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_URL", OLLAMA)
    assert await index(engine, embeddings.OllamaProvider(OLLAMA), SHA) > 0

    hits = await embeddings.search(engine, SHA, QUERY, limit=3)

    assert hits, "indexed and then found nothing — see `warm_model` if the provider is down"
    assert hits[0][0] == TARGET, [h[0] for h in hits]
    # The margin, so a future regression that squeaks past is visible rather than green.
    # Measured at ~0.53 against every chunk of every other file, and deterministic across
    # repeated runs, so this is a wide bar rather than a tuned one.
    assert hits[0][3] > 0.45, f"the top hit barely won: {hits}"


@requires_model
async def test_the_hash_provider_cannot_answer_the_same_question(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control, and the reason the test above means anything.

    `HashProvider` is lexical. The query shares no word with the target, so it has nothing
    to go on — and if it somehow ranked the file first, the test above would be passing on
    word overlap rather than meaning.
    """
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    sha = "e" * 40
    assert await index(engine, embeddings.HashProvider(), sha) > 0

    hits = await embeddings.search(engine, sha, QUERY, limit=5)

    scores = [round(score, 4) for *_rest, score in hits]
    assert all(s == 0.0 for s in scores), f"the query should share no word with anything: {scores}"


def test_the_target_really_does_not_contain_the_words() -> None:
    """If this ever fails, the test above has stopped being a test of semantics — someone
    has added a comment mentioning rate limiting and a lexical index would now win."""
    import re

    source = "\n".join(p.read_text() for p in (FIXTURE / "app").rglob("*.py"))

    assert not re.search(r"\brate\b|\blimit|\bthrottl", source, re.I)


def _paths(hits: list[tuple[str, int, str, float]]) -> list[str]:
    return [h[0] for h in hits]


def _rank(hits: list[tuple[str, int, str, float]], path: str) -> int:
    """Where a file came, or past the end when it did not appear at all."""
    paths = _paths(hits)
    return paths.index(path) if path in paths else len(paths)


# ---- that it is the meaning and not the path -------------------------------------------


@requires_model
async def test_a_question_about_a_different_concern_finds_a_different_file(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One right answer could be luck — the target is a short file with a distinctive
    shape, and an index that returned it for everything would pass the test above.

    What is asserted is discrimination, not a ranking. Picking a #1 for these queries would
    be tuning the test to the model: `routes.py` defines
    `login(username, password, stored_hash, client_ip)` and is a perfectly good answer to a
    question about credentials, and demanding `passwords.py` there would only mean I kept
    rewording the query until it agreed with me.
    """
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_URL", OLLAMA)
    sha = "d" * 40
    await index(engine, embeddings.OllamaProvider(OLLAMA), sha)

    hashing = await embeddings.search(engine, sha, "how are user credentials stored safely", 3)
    sessions = await embeddings.search(engine, sha, "how is a login session proved valid", 3)

    # Outranks, rather than absent: the fixture has five files, so a top-three is most of
    # the corpus and "not in it" would be a claim about corpus size rather than about
    # meaning. That the right file beats the bucket is the thing being asserted.
    assert _rank(hashing, "app/auth/passwords.py") < _rank(hashing, TARGET), _paths(hashing)
    assert _rank(sessions, "app/auth/tokens.py") < _rank(sessions, TARGET), _paths(sessions)
