import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, Note, ResearchTask, Source, SubTask, SubTaskStatus, TaskStatus
from app.engine.persister import SubTaskPersister, cost_cny

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"


@pytest.fixture
async def session_maker():
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _make_task(maker) -> int:
    async with maker() as session:
        task = ResearchTask(question="评分落库测试", status=TaskStatus.running, token_budget=80000)
        session.add(task)
        await session.commit()
        return task.id


async def test_cost_cny():
    # 1000 prompt + 500 completion on deepseek-chat: 2/8 每百万
    c = cost_cny("deepseek-chat", 1000, 500)
    assert abs(c - (1000 * 2 + 500 * 8) / 1_000_000) < 1e-9
    r = cost_cny("deepseek-reasoner", 1_000_000, 1_000_000)
    assert abs(r - 20.0) < 1e-9


async def test_persist_sources_with_scores(session_maker):
    task_id = await _make_task(session_maker)
    sources = [
        {
            "idx": 1,
            "url": "https://www.thepaper.cn/a",
            "title": "报道",
            "domain": "thepaper.cn",
            "credibility": 4,
            "freshness": 0.93,
            "content_hash": "h1",
        },
        {
            "idx": 2,
            "url": "https://old.gov.cn/2019/x",
            "title": "旧公告",
            "domain": "gov.cn",
            "credibility": 5,
            "freshness": 0.12,
            "content_hash": "h2",
        },
    ]

    async with session_maker() as session:
        p = SubTaskPersister(session)
        idx_to_id = await p.persist_sources(task_id, sources)

    assert idx_to_id == {1: idx_to_id[1], 2: idx_to_id[2]}
    assert idx_to_id[1] != idx_to_id[2]

    async with session_maker() as session:
        rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    assert len(rows) == 2
    assert rows[0].credibility == 4
    assert rows[0].freshness == 0.93
    assert rows[1].credibility == 5
    assert rows[1].freshness == 0.12


async def test_persist_note_rewrites_anchors(session_maker):
    task_id = await _make_task(session_maker)
    async with session_maker() as session:
        sub = SubTask(task_id=task_id, title="锚点重写测试")
        session.add(sub)
        await session.commit()
        sub_task_id = sub.id

    sources = [
        {
            "idx": 10,
            "url": "https://a.com/1",
            "title": "t",
            "domain": "a.com",
            "credibility": 3,
            "freshness": 0.5,
            "content_hash": "x1",
        },
        {
            "idx": 11,
            "url": "https://b.com/2",
            "title": "t2",
            "domain": "b.com",
            "credibility": 3,
            "freshness": 0.5,
            "content_hash": "x2",
        },
    ]
    note = {"summary": "事实甲 [10]，事实乙 [11]，共通 [10][11]。"}

    async with session_maker() as session:
        p = SubTaskPersister(session)
        idx_to_id = await p.persist_sources(task_id, sources)
        note_id = await p.persist_note(task_id, sub_task_id, note, idx_to_id)

    async with session_maker() as session:
        row = await session.get(Note, note_id)
    assert row.task_id == task_id
    assert row.sub_task_id == sub_task_id
    assert f"[{idx_to_id[10]}]" in row.content
    assert f"[{idx_to_id[11]}]" in row.content
    assert "[10]" not in row.content  # 旧锚点已替换


async def test_add_usage_accumulates(session_maker):
    task_id = await _make_task(session_maker)
    async with session_maker() as session:
        p = SubTaskPersister(session)
        await p.add_usage(task_id, "deepseek-chat", 3000, 400)
        await p.add_usage(task_id, "deepseek-chat", 2000, 600)

    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
    assert task.token_used == 6000
    assert abs(task.cost_cny - (5000 * 2 + 1000 * 8) / 1_000_000) < 1e-6


async def test_create_and_pending_sub_tasks(session_maker):
    task_id = await _make_task(session_maker)
    async with session_maker() as session:
        p = SubTaskPersister(session)
        created = await p.create_sub_tasks(
            task_id,
            [
                {"title": "医疗大模型现状", "keywords": "医疗 大模型 现状"},
                {"title": "落地案例", "keywords": None},
            ],
        )

    assert [c["title"] for c in created] == ["医疗大模型现状", "落地案例"]
    assert created[0]["id"] != created[1]["id"]

    async with session_maker() as session:
        p = SubTaskPersister(session)
        pending = await p.pending_sub_tasks(task_id)
        assert [r["title"] for r in pending] == ["医疗大模型现状", "落地案例"]

        await p.mark_sub_task_status(created[0]["id"], SubTaskStatus.done)
        await p.mark_sub_task_status(created[1]["id"], SubTaskStatus.failed, "all reads failed")
        assert await p.pending_sub_tasks(task_id) == []

    async with session_maker() as session:
        rows = list((await session.execute(select(SubTask).order_by(SubTask.id))).scalars().all())
    assert [r.status for r in rows] == [SubTaskStatus.done, SubTaskStatus.failed]
    assert rows[1].error_msg == "all reads failed"


async def test_running_sub_task_stays_pending_for_resume(session_maker):
    """崩溃残留的 running 状态仍在 pending 里 → resume 时重跑。"""
    task_id = await _make_task(session_maker)
    async with session_maker() as session:
        p = SubTaskPersister(session)
        created = await p.create_sub_tasks(task_id, [{"title": "甲", "keywords": None}])
        await p.mark_sub_task_status(created[0]["id"], SubTaskStatus.running)

        pending = await p.pending_sub_tasks(task_id)

    assert [r["title"] for r in pending] == ["甲"]
