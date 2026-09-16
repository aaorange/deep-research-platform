from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AgentEvent, EventType, ResearchTask
from app.services import event_bus


def _event_dict(event: AgentEvent) -> dict:
    # record_many 未逐行 refresh，server_default 的 created_at 可能未加载（异步下访问会抛
    # MissingGreenlet）——实时推送缺该字段无碍，回放接口以 DB 为准。
    try:
        created_at = event.created_at.isoformat() if event.created_at else None
    except Exception:
        created_at = None
    return {
        "seq": event.seq,
        "task_id": event.task_id,
        "sub_task_id": event.sub_task_id,
        "type": str(event.type),
        "payload": event.payload or {},
        "tokens": event.tokens,
        "latency_ms": event.latency_ms,
        "created_at": created_at,
    }


class EventRecorder:
    """agent_events 写入组件。

    seq 通过 research_tasks.event_seq 行锁原子递增分配：
    UPDATE ... RETURNING 在同一事务内完成取号与写入，
    并发下无重复、无空洞，事务回滚时号与事件一起回滚。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(
        self,
        task_id: int,
        type: EventType,
        payload: dict | None = None,
        sub_task_id: int | None = None,
        tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> AgentEvent:
        result = await self.session.execute(
            update(ResearchTask)
            .where(ResearchTask.id == task_id)
            .values(event_seq=ResearchTask.event_seq + 1)
            .returning(ResearchTask.event_seq)
        )
        seq = result.scalar_one()

        event = AgentEvent(
            task_id=task_id,
            sub_task_id=sub_task_id,
            seq=seq,
            type=type,
            payload=payload or {},
            tokens=tokens,
            latency_ms=latency_ms,
        )
        self.session.add(event)
        await self.session.commit()
        await self.session.refresh(event)
        await event_bus.publish_event(task_id, _event_dict(event))
        return event

    async def record_many(
        self,
        task_id: int,
        events: list[dict],
    ) -> list[AgentEvent]:
        """批量写入：一次行锁取一段连续 seq，减少提交次数。"""
        result = await self.session.execute(
            update(ResearchTask)
            .where(ResearchTask.id == task_id)
            .values(event_seq=ResearchTask.event_seq + len(events))
            .returning(ResearchTask.event_seq)
        )
        end_seq = result.scalar_one()
        start_seq = end_seq - len(events) + 1

        rows = []
        for offset, item in enumerate(events):
            rows.append(
                AgentEvent(
                    task_id=task_id,
                    sub_task_id=item.get("sub_task_id"),
                    seq=start_seq + offset,
                    type=item["type"],
                    payload=item.get("payload") or {},
                    tokens=item.get("tokens"),
                    latency_ms=item.get("latency_ms"),
                )
            )
        self.session.add_all(rows)
        await self.session.commit()
        for row in rows:
            await event_bus.publish_event(task_id, _event_dict(row))
        return rows
