import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AgentEvent, ResearchTask, Source, TaskStatus
from app.db.base import get_session
from app.engine.persister import SubTaskPersister
from app.engine.planner import DEPTH_BUDGETS
from app.queue import enqueue_research, get_queue
from app.schemas.task import (
    EventOut,
    InstructionCreate,
    InstructionOut,
    SourceOut,
    TaskControl,
    TaskCreate,
    TaskDetail,
    TaskOut,
)
from app.services import event_bus

router = APIRouter(prefix="/research/tasks", tags=["research"])

TERMINAL_STATUSES = (TaskStatus.done, TaskStatus.failed, TaskStatus.canceled, TaskStatus.stopped)


async def _events_after(session: AsyncSession, task_id: int, after_seq: int) -> list[AgentEvent]:
    result = await session.execute(
        select(AgentEvent)
        .where(AgentEvent.task_id == task_id, AgentEvent.seq > after_seq)
        .order_by(AgentEvent.seq)
    )
    return list(result.scalars().all())


async def _task_status(session: AsyncSession, task_id: int) -> TaskStatus | None:
    result = await session.execute(select(ResearchTask.status).where(ResearchTask.id == task_id))
    return result.scalar_one_or_none()


def _sse_event(event: AgentEvent | dict) -> str:
    if isinstance(event, AgentEvent):
        event = {
            "seq": event.seq,
            "task_id": event.task_id,
            "sub_task_id": event.sub_task_id,
            "type": str(event.type),
            "payload": event.payload or {},
            "tokens": event.tokens,
            "latency_ms": event.latency_ms,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }
    data = json.dumps(event, ensure_ascii=False)
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(
    body: TaskCreate,
    session: AsyncSession = Depends(get_session),
    queue=Depends(get_queue),
) -> ResearchTask:
    task = ResearchTask(
        question=body.question,
        background=body.background,
        depth=body.depth,
        status=TaskStatus.queued,
        token_budget=body.token_budget or DEPTH_BUDGETS[body.depth],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    await enqueue_research(queue, task.id)
    return task


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    session: AsyncSession = Depends(get_session),
) -> list[ResearchTask]:
    result = await session.execute(select(ResearchTask).order_by(ResearchTask.id.desc()))
    return list(result.scalars().all())


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(task_id: int, session: AsyncSession = Depends(get_session)) -> ResearchTask:
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    await session.refresh(task, attribute_names=["sub_tasks"])
    return task


@router.post("/{task_id}/instructions", response_model=InstructionOut, status_code=201)
async def add_instruction(
    task_id: int, body: InstructionCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    """追加研究指示：写入信箱，由执行中的 reflect 节点在下一轮消费。"""
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status in (TaskStatus.done, TaskStatus.canceled, TaskStatus.stopped):
        raise HTTPException(
            status_code=409, detail=f"task in terminal status '{task.status.value}'"
        )
    persister = SubTaskPersister(session)
    return await persister.append_instruction(task_id, body.text)


@router.post("/{task_id}/control", response_model=TaskOut)
async def control_task(
    task_id: int,
    body: TaskControl,
    session: AsyncSession = Depends(get_session),
    queue=Depends(get_queue),
) -> ResearchTask:
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    transitions: dict[tuple[TaskStatus, str], TaskStatus] = {
        (TaskStatus.running, "pause"): TaskStatus.paused,
        # resume → queued 并重新入队：worker 崩溃/被 kill 后残留的 running、
        # paused、failed 均可续跑（子任务级幂等保证不重复消耗）
        (TaskStatus.paused, "resume"): TaskStatus.queued,
        (TaskStatus.running, "resume"): TaskStatus.queued,
        (TaskStatus.failed, "resume"): TaskStatus.queued,
        (TaskStatus.queued, "stop"): TaskStatus.canceled,
        (TaskStatus.paused, "stop"): TaskStatus.stopped,
        (TaskStatus.running, "stop"): TaskStatus.stopped,
    }
    key = (task.status, body.action)
    if key not in transitions:
        raise HTTPException(
            status_code=409,
            detail=f"action '{body.action}' not allowed in status '{task.status.value}'",
        )
    task.status = transitions[key]
    await session.commit()
    if body.action == "resume":
        await enqueue_research(queue, task_id)
    await session.refresh(task)
    return task


@router.get("/{task_id}/sources", response_model=list[SourceOut])
async def list_sources(task_id: int, session: AsyncSession = Depends(get_session)) -> list[Source]:
    """任务信源列表：右栏信源卡数据源（含可信度与新鲜度评分）。"""
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    result = await session.execute(
        select(Source).where(Source.task_id == task_id).order_by(Source.id)
    )
    return list(result.scalars().all())


@router.get("/{task_id}/events", response_model=list[EventOut])
async def list_events(
    task_id: int,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> list[AgentEvent]:
    """序号回放（JSON）：返回 seq > after_seq 的事件，供轮询或 SSE 断连后批量补拉。"""
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    result = await session.execute(
        select(AgentEvent)
        .where(AgentEvent.task_id == task_id, AgentEvent.seq > after_seq)
        .order_by(AgentEvent.seq)
        .limit(limit)
    )
    return list(result.scalars().all())


@router.get("/{task_id}/events/stream")
async def stream_events(
    task_id: int,
    request: Request,
    after_seq: int | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """SSE 事件流：连接即回放 seq > after_seq 的历史，再实时转发 pub/sub 新事件。

    断线重连：EventSource 自动携带 Last-Event-ID 请求头（即上次收到的最大 seq），
    亦可显式传 after_seq 查询参数；两条通道按 seq 去重，界面不丢动作。
    Redis 不可用时自动退化为心跳周期 DB 轮询，流不中断。
    """
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    last_event_id = request.headers.get("last-event-id")
    if after_seq is not None:
        start_after = after_seq
    elif last_event_id:
        try:
            start_after = int(last_event_id)
        except ValueError:
            start_after = 0
    else:
        start_after = 0

    return StreamingResponse(
        _sse_generator(task_id, start_after, session),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse_generator(task_id: int, start_after: int, session: AsyncSession):
    from app.config import get_settings

    heartbeat_s = get_settings().sse_heartbeat_s
    last_seq = start_after
    pubsub = await event_bus.open_subscription(task_id)
    try:
        # 先订阅再回放：订阅与回放之间产生的事件会出现在回放结果里，按 seq 去重
        for ev in await _events_after(session, task_id, last_seq):
            last_seq = ev.seq
            yield _sse_event(ev)

        status = await _task_status(session, task_id)
        if status in TERMINAL_STATUSES:
            yield f"event: end\ndata: {json.dumps({'status': str(status)})}\n\n"
            return

        while True:
            if pubsub is not None:
                try:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=heartbeat_s
                    )
                except Exception:
                    # 连接中断：本连接降级为轮询模式，流不中断
                    await event_bus.close_subscription(pubsub)
                    pubsub = None
                    continue
                if msg and msg.get("type") == "message":
                    try:
                        event = json.loads(msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        event = None
                    if event and event.get("seq", 0) > last_seq:
                        last_seq = event["seq"]
                        yield _sse_event(event)
                        continue
            else:
                await asyncio.sleep(heartbeat_s)

            # 心跳周期：DB 轮询兜底（Redis 不可用 / 发布间隙漏发），并检查终态
            for ev in await _events_after(session, task_id, last_seq):
                last_seq = ev.seq
                yield _sse_event(ev)
            status = await _task_status(session, task_id)
            if status in TERMINAL_STATUSES:
                yield f"event: end\ndata: {json.dumps({'status': str(status)})}\n\n"
                return
            yield ": heartbeat\n\n"
    finally:
        await event_bus.close_subscription(pubsub)
