"""Embedded chunks for semantic search, with an HNSW index.

Separate from 0003 because that one shipped: adding a table to an applied migration is a
migration nobody's database runs.

HNSW rather than IVFFlat: it needs no training pass over existing rows, which matters when
the table is written once per commit and queried immediately afterwards. `m=16` and
`ef_construction=64` are the phase document's, and pgvector's own defaults.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

DIMENSIONS = 1024


def upgrade() -> None:
    # The extension is created in 0001; this is belt and braces for a database that had
    # the table dropped and rebuilt.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "repo_embeddings",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("repo_sha", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding", Vector(DIMENSIONS), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_repo_embeddings_sha", "repo_embeddings", ["repo_sha"])
    # Through `create_index` rather than raw SQL so this is the same declaration the model
    # carries — raw SQL here and nothing in the model is exactly the drift
    # `test_handwritten_migration_matches_models` exists to catch.
    op.create_index(
        "ix_repo_embeddings_hnsw",
        "repo_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_repo_embeddings_hnsw", table_name="repo_embeddings")
    op.drop_index("ix_repo_embeddings_sha", table_name="repo_embeddings")
    op.drop_table("repo_embeddings")
