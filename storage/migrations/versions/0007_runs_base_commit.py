"""Start a run from a commit rather than from the head of a branch.

SWE-bench Lite pins a `base_commit` per instance, and a patch produced against the head of
a branch will not apply to it. Without this the benchmark could be *run* and never
*scored* — `evals/swebench.py` refused to produce predictions rather than emit a number
whose low value nobody could attribute to the right cause.

Distinct from `base_sha`, which is the commit a run *resolved* during SETUP. This column
is the commit the caller *asked for*: null in the ordinary case, where the request was a
branch and the answer is whatever that branch pointed at when the run started. Keeping the
request and the result apart is what makes "the branch moved under us" a visible fact
rather than a rewritten one.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("base_commit", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "base_commit")
