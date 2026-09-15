import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import AgentEvent, EventType, ResearchTask, TaskStatus
from app.services.event_recorder import EventRecorder

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"

ENGINE_KWARGS = {"pool_size": 30, "max_overflow": 10}


@pytest.fixture
async def session_maker():
    engine = create_async_engine(TEST_DB_URL, **ENGINE_KWARGS)
    async with engine.begin() as conn:
        await conn.run_sync(AgentEvent.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    async with engine.begin() as conn:
        await conn.run_sync(AgentEvent.metadata.drop_all)
    await engine.dispose()


async def _make_task(maker) -> int:
    async with maker() as session:
        task = ResearchTask(question="并发事件测试", status=TaskStatus.running, token_budget=80000)
        session.add(task)
        await session.commit()
        return task.id


async def test_concurrent_100_events_no_gap_no_dup(session_maker):
    task_id = await _make_task(session_maker)

    async def write_one(i: int) -> None:
        async with session_maker() as session:
            recorder = EventRecorder(session)
            await recorder.record(task_id, EventType.search, {"i": i, "q": "测试"})

    sem = asyncio.Semaphore(25)

    async def bounded(i: int) -> None:
        async with sem:
            await write_one(i)

    await asyncio.gather(*[bounded(i) for i in range(100)])

    async with session_maker() as session:
        count = await session.scalar(
            select(func.count()).select_from(AgentEvent).where(AgentEvent.task_id == task_id)
        )
        seqs = (
            (
                await session.execute(
                    select(AgentEvent.seq)
                    .where(AgentEvent.task_id == task_id)
                    .order_by(AgentEvent.seq)
                )
            )
            .scalars()
            .all()
        )

    assert count == 100
    assert seqs == list(range(1, 101))


async def test_record_many_batch(session_maker):
    task_id = await _make_task(session_maker)

    events = [
        {"type": EventType.plan, "payload": {"sub": 5}, "tokens": 1200},
        {"type": EventType.search, "payload": {"q": "a"}, "latency_ms": 900},
        {"type": EventType.note, "payload": {"text": "n"}, "tokens": 400},
    ]
    async with session_maker() as session:
        recorder = EventRecorder(session)
        rows = await recorder.record_many(task_id, events)

    assert [r.seq for r in rows] == [1, 2, 3]

    async with session_maker() as session:
        recorder = EventRecorder(session)
        row = await recorder.record(task_id, EventType.reflect, {"round": 1})
    assert row.seq == 4
