"""子图结果落库：sources、notes、task token 累计与成本折算。

DeepSeek 官方定价（2025，缓存未命中）：
  deepseek-chat      输入 ¥2/M tokens   输出 ¥8/M tokens
  deepseek-reasoner  输入 ¥4/M tokens   输出 ¥16/M tokens
"""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Note, ResearchTask, Source, SubTask, SubTaskStatus

PRICE_PER_M = {
    "deepseek-chat": (2.0, 8.0),
    "deepseek-reasoner": (4.0, 16.0),
}


def cost_cny(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    in_price, out_price = PRICE_PER_M.get(model, (2.0, 8.0))
    return round(
        prompt_tokens / 1_000_000 * in_price + completion_tokens / 1_000_000 * out_price,
        6,
    )


class SubTaskPersister:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_sub_tasks(self, task_id: int, sub_tasks: list[dict]) -> list[dict]:
        """大纲落库 sub_tasks 表，返回含 id 的行（保持传入顺序）。"""
        rows = [
            SubTask(task_id=task_id, title=s["title"], keywords=s.get("keywords"))
            for s in sub_tasks
        ]
        self.session.add_all(rows)
        await self.session.commit()
        return [{"id": r.id, "title": r.title, "keywords": r.keywords} for r in rows]

    async def pending_sub_tasks(self, task_id: int) -> list[dict]:
        """待执行子任务：pending + 崩溃残留的 running（resume 时重跑）。"""
        result = await self.session.execute(
            select(SubTask)
            .where(
                SubTask.task_id == task_id,
                SubTask.status.in_([SubTaskStatus.pending, SubTaskStatus.running]),
            )
            .order_by(SubTask.id)
        )
        return [
            {"id": r.id, "title": r.title, "keywords": r.keywords} for r in result.scalars().all()
        ]

    async def mark_sub_task_status(
        self, sub_task_id: int, status: SubTaskStatus, error: str | None = None
    ) -> None:
        await self.session.execute(
            update(SubTask).where(SubTask.id == sub_task_id).values(status=status, error_msg=error)
        )
        await self.session.commit()

    async def persist_sources(self, task_id: int, sources: list[dict]) -> dict[int, int]:
        """写入 sources 表，返回 {subgraph_idx: db_id}。"""
        idx_to_id: dict[int, int] = {}
        for s in sources:
            row = Source(
                task_id=task_id,
                url=s["url"],
                title=s.get("title"),
                domain=s.get("domain"),
                credibility=s.get("credibility"),
                freshness=s.get("freshness"),
                content_hash=s.get("content_hash"),
            )
            self.session.add(row)
            await self.session.flush()
            idx_to_id[s["idx"]] = row.id
        await self.session.commit()
        return idx_to_id

    async def persist_note(
        self,
        task_id: int,
        sub_task_id: int | None,
        note: dict,
        idx_to_id: dict[int, int],
    ) -> int:
        """笔记正文中的 [N] 锚点替换为 [source_db_id]，落 notes 表。"""
        import re

        text = note["summary"]
        for m in set(re.findall(r"\[(\d+)\]", text)):
            idx = int(m)
            if idx in idx_to_id:
                text = text.replace(f"[{m}]", f"[{idx_to_id[idx]}]")
        row = Note(task_id=task_id, sub_task_id=sub_task_id, content=text)
        self.session.add(row)
        await self.session.commit()
        return row.id

    async def add_usage(
        self, task_id: int, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        cost = cost_cny(model, prompt_tokens, completion_tokens)
        await self.session.execute(
            update(ResearchTask)
            .where(ResearchTask.id == task_id)
            .values(
                token_used=ResearchTask.token_used + prompt_tokens + completion_tokens,
                cost_cny=ResearchTask.cost_cny + cost,
            )
        )
        await self.session.commit()

    async def sources_for_task(self, task_id: int) -> list[Source]:
        result = await self.session.execute(
            select(Source).where(Source.task_id == task_id).order_by(Source.id)
        )
        return list(result.scalars().all())
