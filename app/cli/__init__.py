"""`uv run research "子问题"` 单链路 CLI 冒烟入口。

跑一次完整的「搜读记」子图：实时打印动作流（搜索词、抓取 URL、耗时、降级），
结束后打印压缩笔记与 token 成本。全过程事件落库，psql 可查。
"""

import argparse
import asyncio
import sys
import time

from app.db import EventType, ResearchTask, TaskStatus
from app.db.base import SessionLocal
from app.engine.note_writer import write_note
from app.engine.persister import SubTaskPersister
from app.engine.subgraph import WorkerDeps, build_worker_graph
from app.services.event_recorder import EventRecorder
from app.tools.read_page import read_page
from app.tools.web_search import web_search


def _fmt_ms(ms: int | None) -> str:
    return f"{ms}ms" if ms is not None else "-"


class PrintingRecorder(EventRecorder):
    """事件落库的同时打印动作流。"""

    ICONS = {
        EventType.search: "🔎",
        EventType.fetch: "📄",
        EventType.degrade: "⚠️ ",
        EventType.note: "📝",
        EventType.control: "⛔",
    }

    async def record(self, task_id, type, payload=None, **kwargs):
        event = await super().record(task_id, type, payload, **kwargs)
        icon = self.ICONS.get(type, "·")
        line = self._describe(type, payload or {})
        if kwargs.get("latency_ms"):
            line += f"（{_fmt_ms(kwargs['latency_ms'])}）"
        print(f"{icon} {line}")
        return event

    @staticmethod
    def _describe(type: EventType, payload: dict) -> str:
        if type == EventType.search:
            return (
                f"搜索「{payload.get('query')}」via {payload.get('provider')}，"
                f"{payload.get('hits')} 条结果"
            )
        if type == EventType.fetch:
            return (
                f"读取 [{payload.get('domain')}] {str(payload.get('title'))[:40]}"
                f"（信誉 {payload.get('credibility')}/5）"
            )
        if type == EventType.degrade:
            to = payload.get("to") or "放弃"
            return f"读页降级 {payload.get('from')}→{to}：{str(payload.get('error'))[:50]}"
        if type == EventType.note:
            gaps = payload.get("gaps") or []
            return (
                f"笔记生成：{payload.get('summary_chars')} 字 / "
                f"{payload.get('facts')} 条事实 / {len(gaps)} 条缺口"
            )
        if type == EventType.control:
            return f"控制事件：{payload.get('stage')} {payload.get('error')}"
        return str(payload)[:60]


async def run(question: str, max_pages: int) -> int:
    start = time.perf_counter()

    async with SessionLocal() as session:
        task = ResearchTask(question=question, status=TaskStatus.running, token_budget=80000)
        session.add(task)
        await session.commit()
        task_id = task.id

        print(f"▶ task_id={task_id}  问题：{question}")
        print("─" * 60)

        recorder = PrintingRecorder(session)
        deps = WorkerDeps(
            search=web_search,
            read_page=read_page,
            write_note=write_note,
            recorder=recorder,
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="research", description="单子问题研究 CLI 冒烟")
    parser.add_argument("question", help="研究子问题")
    parser.add_argument("--max-pages", type=int, default=3, help="最多读取页面数")
    args = parser.parse_args()

    sys.exit(asyncio.run(run(args.question, args.max_pages)))
