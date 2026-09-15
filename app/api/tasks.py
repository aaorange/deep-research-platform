from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import ResearchTask, TaskStatus
from app.db.base import get_session
from app.engine.planner import DEPTH_BUDGETS
from app.schemas.task import TaskControl, TaskCreate, TaskDetail, TaskOut

router = APIRouter(prefix="/research/tasks", tags=["research"])


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(
    body: TaskCreate, session: AsyncSession = Depends(get_session)
) -> ResearchTask:
    task = ResearchTask(
        question=body.question,
        background=body.background,
        depth=body.depth,
        status=TaskStatus.queued,
        token_budget=DEPTH_BUDGETS[body.depth],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
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


@router.post("/{task_id}/control", response_model=TaskOut)
async def control_task(
    task_id: int,
    body: TaskControl,
    session: AsyncSession = Depends(get_session),
) -> ResearchTask:
    task = await session.get(ResearchTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    transitions: dict[tuple[TaskStatus, str], TaskStatus] = {
        (TaskStatus.running, "pause"): TaskStatus.paused,
        (TaskStatus.paused, "resume"): TaskStatus.running,
        (TaskStatus.queued, "stop"): TaskStatus.canceled,
        (TaskStatus.paused, "stop"): TaskStatus.stopped,
        (TaskStatus.running, "stop"): TaskStatus.stopped,
        (TaskStatus.failed, "resume"): TaskStatus.running,
    }
    key = (task.status, body.action)
    if key not in transitions:
        raise HTTPException(
            status_code=409,
            detail=f"action '{body.action}' not allowed in status '{task.status.value}'",
        )
    task.status = transitions[key]
    await session.commit()
    await session.refresh(task)
    return task
