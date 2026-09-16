import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import AgentEvent, ChatMessage, ChatRole, Note, Report, ResearchTask, Source, TaskStatus
from app.db.base import get_session
from app.engine.persister import SubTaskPersister
from app.engine.planner import DEPTH_BUDGETS
from app.queue import enqueue_research, get_queue
from app.schemas.task import (
    ChatCreate,
    ChatMessageOut,
    EventOut,
    InstructionCreate,
    InstructionOut,
    ReportOut,
    ReportSourceOut,
    SourceOut,
    TaskControl,
    TaskCreate,
    TaskDetail,
    TaskOut,
)
from app.services import event_bus
from app.services.chat import write_chat_reply
from app.services.excerpt import extract_excerpts

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


@router.get("/{task_id}/report", response_model=ReportOut)
async def get_report(task_id: int, session: AsyncSession = Depends(get_session)) -> ReportOut:
    """报告阅读页数据：markdown + citation_map + 按展示编号排序的信源卡（含摘录）。

    摘录取自笔记正文中引用该信源的句子（锚点已重写为信源库 id），
    任务无报告时 404（前端据此显示空态）。
    """
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    report = (
        (
            await session.execute(
                select(Report).where(Report.task_id == task_id).order_by(Report.id.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")

    citation_map: dict[str, int] = {
        str(n): int(sid) for n, sid in (report.citation_map or {}).items()
    }
    cited_ids = sorted(set(citation_map.values()))

    source_rows: dict[int, Source] = {}
    note_contents: list[str] = []
    if cited_ids:
        rows = (
            (
                await session.execute(
                    select(Source).where(Source.task_id == task_id, Source.id.in_(cited_ids))
                )
            )
            .scalars()
            .all()
        )
        source_rows = {s.id: s for s in rows}
        note_contents = list(
            (
                await session.execute(
                    select(Note.content).where(Note.task_id == task_id).order_by(Note.id)
                )
            )
            .scalars()
            .all()
        )

    sources = [
        ReportSourceOut(
            no=int(no),
            id=s.id,
            url=s.url,
            title=s.title,
            domain=s.domain,
            credibility=s.credibility,
            freshness=s.freshness,
            excerpts=extract_excerpts(note_contents, s.id),
        )
        for no, sid in sorted(citation_map.items(), key=lambda kv: int(kv[0]))
        if (s := source_rows.get(sid)) is not None
    ]
    return ReportOut(
        report_id=report.id,
        task_id=task_id,
        question=task.question,
        depth=task.depth,
        markdown=report.markdown,
        citation_map=citation_map,
        token_total=report.token_total,
        cost_cny=task.cost_cny,
        sources=sources,
        chart_specs=report.chart_specs or [],
    )


@router.get("/{task_id}/chat", response_model=list[ChatMessageOut])
async def list_chat(
    task_id: int, session: AsyncSession = Depends(get_session)
) -> list[ChatMessage]:
    """追问历史：报告页进入时加载对话。"""
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    result = await session.execute(
        select(ChatMessage).where(ChatMessage.task_id == task_id).order_by(ChatMessage.id)
    )
    return list(result.scalars().all())


@router.post("/{task_id}/chat", response_model=ChatMessageOut, status_code=201)
async def send_chat(
    task_id: int, body: ChatCreate, session: AsyncSession = Depends(get_session)
) -> ChatMessage:
    """报告追问：仅基于已收集笔记回答（不联网、不触发新搜索）。

    锚点校验保证回答可溯源——[N] 只能是信源库 id，cited_source_ids
    供 UI 标注信源范围。LLM 失败时 502，user 消息不落库（重试无副作用）。
    """
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != TaskStatus.done:
        raise HTTPException(status_code=409, detail="chat is available after the task is done")
    report = (
        (
            await session.execute(
                select(Report.id)
                .where(Report.task_id == task_id)
                .order_by(Report.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if report is None:
        raise HTTPException(status_code=409, detail="report not generated")

    persister = SubTaskPersister(session)
    notes, sources = await persister.synthesis_inputs(task_id)
    if not notes:
        raise HTTPException(status_code=409, detail="no notes collected for this task")
    history_rows = list(
        (
            await session.execute(
                select(ChatMessage).where(ChatMessage.task_id == task_id).order_by(ChatMessage.id)
            )
        )
        .scalars()
        .all()
    )
    history = [{"role": m.role.value, "content": m.content} for m in history_rows]

    try:
        reply, usage = await write_chat_reply(task.question, notes, sources, history, body.text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"chat failed: {type(e).__name__}") from e

    session.add(ChatMessage(task_id=task_id, role=ChatRole.user, content=body.text))
    assistant = ChatMessage(
        task_id=task_id,
        role=ChatRole.assistant,
        content=reply.answer,
        cited_source_ids=reply.cited_ids,
    )
    session.add(assistant)
    if usage is not None:
        await persister.add_usage(
            task_id, get_settings().llm_model_chat, usage.prompt_tokens, usage.completion_tokens
        )
    else:
        await session.commit()
    await session.refresh(assistant)
    return assistant


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
