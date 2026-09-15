"""任务执行编排器：CLI 与 arq worker 共用的执行入口。

职责：置 running → 跑主图（plan → execute → synthesize）→ 回写终态。
图执行异常时置 failed 并向上抛出（调用方决定是否重试/上报）。
"""

import logging

from app.db import ResearchTask, TaskStatus
from app.db.base import SessionLocal
from app.engine.checkpoint import open_graph_checkpointer, thread_id_for_task
from app.engine.main_graph import DEFAULT_MAX_PAGES, OrchestratorDeps, build_main_graph
from app.engine.persister import SubTaskPersister
from app.engine.planner import write_plan
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

        persister = SubTaskPersister(session)
        await persister.mark_task_running(task_id, thread_id_for_task(task_id))

        question, background, depth = task.question, task.background, task.depth
        recorder = PrintingRecorder(session) if verbose else EventRecorder(session)
        deps = OrchestratorDeps(
            write_plan=write_plan,
            run_worker=make_sub_task_runner(verbose=verbose),
            write_report=write_report,
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
                    },
                    config={"configurable": {"thread_id": thread_id_for_task(task_id)}},
                )
            except Exception as e:
                await persister.finish_task(task_id, done=False, error=f"{type(e).__name__}: {e}")
                raise

        has_report = bool(final.get("report_id"))
        if not has_report:
            await persister.finish_task(task_id, done=False, error="no notes or report produced")
        else:
            await persister.finish_task(task_id, done=True)

        await session.refresh(task)
        return {
            "task_id": task_id,
            "status": task.status.value,
            "executed": final.get("executed", 0),
            "notes": len(final.get("notes") or []),
            "sources": len(final.get("sources") or []),
            "worker_errors": len(final.get("worker_errors") or []),
            "report_id": final.get("report_id"),
            "report_chars": final.get("report_chars", 0),
            "citations": final.get("citations", 0),
            "token_used": task.token_used,
            "cost_cny": task.cost_cny,
        }
