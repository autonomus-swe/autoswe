"""SQLAlchemy 2.0 models for README §7. Additive columns beyond the README DDL are nullable
or defaulted, so the README stays true. The migration in ``storage/migrations/versions`` is
hand-written; ``tests/integration/test_storage.py`` asserts it matches these models exactly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    Uuid,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Must match `repo.embeddings.DIMENSIONS`. Named here rather than imported because
# `storage` sits below `repo` and the column width is a schema fact, not a model choice.
EMBEDDING_DIMENSIONS = 1024


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[str]: ARRAY(Text)}


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RunRow(Base):
    __tablename__ = "runs"
    # For the collector's "which runs finished before this?", asked every ten minutes
    # forever. Declared here as well as in migration 0005 so `compare_metadata` does not
    # read it as drift and propose dropping it.
    __table_args__ = (Index("ix_runs_status_finished_at", "status", "finished_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    base_branch: Mapped[str] = mapped_column(Text, nullable=False)
    work_branch: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, server_default="0")
    pr_url: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="anthropic")
    unattended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # What SETUP resolved: the commit this run actually started from.
    base_sha: Mapped[str | None] = mapped_column(Text)
    # What the caller asked for. Null means "the head of `base_branch`, whatever that was
    # when the run started" — and keeping the two apart is what makes "the branch moved
    # under us" a visible fact rather than a rewritten one. See migration 0007.
    base_commit: Mapped[str | None] = mapped_column(Text)
    # `owner/repo` of the repository the pull request is opened on, when that is not
    # `repo_url`. Null for the ordinary case, where a run pushes to and opens on the same
    # repository — see migration 0006.
    upstream: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (PrimaryKeyConstraint("run_id", "id", name="pk_tasks"),)

    id: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    depends_on: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    files: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    acceptance_criteria: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    test_selector: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    replanned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class StepRow(Base):
    """One row per agent invocation."""

    __tablename__ = "steps"
    __table_args__ = (Index("ix_steps_run_id", "run_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(Text)
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ToolCallRow(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_step_id", "step_id"),
        Index("ix_tool_calls_step_id_seq", "step_id", "seq"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Rows written in one transaction share created_at, so replaying a run's actions in
    # order needs a monotonic column (same reason artifacts has one).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    step_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("steps.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_preview: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    approved_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class LLMCallRow(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_step_id", "step_id"),
        Index("ix_llm_calls_step_id_seq", "step_id", "seq"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    step_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("steps.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    effort: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    stop_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class CheckpointRow(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (PrimaryKeyConstraint("run_id", "seq", name="pk_checkpoints"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class EventRow(Base):
    """Durable copy of the Redis stream."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = _created_at()
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_run_id_kind_seq", "run_id", "kind", "seq"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str | None] = mapped_column(Text)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class RepoSymbolRow(Base):
    """One definition in one file, at one repository SHA.

    Keyed by SHA rather than by run, because the index describes a commit and not a run:
    two runs against the same base share it, and re-indexing the same SHA is a no-op. That
    is the whole reason indexing a 3000-file repository can be affordable.

    `refs` holds the identifiers used *inside* this definition, which is what makes the
    table answer "who calls paginate" rather than only "where is paginate".
    """

    __tablename__ = "repo_symbols"
    __table_args__ = (
        Index("ix_repo_symbols_sha", "repo_sha"),
        Index("ix_repo_symbols_sha_name", "repo_sha", "name"),
        Index("ix_repo_symbols_sha_path", "repo_sha", "path"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_sha: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    refs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _created_at()


class RepoEmbeddingRow(Base):
    """One embedded chunk of one file, at one repository SHA.

    Keyed by SHA for the same reason `repo_symbols` is: the embedding describes a commit,
    so two runs against the same base share it and re-indexing is free.

    `text` is stored beside the vector because a search result has to show something a
    reader recognises, and re-reading the file to reconstruct the chunk would mean the
    index stops working the moment the worktree is gone.
    """

    __tablename__ = "repo_embeddings"
    # The HNSW index is declared here and not only in the migration: `compare_metadata`
    # reads the models, so an index the migration creates in raw SQL reads as drift and
    # the next autogenerate proposes dropping it. Caught by
    # `test_handwritten_migration_matches_models`.
    __table_args__ = (
        Index("ix_repo_embeddings_sha", "repo_sha"),
        Index(
            "ix_repo_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_sha: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_id: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


CORE_TABLES: tuple[str, ...] = (
    "runs",
    "tasks",
    "steps",
    "tool_calls",
    "llm_calls",
    "checkpoints",
    "events",
    "artifacts",
    "repo_symbols",
    "repo_embeddings",
)
