"""Add a monotonic seq to tool_calls and llm_calls so a run replays in order.

Rows written inside one transaction share ``created_at``, which makes the audit trail
ambiguous. ``artifacts`` already carries such a column; this gives the two call tables the
same guarantee.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = ("tool_calls", "llm_calls")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        )
        op.create_unique_constraint(f"uq_{table}_seq", table, ["seq"])
        op.create_index(f"ix_{table}_step_id_seq", table, ["step_id", "seq"])


def downgrade() -> None:
    for table in TABLES:
        op.drop_index(f"ix_{table}_step_id_seq", table_name=table)
        op.drop_constraint(f"uq_{table}_seq", table, type_="unique")
        op.drop_column(table, "seq")
