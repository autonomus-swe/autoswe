"""Cross-fork pull requests: where the PR is opened, when that is not where we pushed.

The fork workflow this project insists on for its own development is the one it could not
offer its users: a run cloned `repo_url`, pushed a branch there, and opened the pull
request there. That works when you own the repository and not otherwise — and "not
otherwise" is the interesting case, because an agent contributing to a repository it does
not own is the whole point of the pattern.

Nullable with no default, which is the honest shape: almost every run pushes and opens in
the same place, and a column defaulted to something would claim otherwise.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("upstream", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "upstream")
