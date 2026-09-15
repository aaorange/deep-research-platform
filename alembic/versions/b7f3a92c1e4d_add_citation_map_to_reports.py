"""add citation_map to reports

Revision ID: b7f3a92c1e4d
Revises: e19281cc56f4
Create Date: 2026-09-16 09:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7f3a92c1e4d"
down_revision: str | Sequence[str] | None = "e19281cc56f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "reports",
        sa.Column(
            "citation_map",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="展示编号 → 信源 id，报告阅读页渲染信源卡",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("reports", "citation_map")
