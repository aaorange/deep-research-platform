"""add run_token to research_tasks and unique report per task

Revision ID: d4a7e9f1b2c3
Revises: c8e41b7f2d90
Create Date: 2026-09-16 11:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a7e9f1b2c3"
down_revision: str | Sequence[str] | None = "c8e41b7f2d90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 历史并发 bug 可能产生同任务重复报告：每任务仅保留最新一份，约束才能建上
    op.execute(
        """
        DELETE FROM reports r
        USING reports r2
        WHERE r.task_id = r2.task_id AND r.id < r2.id
        """
    )
    op.add_column(
        "research_tasks",
        sa.Column(
            "run_token",
            sa.String(36),
            nullable=True,
            comment="job 所有权令牌：resume 重入后旧 job 凭此退出，防并发重复执行",
        ),
    )
    op.create_unique_constraint("uq_reports_task_id", "reports", ["task_id"])
    # 唯一约束自带索引，原普通索引冗余（模型已不再声明 index=True）
    op.drop_index("ix_reports_task_id", table_name="reports")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index("ix_reports_task_id", "reports", ["task_id"])
    op.drop_constraint("uq_reports_task_id", "reports", type_="unique")
    op.drop_column("research_tasks", "run_token")
