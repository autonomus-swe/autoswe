"""Semantic search against a real pgvector: index a repository, then ask it a question.

The unit tests cover chunking and the provider contract with no database. What only a real
database can show is the half this depends on Postgres for — that a 1024-wide vector round
trips through the column, that `<=>` orders by cosine distance the way the query assumes,
and that the similarity the caller sees is a bigger-is-better number rather than a raw
distance.

The provider here is `HashProvider`, which the module exports rather than the tests
defining: it is lexical, so a query matches on shared words, which is enough to check that
the right chunk comes back first without a key or a model download. What it cannot check is
whether an embedding understands that `TokenBucket` is about rate limiting — that is the
provider's job, not this pipeline's, and no test here pretends otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from repo import embeddings, symbols
from storage import repo as store
from tests.fakes import make_ctx
from tools.search import SearchCodeTool

pytestmark = pytest.mark.integration

SHA = "a" * 40


def build_repo(root: Path) -> None:
    """A tree where the answer to the planted question lives in exactly one file."""
    (root / "limits").mkdir()
    (root / "limits" / "bucket.py").write_text(
        "class TokenBucket:\n"
        "    def refill(self, elapsed):\n"
        "        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)\n"
        "        return self.tokens\n"
    )
    (root / "billing.py").write_text(
        "def invoice_total(items, tax):\n"
        "    subtotal = sum(i.price * i.quantity for i in items)\n"
        "    return subtotal + subtotal * tax\n"
    )
    (root / "diffs.py").write_text(
        "def parse_unified_diff(patch):\n"
        "    hunks = [h for h in patch.split('@@') if h.strip()]\n"
        "    return hunks\n"
    )


async def index(engine: AsyncEngine, root: Path, sha: str = SHA) -> int:
    parsed, _stats = symbols.parse_repo(root)
    return await embeddings.index_repo(engine, root, sha, parsed)


# ---- the round trip ------------------------------------------------------------------------


async def test_the_nearest_chunk_for_a_question_is_the_planted_one(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    build_repo(tmp_path)

    assert await index(engine, tmp_path) > 0

    hits = await embeddings.search(engine, SHA, "token bucket capacity refill rate", limit=3)

    assert hits, "indexed and then found nothing"
    path, line, first, score = hits[0]
    assert path == "limits/bucket.py", [h[0] for h in hits]
    assert line > 0 and "TokenBucket" in first, f"the result names nothing: {first!r}"
    assert 0.0 < score <= 1.0, "a similarity, not a distance"


async def test_results_come_back_best_first(engine: AsyncEngine, tmp_path: Path) -> None:
    """The ordering is the database's, not ours — worth pinning, because the operator
    returns distance and the caller reads the number as similarity."""
    build_repo(tmp_path)
    await index(engine, tmp_path)

    hits = await embeddings.search(engine, SHA, "parse unified diff patch hunks", limit=5)

    assert hits[0][0] == "diffs.py"
    assert [h[3] for h in hits] == sorted((h[3] for h in hits), reverse=True)


async def test_a_thousand_and_twenty_four_coordinates_survive_the_column(
    engine: AsyncEngine, tmp_path: Path, db: AsyncSession
) -> None:
    """A vector silently truncated at the column would still search, just badly."""
    build_repo(tmp_path)
    await index(engine, tmp_path)

    probe = [1.0] + [0.0] * (embeddings.DIMENSIONS - 1)
    rows = await store.nearest_chunks(db, SHA, probe, limit=1)

    assert rows, "no rows stored"
    assert len(list(rows[0][0].embedding)) == embeddings.DIMENSIONS


async def test_another_commit_sees_nothing(engine: AsyncEngine, tmp_path: Path) -> None:
    """The index describes a commit. A different base must not read a stale one."""
    build_repo(tmp_path)
    await index(engine, tmp_path)

    assert await embeddings.search(engine, "b" * 40, "token bucket refill") == []


async def test_a_commit_already_indexed_is_not_indexed_again(
    engine: AsyncEngine, tmp_path: Path, db: AsyncSession
) -> None:
    """Two runs against the same base share the vectors, for the reason the symbol index
    does: they describe the commit rather than the run."""
    build_repo(tmp_path)
    first = await index(engine, tmp_path)

    second = await index(engine, tmp_path)

    assert first > 0 and second == 0
    assert await count_rows(db) == first, "re-indexing duplicated rows"


async def count_rows(s: AsyncSession) -> int:
    result = await s.execute(
        text("select count(*) from repo_embeddings where repo_sha = :sha"), {"sha": SHA}
    )
    return int(result.scalar_one())


async def test_the_hnsw_index_is_the_one_the_migration_claims(engine: AsyncEngine) -> None:
    """An HNSW index that silently failed to build leaves a sequential scan that is correct
    and slow — which no query test would catch."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("select indexdef from pg_indexes where tablename = 'repo_embeddings'")
            )
        ).scalars()
    definitions = list(rows)

    assert any("USING hnsw" in d and "vector_cosine_ops" in d for d in definitions), definitions


# ---- what the agent actually calls -----------------------------------------------------------


async def test_search_code_answers_a_plain_words_question(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    build_repo(tmp_path)
    await index(engine, tmp_path)
    ctx = make_ctx(tmp_path, engine=engine, base_sha=SHA)

    result = await SearchCodeTool().run(ctx, semantic=True, query="token bucket refill capacity")

    assert not result.is_error
    assert "limits/bucket.py" in result.content
    assert "by meaning" in result.content


async def test_a_query_with_no_index_falls_back_to_text_and_says_so(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Nothing indexed this commit. Answering with silence would read as 'no such code'."""
    build_repo(tmp_path)
    ctx = make_ctx(tmp_path, engine=engine, base_sha=SHA)

    result = await SearchCodeTool().run(ctx, query="where is the token bucket refilled")

    assert "no semantic index" in result.content
    assert "bucket.py" in result.content, "the fallback still found it by words"


async def test_both_modes_together_keep_their_answers_apart(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """A chunk from an index and a line number from ripgrep are different kinds of answer;
    interleaving them hides which is which."""
    build_repo(tmp_path)
    await index(engine, tmp_path)
    ctx = make_ctx(tmp_path, engine=engine, base_sha=SHA)

    result = await SearchCodeTool().run(
        ctx, semantic=True, query="token bucket refill", pattern="invoice_total"
    )

    assert "by meaning" in result.content
    assert "billing.py" in result.content
    assert result.content.index("by meaning") < result.content.index("billing.py")
