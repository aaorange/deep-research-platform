"""`uv run python -m app.cli` 研究入口。

- 默认：单子问题跑一次「搜读记」子图（W1 冒烟入口，保留作回归工具）
- --depth quick|std|deep：跑主图（plan → execute 并行 → synthesize），
  产出大纲、多份笔记与带引用的报告
"""

import argparse
import asyncio
import sys
import time

from app.db import ResearchTask, TaskStatus
from app.db.base import SessionLocal
from app.engine.note_writer import write_note
from app.engine.orchestrator import execute_research
from app.engine.persister import SubTaskPersister
from app.engine.planner import DEPTH_BUDGETS, DEPTH_SUBTASK_COUNT
from app.engine.subgraph import WorkerDeps, build_worker_graph
from app.services.printing_recorder import PrintingRecorder
from app.tools.read_page import read_page
from app.tools.web_search import web_search


async def run_single(question: str, max_pages: int) -> int:
    start = time.perf_counter()

    async with SessionLocal() as session:
        task = ResearchTask(question=question, status=TaskStatus.running, token_budget=80000)
        session.add(task)
        await session.commit()
        task_id = task.id

        print(f"▶ task_id={task_id}  问题：{question}")
        print("─" * 60)

        deps = WorkerDeps(
            search=web_search,
            read_page=read_page,
            write_note=write_note,
            recorder=PrintingRecorder(session),
            persister=SubTaskPersister(session),
        )
        graph = build_worker_graph(deps)
        final = await graph.ainvoke(
            {
                "task_id": task_id,
                "sub_task_id": None,
                "title": question,
                "max_pages": max_pages,
            }
        )

        await session.refresh(task)

    print("─" * 60)
    if final.get("error"):
        print(f"✗ 失败：{final['error']}")
        return 1

    note = final["note"]
    print(f"✓ 笔记（{len(note['summary'])} 字，{len(note['facts'])} 条事实）：")
    print()
    print(note["summary"])
    print()
    if note.get("gaps"):
        print("待补缺口：")
        for g in note["gaps"]:
            print(f"  - {g}")
    elapsed = time.perf_counter() - start
    print("─" * 60)
    print(
        f"✓ 完成：耗时 {elapsed:.1f}s | tokens {final['note_tokens']} "
        f"| 成本 ¥{task.cost_cny:.4f} | 事件可查："
        f"SELECT * FROM agent_events WHERE task_id={task_id} ORDER BY seq;"
    )
    return 0


async def run_full(question: str, background: str, depth: str, max_pages: int) -> int:
    start = time.perf_counter()

    async with SessionLocal() as session:
        task = ResearchTask(
            question=question,
            background=background or None,
            depth=depth,
            status=TaskStatus.queued,
            token_budget=DEPTH_BUDGETS[depth],
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    print(f"▶ task_id={task_id}  问题：{question}")
    print(
        f"  深度 {depth}（{DEPTH_SUBTASK_COUNT[depth]} 个子任务，"
        f"预算 {DEPTH_BUDGETS[depth]} tokens）"
    )
    print("─" * 60)

    try:
        final = await execute_research(task_id, verbose=True, max_pages=max_pages)
    except Exception as e:  # noqa: BLE001
        print(f"✗ 执行失败：{e}")
        return 1

    print("─" * 60)
    print(
        f"✓ 状态 {final['status']} | {final['notes']} 份笔记 / {final['sources']} 条信源"
        f" / {final['worker_errors']} 个失败子任务"
    )
    if final.get("supplemented"):
        print(
            f"✓ 反思 {final['reflect_rounds']} 轮：补充 {final['supplemented']} 个子任务"
            "（缺口检测 + 追加指示）"
        )
    if final.get("budget_degraded"):
        print("⚠ 预算降级：已跳过剩余检索与补充轮，报告尾部含预算受限说明")
    if final["report_id"]:
        print(
            f"✓ 报告 #{final['report_id']}：{final['report_chars']} 字 / "
            f"{final['citations']} 个引用编号 | tokens {final['token_used']} "
            f"| 成本 ¥{final['cost_cny']:.4f}"
        )
    else:
        print("✗ 未生成报告（无笔记产出）")
    elapsed = time.perf_counter() - start
    print("─" * 60)
    print(f"✓ 完成：耗时 {elapsed:.1f}s | 报告可查：SELECT * FROM reports WHERE task_id={task_id};")
    return 0 if final["report_id"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="research", description="Deep Research CLI")
    parser.add_argument("question", help="研究问题")
    parser.add_argument("--background", "-b", default=None, help="研究背景（可选）")
    parser.add_argument(
        "--depth",
        choices=["quick", "std", "deep"],
        default=None,
        help="指定后跑主图（plan→execute→synthesize）；缺省跑单链路冒烟",
    )
    parser.add_argument("--max-pages", type=int, default=3, help="每个子任务最多读取页面数")
    args = parser.parse_args()

    if args.depth:
        sys.exit(asyncio.run(run_full(args.question, args.background, args.depth, args.max_pages)))
    sys.exit(asyncio.run(run_single(args.question, args.max_pages)))
