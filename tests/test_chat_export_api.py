import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, Note, Report, ResearchTask, Source, TaskStatus
from app.db.base import get_session
from app.main import app
from app.queue import get_queue
from app.services.chat import ChatReply

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"

CHART_SPECS = [
    {
        "id": "c1",
        "title": "市场规模趋势",
        "type": "bar",
        "x": ["2023", "2024", "2025"],
        "series": [{"name": "亿元", "data": [42, 58, 82]}],
    }
]
REPORT_MD = "# 报告\n\n规模见下表 [1]。\n\n<!-- chart:c1 -->\n\n<script>alert(1)</script> 部分。"


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


async def seed_done_task(maker, *, status=TaskStatus.done, with_report=True):
    async with maker() as session:
        task = ResearchTask(
            question="国产大模型医疗落地情况", status=status, token_budget=80_000, cost_cny=0.4
        )
        session.add(task)
        await session.flush()
        s = Source(
            task_id=task.id,
            url="https://gov.cn/p",
            title="政策公告",
            domain="gov.cn",
            credibility=5,
        )
        session.add(s)
        await session.flush()
        session.add(Note(task_id=task.id, content=f"审批周期缩短 40% [{s.id}]。"))
        if with_report:
            session.add(
                Report(
                    task_id=task.id,
                    version=1,
                    markdown=REPORT_MD,
                    chart_specs=CHART_SPECS,
                    citation_map={"1": s.id},
                    token_total=9_800,
                )
            )
        await session.commit()
        return task.id, s.id


# ---- 报告 API 返回图表规格 ----


async def test_report_api_returns_chart_specs(client):
    c, maker = client
    task_id, _ = await seed_done_task(maker)
    resp = await c.get(f"/api/research/tasks/{task_id}/report")
    assert resp.status_code == 200
    assert resp.json()["chart_specs"] == CHART_SPECS


# ---- 追问 API ----


async def test_chat_roundtrip(client, monkeypatch):
    c, maker = client
    task_id, src_id = await seed_done_task(maker)

    async def fake_reply(question, notes, sources, history, followup):
        assert followup == "审批周期缩短了多少？"
        assert history == []
        return ChatReply(answer=f"缩短了 40% [{src_id}]。"), None

    import app.api.tasks as tasks_api

    monkeypatch.setattr(tasks_api, "write_chat_reply", fake_reply)

    resp = await c.post(
        f"/api/research/tasks/{task_id}/chat", json={"text": "审批周期缩短了多少？"}
    )
    assert resp.status_code == 201
    msg = resp.json()
    assert msg["role"] == "assistant"
    assert "40%" in msg["content"]
    assert msg["cited_source_ids"] == [src_id]

    # 多轮：第二轮能看到第一轮历史
    async def fake_reply2(question, notes, sources, history, followup):
        assert [h["role"] for h in history] == ["user", "assistant"]
        return ChatReply(answer="如前所述。"), None

    monkeypatch.setattr(tasks_api, "write_chat_reply", fake_reply2)
    resp = await c.post(f"/api/research/tasks/{task_id}/chat", json={"text": "还有别的结论吗"})
    assert resp.status_code == 201

    # 历史接口：4 条（两轮 user+assistant）
    resp = await c.get(f"/api/research/tasks/{task_id}/chat")
    assert resp.status_code == 200
    assert [m["role"] for m in resp.json()] == ["user", "assistant", "user", "assistant"]


async def test_chat_guards(client):
    c, maker = client
    # 非 done 任务 409
    task_id, _ = await seed_done_task(maker, status=TaskStatus.running)
    resp = await c.post(f"/api/research/tasks/{task_id}/chat", json={"text": "追问内容"})
    assert resp.status_code == 409

    # done 但无报告 409
    task_id, _ = await seed_done_task(maker, with_report=False)
    resp = await c.post(f"/api/research/tasks/{task_id}/chat", json={"text": "追问内容"})
    assert resp.status_code == 409

    # 文本过短 422
    task_id, _ = await seed_done_task(maker)
    resp = await c.post(f"/api/research/tasks/{task_id}/chat", json={"text": "问"})
    assert resp.status_code == 422

    # 任务不存在 404
    resp = await c.post("/api/research/tasks/99999/chat", json={"text": "追问内容"})
    assert resp.status_code == 404


# ---- 导出 API ----


async def test_export_markdown(client):
    c, maker = client
    task_id, _ = await seed_done_task(maker)
    report_id = (await c.get(f"/api/research/tasks/{task_id}/report")).json()["report_id"]

    resp = await c.get(f"/api/reports/{report_id}/export?format=md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "<!-- chart:c1 -->" in resp.text
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]


async def test_export_html(client):
    c, maker = client
    task_id, _ = await seed_done_task(maker)
    report_id = (await c.get(f"/api/research/tasks/{task_id}/report")).json()["report_id"]

    resp = await c.get(f"/api/reports/{report_id}/export?format=html")
    assert resp.status_code == 200
    body = resp.text
    # 图表容器与规格内联
    assert '<div class="chart" data-chart="c1"></div>' in body
    assert '"c1"' in body and '"市场规模趋势"' in body
    # 信源清单
    assert "https://gov.cn/p" in body
    # 注入的 <script> 被转义为文本（渲染前转义尖括号）
    assert "<script>alert(1)</script>" not in body.replace("<script src=", "")
    assert "&lt;script&gt;" in body
    # 引用角标为上标
    assert '<sup class="cite">[1]</sup>' in body


async def test_export_pdf(client):
    c, maker = client
    task_id, _ = await seed_done_task(maker)
    report_id = (await c.get(f"/api/research/tasks/{task_id}/report")).json()["report_id"]

    resp = await c.get(f"/api/reports/{report_id}/export?format=pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:5] == b"%PDF-"


async def test_export_guards(client):
    c, maker = client
    resp = await c.get("/api/reports/99999/export?format=md")
    assert resp.status_code == 404

    task_id, _ = await seed_done_task(maker)
    report_id = (await c.get(f"/api/research/tasks/{task_id}/report")).json()["report_id"]
    resp = await c.get(f"/api/reports/{report_id}/export?format=docx")
    assert resp.status_code == 422
