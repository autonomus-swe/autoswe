"""Chunking and the provider contract: everything about embeddings that needs no database.

The chunking is where the judgement is. A fixed line window cuts functions in half and half
a function embeds to something that is about neither half, so chunks follow the symbol
index — and the one case worth real care is a definition too long for a single chunk, where
the piece from the middle has to still say which function it came from.

`HashProvider` is exported by the module rather than defined here on purpose. A fake that
lives in a test file is one nobody can run the pipeline against; this one is real enough to
index and query with, and honest about being lexical rather than semantic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repo import embeddings
from repo.symbols import Symbol

pytestmark = pytest.mark.unit


def sym(path: str, name: str, start: int, end: int, signature: str = "") -> Symbol:
    return Symbol(
        path=path,
        kind="function",
        name=name,
        signature=signature or f"def {name}():",
        start_line=start,
        end_line=end,
        refs=[],
    )


# ---- chunking ---------------------------------------------------------------------------


def test_a_chunk_is_a_definition_and_carries_where_it_came_from(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pages.py").write_text(
        "import os\n\n\ndef paginate(items, size):\n    return items[:size]\n"
    )

    chunks = embeddings.chunk_symbols(
        tmp_path, [sym("app/pages.py", "paginate", 4, 5, "def paginate(items, size):")]
    )

    (chunk,) = chunks
    assert chunk.path == "app/pages.py" and chunk.start_line == 4
    assert "def paginate(items, size):" in chunk.text
    assert "return items[:size]" in chunk.text
    assert "app/pages.py" in chunk.text, "the path carries vocabulary the body omits"
    assert "import os" not in chunk.text, "and stops at the definition"


def test_a_long_definition_splits_with_its_signature_repeated(tmp_path: Path) -> None:
    """The piece from the middle of a hundred-line handler still has to say which handler.
    Without the header it embeds as anonymous code and matches nothing anyone asks for."""
    body = "\n".join(f"    step_{i}()" for i in range(embeddings.MAX_CHUNK_LINES * 2))
    (tmp_path / "handler.py").write_text(f"def handle(request):\n{body}\n")

    chunks = embeddings.chunk_symbols(
        tmp_path,
        [
            sym(
                "handler.py",
                "handle",
                1,
                embeddings.MAX_CHUNK_LINES * 2 + 1,
                "def handle(request):",
            )
        ],
    )

    assert len(chunks) == 3, [c.start_line for c in chunks]
    assert all("def handle(request):" in c.text for c in chunks)
    assert "continued at line" in chunks[1].text
    assert [c.start_line for c in chunks] == [1, 61, 121]
    assert len({c.chunk_id for c in chunks}) == 3, "each piece is addressable on its own"


def test_a_file_that_vanished_between_indexing_and_chunking_is_skipped(tmp_path: Path) -> None:
    """The worktree is live; a symbol row can outlive the file it describes."""
    assert embeddings.chunk_symbols(tmp_path, [sym("gone.py", "f", 1, 2)]) == []


def test_each_file_is_read_once_however_many_definitions_it_has(tmp_path: Path) -> None:
    """A module with forty definitions read forty times is the whole cost on a large
    repository."""
    (tmp_path / "many.py").write_text("\n".join(f"def f{i}():\n    return {i}" for i in range(20)))
    reads = 0
    original = Path.read_text

    def counting(self: Path, *a: object, **kw: object) -> str:
        nonlocal reads
        if self.name == "many.py":
            reads += 1
        return original(self, *a, **kw)  # type: ignore[arg-type]

    symbols = [sym("many.py", f"f{i}", i * 2 + 1, i * 2 + 2) for i in range(20)]
    Path.read_text = counting  # type: ignore[method-assign]
    try:
        chunks = embeddings.chunk_symbols(tmp_path, symbols)
    finally:
        Path.read_text = original  # type: ignore[method-assign]

    assert len(chunks) == 20
    assert reads == 1, f"read the file {reads} times"


def test_the_first_line_of_a_result_is_something_a_reader_recognises(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def paginate(items):\n    return items\n")

    (chunk,) = embeddings.chunk_symbols(tmp_path, [sym("a.py", "paginate", 1, 2)])

    assert chunk.first_line.startswith("a.py"), chunk.first_line


# ---- the hash provider ---------------------------------------------------------------------


async def test_the_hash_provider_is_deterministic_and_the_right_width() -> None:
    provider = embeddings.HashProvider()

    first, second = await provider.embed(["def paginate(items, size)", "def paginate(items, size)"])

    assert first == second
    assert len(first) == embeddings.DIMENSIONS


async def test_texts_that_share_words_land_nearer_than_texts_that_do_not() -> None:
    """The only property the machinery around it depends on. It is lexical, not semantic —
    it cannot tell you `TokenBucket` is about rate limiting, and does not claim to."""
    provider = embeddings.HashProvider()

    related, same_topic, unrelated = await provider.embed(
        [
            "rate limiting token bucket refill",
            "token bucket rate limiting capacity",
            "parse a unified diff into files",
        ]
    )

    assert embeddings.cosine(related, same_topic) > embeddings.cosine(related, unrelated)


def test_code_is_tokenised_as_code_rather_than_as_prose() -> None:
    """The bug the prose test above could not see. Splitting on whitespace gives
    `self.capacity,` and `TokenBucket:` — tokens no question contains — and with it the
    nearest chunk for "token bucket capacity refill" was a billing function."""
    vocabulary = embeddings.words("class TokenBucket:\n    self.capacity = capacity\n")

    assert {"token", "bucket", "capacity"} <= vocabulary
    assert "tokenbucket" in vocabulary, "and the identifier itself stays reachable"
    assert not any(w.endswith((",", ":", ".")) for w in vocabulary)


def test_an_acronym_splits_where_the_next_word_starts() -> None:
    assert {"http", "server"} <= embeddings.words("HTTPServer")


async def test_an_empty_text_embeds_without_dividing_by_zero() -> None:
    (vector,) = await embeddings.HashProvider().embed([""])

    assert len(vector) == embeddings.DIMENSIONS and not any(vector)


def test_cosine_is_one_for_a_vector_against_itself() -> None:
    v = [0.5, 0.5, 0.7071]
    assert embeddings.cosine(v, v) == pytest.approx(1.0, abs=1e-6)
    assert embeddings.cosine(v, [0.0] * 3) == 0.0, "and zero rather than an error"


# ---- provider selection -----------------------------------------------------------------


def test_an_unconfigured_run_gets_a_working_pipeline_with_weaker_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaulting to an error would fail a run over a feature it never asked for."""
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)

    assert embeddings.provider_from_env().name == "hash"


def test_asking_for_voyage_without_a_key_says_so_and_carries_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "voyage")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    assert embeddings.provider_from_env().name == "hash"


def test_asking_for_ollama_gets_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")

    provider = embeddings.provider_from_env()

    assert provider.name == "ollama" and provider.dimensions == embeddings.DIMENSIONS


def test_a_narrower_local_model_is_padded_to_the_column_width() -> None:
    """The useful local models are 768 wide and the column is 1024. Padding is lossless
    for cosine: the extra coordinates are zero in every vector."""
    padded = embeddings._fit([1.0, 0.0] * 384)

    assert len(padded) == embeddings.DIMENSIONS
    assert embeddings.cosine(padded, embeddings._fit([1.0, 0.0] * 384)) == pytest.approx(1.0)
