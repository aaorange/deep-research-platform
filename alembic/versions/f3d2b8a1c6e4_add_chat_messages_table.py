"""add chat_messages table

Revision ID: f3d2b8a1c6e4
Revises: d4a7e9f1b2c3
Create Date: 2026-09-16 15:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3d2b8a1c6e4"
down_revision: str | Sequence[str] | None = "d4a7e9f1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Enum("user", "assistant", name="chat_role"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "cited_source_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=True,
            comment="assistant 回答引用的信源库 id 列表（[N] 锚点提取）",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"]),
    )
    op.create_index("ix_chat_messages_task_id", "chat_messages", ["task_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_chat_messages_task_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    sa.Enum(name="chat_role").drop(op.get_bind(), checkfirst=True)
