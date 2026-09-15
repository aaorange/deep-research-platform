"""arq 队列连接：API 侧入队用。

连接池由 FastAPI lifespan 管理生命周期，get_queue 作为依赖注入
（测试用 dependency_overrides 换假实现，不连 Redis）。
"""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Request

from app.config import get_settings
from app.worker import JOB_NAME


async def create_arq_pool() -> ArqRedis:
    return await create_pool(RedisSettings.from_dsn(get_settings().redis_url))


async def get_queue(request: Request) -> ArqRedis:
    return request.app.state.arq


async def enqueue_research(pool: ArqRedis, task_id: int) -> None:
    await pool.enqueue_job(JOB_NAME, task_id)
