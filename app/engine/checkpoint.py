"""LangGraph Postgres 检查点：为断点续跑提供图状态持久化。

主图（W2）以 thread_id = f"task-{task_id}" 建检查点，服务重启后
从最近的 checkpoint 恢复，已完成节点不重跑。
"""

import sys
from contextlib import asynccontextmanager, nullcontext

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import get_settings


def thread_id_for_task(task_id: int) -> str:
    return f"task-{task_id}"


def _psycopg_url(asyncpg_url: str) -> str:
    # asyncpg: postgresql+asyncpg://user:pass@host:5432/db
    # psycopg:  postgresql://user:pass@host:5432/db
    return asyncpg_url.replace("+asyncpg", "")


@asynccontextmanager
async def open_checkpointer():
    """async with 用检查点：进入时 setup 建表（幂等，CREATE TABLE IF NOT EXISTS）。

    注意：from_conn_string 本身不建表，必须显式 setup()——全新数据库
    （如部署环境首跑）缺少 checkpoints 表会直接 UndefinedTable。
    """
    async with AsyncPostgresSaver.from_conn_string(
        _psycopg_url(get_settings().database_url)
    ) as saver:
        await saver.setup()
        yield saver


def open_graph_checkpointer():
    """CLI/主图用检查点。

    Windows 开发机降级为内存检查点：psycopg 需要 SelectorEventLoop，而
    crawl4ai/playwright 需要 Proactor（subprocess），二者在 Windows 互斥；
    Postgres 检查点只在 Linux（部署环境）启用。
    """
    if sys.platform == "win32":
        return nullcontext(InMemorySaver())
    return open_checkpointer()
