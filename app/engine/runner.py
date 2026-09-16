"""生产装配：并行 worker 运行器（独立 session）+ 真实工具链。

每个子任务一个 session 跑 W1「搜读记」子图——AsyncSession 禁止并发写，
并行必须各开各的 session；事件 seq 由 research_tasks 行锁原子分配，跨
session 并发安全。子任务状态（running→done/failed）随执行回写。
"""

from app.db import SubTaskStatus, TaskStatus
from app.db.base import SessionLocal
from app.engine.main_graph import JobSupersededError
from app.engine.note_writer import write_note
from app.engine.persister import SubTaskPersister
from app.engine.subgraph import WorkerDeps, build_worker_graph
from app.services.event_recorder import EventRecorder
from app.services.printing_recorder import PrintingRecorder
from app.tools.read_page import read_page
from app.tools.web_search import web_search


def make_sub_task_runner(verbose: bool = False, run_token: str | None = None):
    """返回 WorkerRunner；verbose=True 时 worker 事件同步打印（CLI 模式）。

    run_token：job 所有权令牌；子任务内每步（搜索/读页/笔记前）复查
    (status, token)，用户终止/暂停后最多一个工具调用的延迟即退出，
    而非等整个飞行批次跑完。
    """

    async def run_worker(task_id: int, sub_task: dict, max_pages: int) -> dict:
        async with SessionLocal() as session:
            persister = SubTaskPersister(session)

            async def fence() -> None:
                status, current = await persister.task_run_state(task_id)
                if status != TaskStatus.running or current != run_token:
                    reason = (
                        f"task {getattr(status, 'value', status)}"
                        if status != TaskStatus.running
                        else "superseded"
                    )
                    raise JobSupersededError(reason)

            await persister.mark_sub_task_status(sub_task["id"], SubTaskStatus.running)

            recorder = PrintingRecorder(session) if verbose else EventRecorder(session)
            deps = WorkerDeps(
                search=web_search,
                read_page=read_page,
                write_note=write_note,
                recorder=recorder,
                persister=persister,
                fence=fence if run_token else None,
            )
            graph = build_worker_graph(deps)
            final = await graph.ainvoke(
                {
                    "task_id": task_id,
                    "sub_task_id": sub_task["id"],
                    "title": sub_task["title"],
                    "keywords": sub_task.get("keywords"),
                    "max_pages": max_pages,
                }
            )

            error = final.get("error")
            await persister.mark_sub_task_status(
                sub_task["id"],
                SubTaskStatus.failed if error else SubTaskStatus.done,
                error,
            )
            return {
                "sub_task_id": sub_task["id"],
                "title": sub_task["title"],
                "note": final.get("note"),
                "sources": final.get("sources", []),
                "note_tokens": final.get("note_tokens", 0),
                "note_db_id": final.get("note_db_id"),
                "error": error,
            }

    return run_worker
