"""An index for the collector's sweep over finished runs.

The garbage collector asks "which runs finished before this?" every ten minutes, forever.
On a development database that is a sequential scan of nothing; on a deployment with a
year of runs it is a sequential scan of a year of runs, six times an hour.

Declared on the model as well as here. An index created only in a migration reads as drift
to `compare_metadata`, and the next autogenerate proposes dropping it — which leaves a
sequential scan that is correct and slow, the kind of regression no test notices.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_runs_status_finished_at", "runs", ["status", "finished_at"])


def downgrade() -> None:
    op.drop_index("ix_runs_status_finished_at", table_name="runs")
