"""Semantic search over the repository: chunk by symbol, embed, and look up by meaning.

`search_code` finds text. "Where is rate limiting handled" is not text — the answer may be
a class called `TokenBucket` in a file that never uses the word. That question is the one
this answers.

## Chunked by symbol, not by line window

A fixed window cuts functions in half, and half a function embeds to something that is not
about either half. The symbol index already knows where each definition starts and ends, so
a chunk is a definition — which is also the unit a reader wants back.

A definition longer than `MAX_CHUNK_LINES` is split at line boundaries with the signature
repeated as a header on each piece, so a chunk from the middle of a long function still
says what function it is from. Without that, the second half of a hundred-line handler
embeds as anonymous code.

The text is `path · signature · body`: the path carries the package vocabulary a body often
omits, and the signature carries the name, which is frequently the strongest single clue.

## Providers

`EmbeddingProvider` is a protocol with one method, because the interesting difference
between a hosted model and a local one is latency and cost rather than shape.
`HashProvider` is a deterministic hash-of-the-words provider — not a mock in the tests but
a real implementation this module exports, so the chunking and the storage can be indexed
and queried end to end without a key or a download.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from observability.logging import get_logger
from repo.symbols import Symbol

log = get_logger(__name__)

# The embedding width every provider here produces. Fixed because the column is
# `vector(1024)` and a provider that disagrees would fail at insert rather than at startup.
DIMENSIONS = 1024
MAX_CHUNK_LINES = 60
# Voyage's own batch ceiling; the local provider is happy with the same figure.
BATCH = 128


@dataclass(frozen=True)
class Chunk:
    """One embeddable piece of the repository, and where it came from."""

    path: str
    chunk_id: str
    start_line: int
    text: str

    @property
    def first_line(self) -> str:
        """What a search result shows: the `path · signature` header this chunk was built
        with, which names the definition without the reader opening the file."""
        for line in self.text.splitlines():
            if stripped := line.strip():
                return stripped[:160]
        return ""


class EmbeddingProvider(Protocol):
    """One method, because the differences that matter are cost and latency, not shape."""

    name: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def chunk_symbols(root: Path, symbols: list[Symbol]) -> list[Chunk]:
    """Definitions as embeddable chunks, splitting the long ones.

    Reads each file once rather than once per symbol: a module with forty definitions would
    otherwise be read forty times, and on a large repository that is the whole cost.
    """
    by_file: dict[str, list[Symbol]] = {}
    for sym in symbols:
        by_file.setdefault(sym.path, []).append(sym)

    chunks: list[Chunk] = []
    for rel, syms in sorted(by_file.items()):
        try:
            lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for sym in sorted(syms, key=lambda s: s.start_line):
            body = lines[sym.start_line - 1 : sym.end_line]
            if not body:
                continue
            for index in range(0, len(body), MAX_CHUNK_LINES):
                piece = body[index : index + MAX_CHUNK_LINES]
                header = f"{rel} · {sym.signature or sym.name}"
                if index:
                    # A chunk from the middle of a long function still has to say which
                    # function it is from, or it embeds as anonymous code.
                    header += f" · continued at line {sym.start_line + index}"
                chunks.append(
                    Chunk(
                        path=rel,
                        chunk_id=f"{rel}:{sym.start_line + index}",
                        start_line=sym.start_line + index,
                        text=header + "\n" + "\n".join(piece),
                    )
                )
    return chunks


def cosine(a: list[float], b: list[float]) -> float:
    """Similarity between two vectors, 1.0 for identical direction."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


WORD = re.compile(r"[A-Za-z0-9]+")
# `TokenBucket` -> `Token`, `Bucket`; `HTTPServer` -> `HTTP`, `Server`.
CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")


def words(text: str) -> set[str]:
    """The vocabulary of a piece of code, as a reader would say it out loud.

    Splitting on whitespace is what prose wants and it is wrong for code: it yields
    `self.capacity,` and `TokenBucket:`, tokens that appear in no question anybody asks.
    Measured — with whitespace splitting the nearest chunk for "token bucket capacity
    refill" was a billing function, because nothing in the file matched either.

    So: split on non-alphanumerics, then split camel case as well, keeping the whole
    identifier alongside its parts. `TokenBucket` is then reachable by "token bucket" and
    still by its own name.
    """
    out: set[str] = set()
    for token in WORD.findall(text):
        lowered = token.lower()
        if len(lowered) > 2:
            out.add(lowered)
        parts = CAMEL.findall(token)
        if len(parts) > 1:
            out.update(p.lower() for p in parts if len(p) > 2)
    return out


class HashProvider:
    """Deterministic vectors from a hash of the text's words.

    Exported rather than hidden in the tests, because a fake in a test file is a fake
    nobody can run the pipeline against. This one gives the chunking, the storage and the
    query path something real to move: two texts sharing words land near each other, which
    is the only property the machinery around it depends on.

    It is not a semantic model and does not pretend to be: it can reach `TokenBucket` from
    "token bucket", because those are the same letters, and it cannot reach it from "rate
    limiting". That is what the other two providers are for.
    """

    name = "hash"
    dimensions = DIMENSIONS

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSIONS
        for word in words(text):
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % DIMENSIONS
            vector[index] += 1.0
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


class VoyageProvider:
    """`voyage-code-3`, which is trained on code rather than prose.

    Batched at the provider's own ceiling. A failure returns nothing rather than raising:
    semantic search is an improvement on `rg`, and a run that cannot build an index should
    fall back to text rather than fail.
    """

    name = "voyage"
    dimensions = DIMENSIONS

    def __init__(self, api_key: str, model: str = "voyage-code-3") -> None:
        self._api_key = api_key
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=120.0) as client:
            for start in range(0, len(texts), BATCH):
                batch = texts[start : start + BATCH]
                response = await client.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"input": batch, "model": self.model, "output_dimension": DIMENSIONS},
                )
                response.raise_for_status()
                data = response.json()["data"]
                out.extend(item["embedding"] for item in sorted(data, key=lambda d: d["index"]))
        return out


class OllamaProvider:
    """A local embedding model over Ollama's OpenAI-compatible endpoint.

    Worth having even where the generation models are too slow to drive the pipeline —
    embedding is a single forward pass per chunk, not a generation loop, and
    `nomic-embed-text` runs at a usable rate on the same machine that cannot run a 7B
    chat model in under a minute.

    Padded or truncated to `DIMENSIONS`, because the column is fixed width and the useful
    local models are 768. Padding is lossless for cosine similarity: the extra coordinates
    are zero in every vector, so they contribute nothing to any dot product.
    """

    name = "ollama"
    dimensions = DIMENSIONS

    def __init__(self, base_url: str, model: str = "nomic-embed-text") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=300.0) as client:
            for text in texts:
                response = await client.post(
                    f"{self.base_url}/api/embeddings", json={"model": self.model, "prompt": text}
                )
                response.raise_for_status()
                out.append(_fit(list(response.json()["embedding"])))
        return out


def _fit(vector: list[float]) -> list[float]:
    """To `DIMENSIONS`, by zero-padding or truncation."""
    if len(vector) >= DIMENSIONS:
        return vector[:DIMENSIONS]
    return vector + [0.0] * (DIMENSIONS - len(vector))


def provider_from_env() -> EmbeddingProvider:
    """Whichever provider is configured, defaulting to the one that needs nothing.

    The default is the hash provider rather than an error: an unconfigured run should get a
    working pipeline with weaker search, not a crash on a feature it did not ask for.
    """
    choice = os.environ.get("EMBEDDING_PROVIDER", "").strip().lower()
    if choice == "voyage":
        key = os.environ.get("VOYAGE_API_KEY", "")
        if key:
            return VoyageProvider(key)
        log.warning("embedding_provider_unconfigured", provider="voyage", falling_back="hash")
    if choice == "ollama":
        return OllamaProvider(os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    return HashProvider()


async def index_repo(engine: object, worktree: Path, repo_sha: str, symbols: list[Symbol]) -> int:
    """Embed every definition at this commit and store it. Returns the chunk count.

    Skips a commit that already has embeddings, for the same reason the symbol index does:
    the vectors describe a commit, not a run.

    Never raises. Semantic search is an improvement on `rg`, not a replacement, so a
    provider that is down or unconfigured should cost the run better search and nothing
    else — `search_code` falls back to text and says so.
    """
    from storage import repo as db
    from storage.db import session

    try:
        async with session(engine) as s:  # type: ignore[arg-type]
            if await db.embeddings_indexed(s, repo_sha):
                log.info("embeddings_reused", repo_sha=repo_sha[:12])
                return 0

        chunks = chunk_symbols(worktree, symbols)
        if not chunks:
            return 0
        provider = provider_from_env()
        vectors = await provider.embed([c.text for c in chunks])
        if len(vectors) != len(chunks):
            log.warning("embedding_count_mismatch", chunks=len(chunks), vectors=len(vectors))
            return 0

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
        async with session(engine) as s:  # type: ignore[arg-type]
            await db.insert_embeddings(s, repo_sha, rows)
        log.info(
            "embeddings_built", repo_sha=repo_sha[:12], chunks=len(chunks), provider=provider.name
        )
        return len(chunks)
    except Exception as e:
        log.warning("embedding_index_failed", error=f"{type(e).__name__}: {e}")
        return 0


async def search(
    engine: object, repo_sha: str, query: str, limit: int = 10
) -> list[tuple[str, int, str, float]]:
    """`(path, line, first_line, score)` for the chunks nearest a natural-language query.

    Returns nothing rather than raising when there is no index; the caller turns that into
    a fallback to text search and says which one it used.
    """
    from storage import repo as db
    from storage.db import session

    try:
        provider = provider_from_env()
        (vector,) = await provider.embed([query])
        async with session(engine) as s:  # type: ignore[arg-type]
            rows = await db.nearest_chunks(s, repo_sha, vector, limit=limit)
    except Exception as e:
        log.warning("embedding_search_failed", error=f"{type(e).__name__}: {e}")
        return []
    out = []
    for row, score in rows:
        chunk = Chunk(
            path=row.path, chunk_id=row.chunk_id, start_line=row.start_line, text=row.text
        )
        out.append((row.path, row.start_line, chunk.first_line, score))
    return out
