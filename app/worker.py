"""arq worker：从 Redis 队列取研究任务执行主图并回写状态。

启动：uv run arq app.worker.WorkerSettings
任务由 API 侧 POST /research/tasks 入队（_job_name 固定为 run_research_task）。
"""

import logging

from arq.connections import RedisSettings

from app.config import get_settings
from app.engine.orchestrator import execute_research

logger = logging.getLogger(__name__)

JOB_NAME = "run_research_task"


async def run_research_task(ctx: dict, task_id: int) -> dict:
    """执行研究任务；execute_research 内部已处理状态回写与异常落库。"""
    try:
        return await execute_research(task_id)
    except Exception:
        logger.exception("research task %s failed", task_id)
        return {"task_id": task_id, "ok": False}


class WorkerSettings:
    functions = [run_research_task]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
