"""子图结果落库：sources、notes、reports、task 状态与 token 累计成本折算。

DeepSeek 官方定价（2025，缓存未命中）：
  deepseek-chat      输入 ¥2/M tokens   输出 ¥8/M tokens
  deepseek-reasoner  输入 ¥4/M tokens   输出 ¥16/M tokens
"""

from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    Note,
    Report,
    ResearchTask,
    Source,
    SubTask,
    SubTaskStatus,
    TaskStatus,
)

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

    async def create_sub_tasks(
        self, task_id: int, sub_tasks: list[dict], round_no: int = 1
    ) -> list[dict]:
        """大纲落库 sub_tasks 表（round_no=1 首轮，≥2 反思补充轮）。"""
        rows = [
            SubTask(
                task_id=task_id, title=s["title"], keywords=s.get("keywords"), round_no=round_no
            )
            for s in sub_tasks
        ]
        self.session.add_all(rows)
        await self.session.commit()
        return [{"id": r.id, "title": r.title, "keywords": r.keywords} for r in rows]

    async def all_sub_tasks(self, task_id: int) -> list[dict]:
        """全部子任务（含状态与轮次），reflect 检查清单用。"""
        result = await self.session.execute(
            select(SubTask).where(SubTask.task_id == task_id).order_by(SubTask.id)
        )
        return [
            {
                "id": r.id,
                "title": r.title,
                "keywords": r.keywords,
                "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                "round_no": r.round_no,
            }
            for r in result.scalars().all()
        ]

    async def pending_sub_tasks(self, task_id: int) -> list[dict]:
        """待执行子任务：pending + 崩溃残留的 running（resume 时重跑）。

        自愈：running 且已落笔记（崩溃窗口内笔记已持久化但状态未回写）→
        置 done，resume 重入时不会重跑该子任务、不产生重复笔记。
        """
        await self.session.execute(
            text("""
                UPDATE sub_tasks SET status = 'done'
                WHERE task_id = :tid AND status = 'running'
                  AND EXISTS (SELECT 1 FROM notes WHERE notes.sub_task_id = sub_tasks.id)
            """),
            {"tid": task_id},
        )
        await self.session.commit()
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

    async def skip_pending_sub_tasks(self, task_id: int) -> int:
        """预算降级：pending/running 全部置 skipped，返回跳过数。"""
        result = await self.session.execute(
            update(SubTask)
            .where(
                SubTask.task_id == task_id,
                SubTask.status.in_([SubTaskStatus.pending, SubTaskStatus.running]),
            )
            .values(status=SubTaskStatus.skipped, error_msg="budget degraded")
        )
        await self.session.commit()
        return result.rowcount or 0

    async def task_budget_state(self, task_id: int) -> tuple[int, int]:
        """新鲜读 (token_used, token_budget)：列级 SELECT 不走 identity map。

        add_usage 是 raw UPDATE 绕过 ORM，session.get 会命中陈旧值；
        预算判定每次都要读到最新累计。
        """
        row = (
            await self.session.execute(
                select(ResearchTask.token_used, ResearchTask.token_budget).where(
                    ResearchTask.id == task_id
                )
            )
        ).first()
        return (row.token_used, row.token_budget) if row else (0, 0)

    async def task_run_state(self, task_id: int) -> tuple[TaskStatus | None, str | None]:
        """新鲜读 (status, run_token)：job 围栏检查用，绕过 identity map。"""
        row = (
            await self.session.execute(
                select(ResearchTask.status, ResearchTask.run_token).where(
                    ResearchTask.id == task_id
                )
            )
        ).first()
        return (row.status, row.run_token) if row else (None, None)

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

    async def synthesis_inputs(self, task_id: int) -> tuple[list[dict], list[dict]]:
        """报告综合输入：全部笔记（带子任务标题）+ 全部信源，均按落库顺序。

        笔记 content 的 [N] 锚点已是信源库 id（persist_note 重写过）。
        """
        note_rows = (
            await self.session.execute(
                select(Note, SubTask.title)
                .outerjoin(SubTask, Note.sub_task_id == SubTask.id)
                .where(Note.task_id == task_id)
                .order_by(Note.id)
            )
        ).all()
        notes = [
            {
                "sub_task_id": n.sub_task_id,
                "title": title or "未归类笔记",
                "content": n.content,
            }
            for n, title in note_rows
        ]
        src_rows = (
            (
                await self.session.execute(
                    select(Source).where(Source.task_id == task_id).order_by(Source.id)
                )
            )
            .scalars()
            .all()
        )
        sources = [
            {
                "id": s.id,
                "url": s.url,
                "title": s.title,
                "domain": s.domain,
                "credibility": s.credibility,
            }
            for s in src_rows
        ]
        return notes, sources

    async def persist_report(
        self, task_id: int, markdown: str, citation_map: dict[int, int], token_total: int
    ) -> int:
        row = Report(
            task_id=task_id,
            version=1,
            markdown=markdown,
            citation_map={str(n): sid for n, sid in citation_map.items()},
            token_total=token_total,
        )
        self.session.add(row)
        try:
            await self.session.commit()
            return row.id
        except IntegrityError:
            # 并发窗口内另一 job 已落报告：唯一约束兜底，返回既有报告
            await self.session.rollback()
            existing = await self.session.execute(
                select(Report.id)
                .where(Report.task_id == task_id)
                .order_by(Report.id.desc())
                .limit(1)
            )
            return existing.scalar_one()

    async def latest_report(self, task_id: int) -> Report | None:
        """任务已有报告（synthesize 幂等用）：一任务只保留最新一份。"""
        result = await self.session.execute(
            select(Report).where(Report.task_id == task_id).order_by(Report.id.desc()).limit(1)
        )
        return result.scalars().first()

    async def mark_task_running(
        self, task_id: int, thread_id: str | None = None, run_token: str | None = None
    ) -> None:
        await self.session.execute(
            update(ResearchTask)
            .where(ResearchTask.id == task_id)
            .values(
                status=TaskStatus.running,
                error_msg=None,
                thread_id=ResearchTask.thread_id if thread_id is None else thread_id,
                run_token=ResearchTask.run_token if run_token is None else run_token,
            )
        )
        await self.session.commit()

    async def finish_task(
        self, task_id: int, done: bool, error: str | None = None, run_token: str | None = None
    ) -> None:
        """终态回写：仅在 running 且（传入时）令牌仍归本 job 时生效。

        暂停/终止/被 resume 新 job 抢占的场景，状态归控制方或新 job 所有，
        旧 job 不得覆盖。
        """
        conditions = [ResearchTask.id == task_id, ResearchTask.status == TaskStatus.running]
        if run_token is not None:
            conditions.append(ResearchTask.run_token == run_token)
        await self.session.execute(
            update(ResearchTask)
            .where(*conditions)
            .values(
                status=TaskStatus.done if done else TaskStatus.failed,
                error_msg=error,
                finished_at=datetime.now(UTC),
            )
        )
        await self.session.commit()

    # ---- 追加指示信箱（JSONB 读改写，追加竞态窗口为毫秒级，可接受） ----

    async def append_instruction(self, task_id: int, instruction_text: str) -> dict:
        """追加一条指示：{id, text, created_at, consumed_round: null}。"""
        task = await self.session.get(ResearchTask, task_id)
        inbox: list[dict] = list(task.extra_instructions or [])
        next_id = max((int(i.get("id", 0)) for i in inbox), default=0) + 1
        item = {
            "id": next_id,
            "text": instruction_text,
            "created_at": datetime.now(UTC).isoformat(),
            "consumed_round": None,
        }
        inbox.append(item)
        task.extra_instructions = inbox
        await self.session.commit()
        return item

    async def pending_instructions(self, task_id: int) -> list[dict]:
        """未消费的追加指示，按 id 升序。

        populate_existing 强制回库：mark_instructions_consumed 的 raw UPDATE
        绕过 ORM，identity map 里的旧值会让指示被重复消费。
        """
        task = (
            (
                await self.session.execute(
                    select(ResearchTask)
                    .where(ResearchTask.id == task_id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if not task or not task.extra_instructions:
            return []
        return [i for i in task.extra_instructions if i.get("consumed_round") is None]

    async def mark_instructions_consumed(self, task_id: int, round_no: int) -> None:
        """原子消费：单条 UPDATE 在服务端完成，避免与 API 追加并发丢失。

        round_no 显式 CAST：jsonb_build_object 参数类型无法推断。
        """
        await self.session.execute(
            text("""
                UPDATE research_tasks
                SET extra_instructions = COALESCE((
                    SELECT jsonb_agg(
                        CASE WHEN elem->>'consumed_round' IS NULL
                             THEN elem || jsonb_build_object(
                                 'consumed_round', CAST(:round_no AS integer)
                             )
                             ELSE elem
                        END
                        ORDER BY (elem->>'id')::int
                    )
                    FROM jsonb_array_elements(extra_instructions) AS elem
                ), '[]'::jsonb)
                WHERE id = :task_id
            """),
            {"task_id": task_id, "round_no": round_no},
        )
        await self.session.commit()
