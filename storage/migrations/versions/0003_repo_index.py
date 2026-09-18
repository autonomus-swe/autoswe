"""The symbol index: one row per definition, keyed by repository SHA.

Keyed by SHA and not by run, because the index describes a commit. Two runs against the
same base share it and re-indexing the same SHA is a no-op, which is what makes indexing a
three-thousand-file repository affordable at all.

`repo_embeddings` is named alongside this in the phase document but arrives with Step 5.3:
it needs the `vector` extension, and a migration that creates a table nothing reads yet —
requiring an extension nobody has verified is present — is a worse thing to ship than a
second migration.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repo_symbols",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("repo_sha", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column(
            "refs", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # By SHA for the "already indexed?" check, by (sha, name) for "where is paginate", and
    # by (sha, path) for the per-file read `list_symbols` does.
    op.create_index("ix_repo_symbols_sha", "repo_symbols", ["repo_sha"])
    op.create_index("ix_repo_symbols_sha_name", "repo_symbols", ["repo_sha", "name"])
    op.create_index("ix_repo_symbols_sha_path", "repo_symbols", ["repo_sha", "path"])


def downgrade() -> None:
    op.drop_index("ix_repo_symbols_sha_path", table_name="repo_symbols")
    op.drop_index("ix_repo_symbols_sha_name", table_name="repo_symbols")
    op.drop_index("ix_repo_symbols_sha", table_name="repo_symbols")
    op.drop_table("repo_symbols")
