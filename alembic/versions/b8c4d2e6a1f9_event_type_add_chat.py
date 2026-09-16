"""event_type enum add 'chat' value

Revision ID: b8c4d2e6a1f9
Revises: f3d2b8a1c6e4
Create Date: 2026-09-16 23:10:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c4d2e6a1f9"
down_revision: str | Sequence[str] | None = "f3d2b8a1c6e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """追问计费事件：与 plan/note/reflect/synthesize 同为成本看板对账数据源。"""
    op.execute("ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'chat'")


def downgrade() -> None:
    """Postgres 枚举不支持移除值，chat 值保留无害（代码层已不再写入）。"""
