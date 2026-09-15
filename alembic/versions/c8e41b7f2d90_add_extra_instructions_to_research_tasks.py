"""add extra_instructions to research_tasks

Revision ID: c8e41b7f2d90
Revises: b7f3a92c1e4d
Create Date: 2026-09-16 10:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e41b7f2d90"
down_revision: str | Sequence[str] | None = "b7f3a92c1e4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "research_tasks",
        sa.Column(
            "extra_instructions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
            comment="追加指示信箱：[{id, text, created_at, consumed_round}]，reflect 节点消费",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("research_tasks", "extra_instructions")
