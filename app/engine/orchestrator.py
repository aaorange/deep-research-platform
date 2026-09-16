"""任务执行编排器：CLI 与 arq worker 共用的执行入口。

职责：置 running → 跑主图（plan → execute → reflect → synthesize）→ 回写终态。
图执行异常时置 failed 并向上抛出（调用方决定是否重试/上报）。
"""

import logging
import uuid

from app.db import ResearchTask, TaskStatus
from app.db.base import SessionLocal
from app.engine.budget import is_degraded
from app.engine.checkpoint import open_graph_checkpointer, thread_id_for_task
from app.engine.main_graph import (
    DEFAULT_MAX_PAGES,
    JobSupersededError,
    OrchestratorDeps,
    build_main_graph,
)
from app.engine.persister import SubTaskPersister
from app.engine.planner import write_plan
from app.engine.reflector import reflect_on_coverage
from app.engine.runner import make_sub_task_runner
from app.engine.synthesizer import write_report
from app.services.event_recorder import EventRecorder
from app.services.printing_recorder import PrintingRecorder

logger = logging.getLogger(__name__)


async def execute_research(
    task_id: int,
    verbose: bool = False,
    max_pages: int = DEFAULT_MAX_PAGES,
    session_maker=SessionLocal,
) -> dict:
    """执行一个研究任务并回写终态，返回摘要 dict。

    session_maker 仅供测试注入测试库；生产路径默认 SessionLocal。

    Raises:
        ValueError: 任务不存在或状态不允许执行（done/canceled/stopped）。
    """
    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        if task is None:
            raise ValueError(f"task {task_id} not found")
        if task.status in (TaskStatus.done, TaskStatus.canceled, TaskStatus.stopped):
            raise ValueError(f"task {task_id} in terminal status {task.status.value}")
        if task.status == TaskStatus.paused:
            # 排队真空期被用户暂停的 job：worker 取到后直接退出，不置 running。
            # 状态归控制方所有，不回写终态。
            logger.info("task %s paused before job start, exit", task_id)
            return {"task_id": task_id, "status": "paused"}

        persister = SubTaskPersister(session)
        run_token = uuid.uuid4().hex
        # 原子认领：入口读状态与 UPDATE 之间被并发暂停时，条件更新不命中，本 job 退出
        claimed = await persister.mark_task_running(
            task_id, thread_id_for_task(task_id), run_token=run_token
        )
        if not claimed:
            logger.info("task %s lost claim (paused/terminal), exit", task_id)
            return {"task_id": task_id, "status": "paused"}

        question, background, depth = task.question, task.background, task.depth
        recorder = PrintingRecorder(session) if verbose else EventRecorder(session)
        deps = OrchestratorDeps(
            write_plan=write_plan,
            run_worker=make_sub_task_runner(verbose=verbose, run_token=run_token),
            write_report=write_report,
            reflect=reflect_on_coverage,
            recorder=recorder,
            store=persister,
        )

        async with open_graph_checkpointer() as checkpointer:
            graph = build_main_graph(deps, checkpointer=checkpointer)
            try:
                final = await graph.ainvoke(
                    {
                        "task_id": task_id,
                        "question": question,
                        "background": background,
                        "depth": depth,
                        "max_pages": max_pages,
                        "run_token": run_token,
                    },
                    config={"configurable": {"thread_id": thread_id_for_task(task_id)}},
                )
            except JobSupersededError:
                # 本 job 已被暂停/终止或被 resume 的新 job 抢占：
                # 任务状态归控制方/新 job 所有，不回写终态、安静退出
                logger.info("task %s superseded (token %s), job exits", task_id, run_token[:8])
                return {"task_id": task_id, "status": "superseded"}
            except Exception as e:
                await persister.finish_task(
                    task_id, done=False, error=f"{type(e).__name__}: {e}", run_token=run_token
                )
                raise

        has_report = bool(final.get("report_id"))
        if not has_report:
            await persister.finish_task(
                task_id, done=False, error="no notes or report produced", run_token=run_token
            )
        else:
            await persister.finish_task(task_id, done=True, run_token=run_token)

        # 补充轮后 state 只含末轮产物，笔记/信源从 DB 取全量
        all_notes, all_sources = await persister.synthesis_inputs(task_id)

        await session.refresh(task)
        return {
            "task_id": task_id,
            "status": task.status.value,
            "executed": final.get("executed", 0),
            "notes": len(all_notes),
            "sources": len(all_sources),
            "worker_errors": len(final.get("worker_errors") or []),
            "reflect_rounds": final.get("reflect_rounds", 0),
            "supplemented": final.get("supplemented", 0),
            "report_id": final.get("report_id"),
            "report_chars": final.get("report_chars", 0),
            "citations": final.get("citations", 0),
            "budget_degraded": is_degraded(task.token_used, task.token_budget),
            "token_used": task.token_used,
            "cost_cny": task.cost_cny,
        }
