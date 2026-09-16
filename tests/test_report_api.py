import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, Note, Report, ResearchTask, Source, TaskStatus
from app.db.base import get_session
from app.main import app
from app.queue import get_queue

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"


class FakeQueue:
    async def enqueue_job(self, name, *args, **kwargs):
        return None


@pytest.fixture
async def client():
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_queue] = lambda: FakeQueue()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, maker
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed_done_task(maker, *, with_report=True):
    """造一个 done 任务：2 信源 + 笔记（锚点已重写为信源库 id）+ 报告。"""
    async with maker() as session:
        task = ResearchTask(
            question="国产大模型医疗行业落地情况",
            status=TaskStatus.done,
            token_budget=80_000,
            token_used=41_302,
            cost_cny=0.31,
        )
        session.add(task)
        await session.flush()

        s1 = Source(
            task_id=task.id,
            url="https://www.gov.cn/policy/llm",
            title="医疗 AI 政策解读",
            domain="gov.cn",
            credibility=5,
            freshness=0.9,
        )
        s2 = Source(
            task_id=task.id,
            url="https://news.example.com/llm-med",
            title="大模型医疗落地盘点",
            domain="news.example.com",
            credibility=3,
            freshness=0.6,
        )
        session.add_all([s1, s2])
        await session.flush()

        session.add(
            Note(
                task_id=task.id,
                content=f"监管层在 2025 年 3 月发布三类证审批新规[{s1.id}]，审批周期缩短 40%。"
                f"厂商侧已有 120 家完成备案[{s2.id}]。",
            )
        )
        if with_report:
            session.add(
                Report(
                    task_id=task.id,
                    version=1,
                    markdown=f"# 报告\n\n监管政策收紧[{s1.id}]，市场规模扩张[{s2.id}][{s1.id}]。",
                    citation_map={"1": s1.id, "2": s2.id},
                    token_total=9_800,
                )
            )
        await session.commit()
        return task.id


async def test_get_report_returns_sources_with_excerpts(client):
    c, maker = client
    task_id = await _seed_done_task(maker)

    resp = await c.get(f"/api/research/tasks/{task_id}/report")
    assert resp.status_code == 200
    data = resp.json()

    assert data["task_id"] == task_id
    assert data["question"] == "国产大模型医疗行业落地情况"
    assert "监管政策收紧[1]" in data["markdown"]
    assert data["citation_map"] == {"1": data["sources"][0]["id"], "2": data["sources"][1]["id"]}

    # 信源卡按展示编号排序，摘录从笔记提取（锚点已剥除）
    first, second = data["sources"]
    assert first["no"] == 1 and second["no"] == 2
    assert first["domain"] == "gov.cn" and first["credibility"] == 5
    assert any("三类证审批新规" in e for e in first["excerpts"])
    assert any("[1]" not in e and "[2]" not in e for e in first["excerpts"])
    assert any("120 家" in e for e in second["excerpts"])


async def test_get_report_not_found_variants(client):
    c, maker = client
    task_id = await _seed_done_task(maker, with_report=False)

    # 任务存在但无报告
    resp = await c.get(f"/api/research/tasks/{task_id}/report")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "report not found"

    # 任务不存在
    resp = await c.get("/api/research/tasks/99999/report")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "task not found"


async def test_report_with_uncited_source_omits_it(client):
    """未被 citation_map 引用的信源不出现在报告信源卡中。"""
    c, maker = client
    async with maker() as session:
        task = ResearchTask(question="仅引用部分信源", status=TaskStatus.done, token_budget=80000)
        session.add(task)
        await session.flush()
        s = Source(
            task_id=task.id, url="https://a.example.com/x", title="被引用", domain="a.example.com"
        )
        unused = Source(
            task_id=task.id, url="https://b.example.com/y", title="未被引用", domain="b.example.com"
        )
        session.add_all([s, unused])
        await session.flush()
        session.add(
            Report(
                task_id=task.id,
                version=1,
                markdown=f"正文引用[{s.id}]。",
                citation_map={"1": s.id},
                token_total=100,
            )
        )
        await session.commit()
        task_id = task.id

    resp = await c.get(f"/api/research/tasks/{task_id}/report")
    assert resp.status_code == 200
    data = resp.json()
    assert [s["no"] for s in data["sources"]] == [1]
    assert data["sources"][0]["title"] == "被引用"
