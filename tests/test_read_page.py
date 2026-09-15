import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.tools.read_page as rp
from app.db import AgentEvent, Base, EventType, ResearchTask, TaskStatus
from app.services.event_recorder import EventRecorder
from app.tools.read_page import (
    ReadPageError,
    degradation_events,
    read_page,
)

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"


class FakeCache:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _fake_tier(payload: dict):
    async def fetch(url, client):
        return payload

    return fetch


async def test_chain_first_tier_success():
    cache = FakeCache()

    async def jina(url, client):
        return {"title": "Jina 标题", "text": "x" * 300}

    orig = rp._jina_fetch
    rp._jina_fetch = jina
    try:
        page = await read_page("https://a.com/doc", cache=cache)
    finally:
        rp._jina_fetch = orig

    assert page.provider == "jina"
    assert page.degradations == []
    assert len(page.text) == 300
    assert len(cache.store) == 1


async def test_degrade_jina_to_crawl4ai(monkeypatch):
    async def jina_fail(url, client):
        raise RuntimeError("jina failed after 2 retries")

    async def crawl_ok(url, client):
        return {"title": None, "text": "y" * 250}

    monkeypatch.setattr(rp, "_jina_fetch", jina_fail)
    monkeypatch.setattr(rp, "_crawl4ai_fetch", crawl_ok)

    page = await read_page("https://b.com/js-page", cache=FakeCache())

    assert page.provider == "crawl4ai"
    assert len(page.degradations) == 1
    d = page.degradations[0]
    assert d["tier"] == "jina"
    assert d["fell_to"] == "crawl4ai"
    assert "jina failed" in d["error"]

    events = degradation_events(page)
    assert events == [{"from": "jina", "to": "crawl4ai", "error": d["error"]}]


async def test_full_chain_fallback_to_trafilatura(monkeypatch):
    async def fail(url, client):
        raise RuntimeError("boom")

    async def traf_ok(url, client):
        return {"title": "T", "text": "z" * 400, "published_date": "2025-06-01"}

    monkeypatch.setattr(rp, "_jina_fetch", fail)
    monkeypatch.setattr(rp, "_crawl4ai_fetch", fail)
    monkeypatch.setattr(rp, "_trafilatura_fetch", traf_ok)

    page = await read_page("https://c.com/article", cache=FakeCache())

    assert page.provider == "trafilatura"
    assert page.published_date == "2025-06-01"
    assert [d["tier"] for d in page.degradations] == ["jina", "crawl4ai"]
    assert page.degradations[-1]["fell_to"] == "trafilatura"


async def test_all_tiers_failed_raises(monkeypatch):
    async def fail(url, client):
        raise RuntimeError("boom")

    monkeypatch.setattr(rp, "_jina_fetch", fail)
    monkeypatch.setattr(rp, "_crawl4ai_fetch", fail)
    monkeypatch.setattr(rp, "_trafilatura_fetch", fail)

    cache = FakeCache()
    with pytest.raises(ReadPageError) as exc_info:
        await read_page("https://d.com/gone", cache=cache)

    assert len(exc_info.value.degradations) == 3
    assert exc_info.value.degradations[-1]["fell_to"] is None
    assert len(cache.store) == 0


async def test_short_content_treated_as_failure(monkeypatch):
    async def jina_short(url, client):
        return {"title": None, "text": "too short"}

    monkeypatch.setattr(rp, "_jina_fetch", jina_short)

    async def crawl_ok(url, client):
        return {"title": None, "text": "w" * 220}

    monkeypatch.setattr(rp, "_crawl4ai_fetch", crawl_ok)

    page = await read_page("https://e.com/thin", cache=FakeCache())
    assert page.provider == "crawl4ai"
    assert (
        "too short" in page.degradations[0]["error"]
        or "content too short" in page.degradations[0]["error"]
    )


async def test_max_chars_truncation(monkeypatch):
    async def jina(url, client):
        return {"title": None, "text": "a" * 5000}

    monkeypatch.setattr(rp, "_jina_fetch", jina)

    page = await read_page("https://f.com/long", max_chars=1000, cache=FakeCache())
    assert len(page.text) == 1000


async def test_cache_hit_skips_network():
    cache = FakeCache()
    key = rp._cache_key("https://g.com/cached")
    cache.store[key] = json.dumps({"title": "缓存页", "text": "c" * 300, "published_date": None})

    async def fail(url, client):
        raise AssertionError("cache hit must not reach network")

    orig_jina = rp._jina_fetch
    rp._jina_fetch = fail
    try:
        page = await read_page("https://g.com/cached", cache=cache)
    finally:
        rp._jina_fetch = orig_jina

    assert page.provider == "cache"
    assert page.title == "缓存页"
    assert page.degradations == []


async def test_force_tier_bypasses_cache(monkeypatch):
    cache = FakeCache()
    key = rp._cache_key("https://h.com/x")
    cache.store[key] = json.dumps({"title": "旧", "text": "o" * 300})

    async def traf(url, client):
        return {"title": "新", "text": "n" * 300}

    monkeypatch.setattr(rp, "_trafilatura_fetch", traf)

    page = await read_page("https://h.com/x", force_tier="trafilatura", cache=cache)
    assert page.provider == "trafilatura"
    assert page.title == "新"


async def test_jina_response_parsing():
    raw = (
        "Title: 深度研究平台设计\n"
        "URL Source: https://example.com/doc\n"
        "Markdown Content:\n"
        "\n"
        "# 深度研究平台设计\n\n正文内容第一段。\n\n正文内容第二段。\n"
    )
    title, body = rp._parse_jina(raw)
    assert title == "深度研究平台设计"
    assert body.startswith("# 深度研究平台设计")
    assert "第二段" in body

    title2, body2 = rp._parse_jina("没有头的纯 Markdown 正文")
    assert title2 is None
    assert body2 == "没有头的纯 Markdown 正文"


async def test_jina_fetch_via_mock_transport(monkeypatch):
    monkeypatch.setattr(rp, "JINA_BACKOFF", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "r.jina.ai"
        return httpx.Response(
            200,
            text=(
                "Title: 测试页\nURL Source: https://example.com\n"
                "Markdown Content:\n\n" + "内容" * 150
            ),
        )

    client = _client(handler)
    result = await rp._jina_fetch("https://example.com", client)
    assert result["title"] == "测试页"
    assert len(result["text"]) > 200


async def test_trafilatura_extract_real_html():
    html = (
        """
    <html><head><title>测试文章</title></head><body>
    <article>
      <h1>测试文章</h1>
      <p>"""
        + "这是一段用于抽取验证的正文内容。" * 20
        + """</p>
    </article>
    <nav>导航 导航 导航</nav>
    </body></html>
    """
    )
    result = rp._trafilatura_extract(html)
    assert "正文内容" in result["text"]
    assert len(result["text"]) > 100


async def test_degrade_events_persisted_to_db(monkeypatch):
    """降级切换事件落库：read_page 的降级轨迹经 EventRecorder 写入 agent_events。"""

    async def jina_fail(url, client):
        raise RuntimeError("jina failed after 2 retries")

    async def crawl_fail(url, client):
        raise RuntimeError("crawl4ai: timeout")

    async def traf_ok(url, client):
        return {"title": "兜底成功", "text": "t" * 260}

    monkeypatch.setattr(rp, "_jina_fetch", jina_fail)
    monkeypatch.setattr(rp, "_crawl4ai_fetch", crawl_fail)
    monkeypatch.setattr(rp, "_trafilatura_fetch", traf_ok)

    page = await read_page("https://persist.com/doc", cache=FakeCache())
    assert page.provider == "trafilatura"
    assert len(page.degradations) == 2

    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            task = ResearchTask(
                question="降级落库测试", status=TaskStatus.running, token_budget=80000
            )
            session.add(task)
            await session.commit()
            task_id = task.id

        async with maker() as session:
            recorder = EventRecorder(session)
            for payload in degradation_events(page):
                await recorder.record(task_id, EventType.degrade, payload)

        async with maker() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentEvent)
                        .where(AgentEvent.task_id == task_id)
                        .order_by(AgentEvent.seq)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 2
        assert rows[0].type == EventType.degrade
        assert rows[0].payload["from"] == "jina"
        assert rows[0].payload["to"] == "crawl4ai"
        assert rows[1].payload["from"] == "crawl4ai"
        assert rows[1].payload["to"] == "trafilatura"
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
