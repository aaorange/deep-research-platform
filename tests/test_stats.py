"""成本看板对账单测：看板数字必须与 agent_events 聚合结果一致。

验收标准（D17）：总成本 / Token 拆分 / 缓存命中率 / 节省金额，全部可由
手工 SUM 事件推导。数据落在 research_test 库。
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import AgentEvent, Base, EventType, ResearchTask, TaskStatus
from app.db.base import get_session
from app.engine.persister import PRICE_PER_M, cost_cny
from app.main import app
from app.queue import get_queue

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"

CHAT_MODEL = get_settings().llm_model_chat
REASONER_MODEL = get_settings().llm_model_reasoner


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


async def _add_task(maker, *, question, depth, status, cost, created_at=None):
    async with maker() as session:
        task = ResearchTask(
            question=question,
            depth=depth,
            status=status,
            token_budget=80_000,
            cost_cny=cost,
            created_at=created_at or datetime.now(UTC),
        )
        session.add(task)
        await session.commit()
        return task.id


async def _add_event(maker, task_id, seq, type_, payload=None, tokens=None, created_at=None):
    async with maker() as session:
        session.add(
            AgentEvent(
                task_id=task_id,
                seq=seq,
                type=type_,
                payload=payload or {},
                tokens=tokens,
                created_at=created_at or datetime.now(UTC),
            )
        )
        await session.commit()


async def seed_full(maker):
    """两个任务 + 全谱系事件：新格式（带 model 拆分）、旧格式（仅 tokens）、缓存事件。"""
    t1 = await _add_task(maker, question="研究一", depth="std", status=TaskStatus.done, cost=0.5)
    t2 = await _add_task(maker, question="研究二", depth="quick", status=TaskStatus.done, cost=0.2)

    # t1：新格式计费事件
    await _add_event(
        maker,
        t1,
        1,
        EventType.plan,
        {"model": CHAT_MODEL, "prompt_tokens": 1000, "completion_tokens": 100},
        tokens=1100,
    )
    await _add_event(
        maker,
        t1,
        2,
        EventType.note,
        {"model": CHAT_MODEL, "prompt_tokens": 2000, "completion_tokens": 300},
        tokens=2300,
    )
    await _add_event(
        maker,
        t1,
        3,
        EventType.reflect,
        {"model": CHAT_MODEL, "prompt_tokens": 500, "completion_tokens": 50},
        tokens=550,
    )
    await _add_event(
        maker,
        t1,
        4,
        EventType.synthesize,
        {"model": REASONER_MODEL, "prompt_tokens": 2000, "completion_tokens": 3000},
        tokens=5000,
    )
    # t1：缓存事件（search 3 中 1 命中，fetch 4 中 2 命中）
    await _add_event(maker, t1, 5, EventType.search, {"provider": "bocha", "query": "a"})
    await _add_event(maker, t1, 6, EventType.search, {"provider": "cache", "query": "b"})
    await _add_event(maker, t1, 7, EventType.search, {"provider": "ddg", "query": "c"})
    await _add_event(maker, t1, 8, EventType.fetch, {"provider": "jina", "url": "u1"})
    await _add_event(maker, t1, 9, EventType.fetch, {"provider": "cache", "url": "u2"})
    await _add_event(maker, t1, 10, EventType.fetch, {"provider": "cache", "url": "u3"})
    await _add_event(maker, t1, 11, EventType.fetch, {"provider": "trafilatura", "url": "u4"})

    # t2：旧格式事件（无 model / 无方向拆分）+ chat 追问
    await _add_event(maker, t2, 1, EventType.note, {}, tokens=4000)
    await _add_event(
        maker,
        t2,
        2,
        EventType.chat,
        {"model": CHAT_MODEL, "prompt_tokens": 800, "completion_tokens": 200},
        tokens=1000,
    )
    return t1, t2


async def test_stats_reconciles_with_events(client):
    c, maker = client
    await seed_full(maker)

    resp = await c.get("/api/stats?days=0")
    assert resp.status_code == 200
    d = resp.json()

    # ---- 对账：Token 总量 = SUM(events.tokens)，方向拆分精确 ----
    # 旧格式 note(4000) 无拆分 → 全记 prompt
    assert d["summary"]["total_tokens"] == 1100 + 2300 + 550 + 5000 + 4000 + 1000
    assert d["summary"]["prompt_tokens"] == 1000 + 2000 + 500 + 2000 + 4000 + 800
    assert d["summary"]["completion_tokens"] == 100 + 300 + 50 + 3000 + 200

    # ---- 对账：总成本 = 各模型按 (prompt, completion) 折算之和 ----
    expected_chat = cost_cny(CHAT_MODEL, 1000 + 2000 + 500 + 4000 + 800, 100 + 300 + 50 + 200)
    expected_reasoner = cost_cny(REASONER_MODEL, 2000, 3000)
    assert d["summary"]["total_cost_cny"] == round(expected_chat + expected_reasoner, 2)

    # ---- 按模型拆分 ----
    by_model = {m["model"]: m for m in d["by_model"]}
    assert len(by_model) == 2
    assert by_model[CHAT_MODEL]["tokens"] == 1100 + 2300 + 550 + 4000 + 1000
    assert by_model[CHAT_MODEL]["calls"] == 5
    assert by_model[REASONER_MODEL]["tokens"] == 5000
    assert by_model[REASONER_MODEL]["calls"] == 1
    assert by_model[CHAT_MODEL]["cost_cny"] == round(expected_chat, 4)
    assert by_model[REASONER_MODEL]["cost_cny"] == round(expected_reasoner, 4)

    # ---- 缓存：命中 3 / 7，节省 = 搜索命中 1 次 × 博查单价 ----
    assert d["cache"]["cache_hits"] == 3
    assert d["cache"]["total_calls"] == 7
    assert d["cache"]["hit_rate"] == round(3 / 7 * 100, 2)
    assert d["cache"]["saved_cny"] == round(1 * get_settings().bocha_price_per_call, 2)

    # ---- 任务与档位均值 ----
    assert d["summary"]["task_count"] == 2
    assert d["avg_by_depth"]["std"]["avg_cost_cny"] == 0.5
    assert d["avg_by_depth"]["quick"]["avg_cost_cny"] == 0.2
    assert [t["question"] for t in d["tasks"]] == ["研究二", "研究一"]

    # ---- 单日趋势（含按模型的日成本拆分） ----
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    point = next(p for p in d["daily"] if p["date"] == today)
    assert point["tokens"] == 1100 + 2300 + 550 + 5000 + 4000 + 1000
    assert point["cost_cny"] == round(expected_chat + expected_reasoner, 2)
    assert point["by_model"][CHAT_MODEL] == round(expected_chat, 4)
    assert point["by_model"][REASONER_MODEL] == round(expected_reasoner, 4)

    # ---- 计价口径注明 ----
    assert CHAT_MODEL in d["pricing_note"]


async def test_stats_days_filter_excludes_old_tasks(client):
    c, maker = client
    old = datetime.now(UTC) - timedelta(days=8)
    t_old = await _add_task(
        maker, question="旧任务", depth="std", status=TaskStatus.done, cost=9.0, created_at=old
    )
    await _add_event(
        maker,
        t_old,
        1,
        EventType.plan,
        {"model": CHAT_MODEL, "prompt_tokens": 100, "completion_tokens": 10},
        tokens=110,
        created_at=old,
    )
    t_new = await _add_task(maker, question="新任务", depth="std", status=TaskStatus.done, cost=0.1)
    await _add_event(
        maker,
        t_new,
        1,
        EventType.plan,
        {"model": CHAT_MODEL, "prompt_tokens": 200, "completion_tokens": 20},
        tokens=220,
    )

    resp = await c.get("/api/stats?days=7")
    d = resp.json()
    assert d["summary"]["task_count"] == 1
    assert d["summary"]["total_tokens"] == 220
    assert [t["question"] for t in d["tasks"]] == ["新任务"]

    resp_all = await c.get("/api/stats?days=0")
    assert resp_all.json()["summary"]["total_tokens"] == 330


async def test_stats_empty_shows_null_not_zero(client):
    c, _ = client
    resp = await c.get("/api/stats?days=7")
    assert resp.status_code == 200
    d = resp.json()
    assert d["summary"]["total_cost_cny"] is None
    assert d["summary"]["total_tokens"] is None
    assert d["cache"]["hit_rate"] is None
    assert d["cache"]["saved_cny"] is None
    assert d["by_model"] == []
    assert d["avg_by_depth"] == {}
    assert PRICE_PER_M  # 计价表存在，口径注记非空
    assert d["pricing_note"]
