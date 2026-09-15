"""baseline

Revision ID: 062ea634353c
Revises:
Create Date: 2026-09-15 22:07:02.663004

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "062ea634353c"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
