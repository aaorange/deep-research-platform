"""D12 SSE 事件流测试：JSON 序号回放 + SSE 回放/实时/心跳/终态关闭 + Last-Event-ID 重连。

说明：httpx ASGITransport 会整包缓冲流式响应，无法验证增量语义，
因此 SSE 用例直接迭代 StreamingResponse.body_iterator（HTTP 层
另经终态任务验证 content-type / 缓存头），真实 TCP 流由 smoke_sse 冒烟覆盖。
实时用例不依赖 Redis：event_bus 不可用时 SSE 退化为心跳周期 DB 轮询，
两条路径（pubsub 实时 / 轮询兜底）在本测试库下等价可断言。
"""

import asyncio
import contextlib
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.tasks import stream_events
from app.config import get_settings
from app.db import Base, EventType, ResearchTask, TaskStatus
from app.db.base import get_session
from app.main import app
from app.queue import get_queue
from app.services.event_recorder import EventRecorder

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"


class FakeQueue:
    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args))


class FakeRequest:
    """模拟 starlette Request 的 headers.get（大小写不敏感）。"""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


@pytest.fixture
async def env():
    engine = create_async_engine(TEST_DB_URL)
    await _terminate_stale_connections(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with maker() as session:
            yield session

    fake_queue = FakeQueue()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_queue] = lambda: fake_queue
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, maker
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _terminate_stale_connections(engine):
    """仅 setup 阶段使用：清掉上次运行残留的连接（本引擎此时池为空，不会误伤）。"""
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'research_test' AND pid <> pg_backend_pid()"
            )
    except Exception:
        pass


async def _record(maker, task_id: int, n_events: int, marker: str | None = None):
    async with maker() as session:
        rec = EventRecorder(session)
        for i in range(n_events):
            await rec.record(
                task_id,
                EventType.control,
                {"stage": f"s{i}", "marker": marker} if marker else {"stage": f"s{i}"},
            )


async def _set_status(maker, task_id: int, status: TaskStatus):
    async with maker() as session:
        task = await session.get(ResearchTask, task_id)
        task.status = status
        await session.commit()


def _seqs(lines: list[str]) -> list[int]:
    out = []
    for ln in lines:
        if ln.startswith("data: "):
            body = json.loads(ln[6:])
            if "seq" in body:
                out.append(body["seq"])
    return out


async def _open_stream(maker, task_id: int, after_seq=None, headers=None):
    """直连调用 SSE 端点，返回（response, session）。调用方负责 _close_stream。"""
    session = maker()
    resp = await stream_events(task_id, FakeRequest(headers), after_seq=after_seq, session=session)
    return resp, session


async def _close_stream(resp, session):
    with contextlib.suppress(Exception):
        await resp.body_iterator.aclose()
    await session.close()


async def _collect(resp, target=None, timeout=5.0, until=None) -> list[str]:
    """迭代 SSE 体，直到 target 条含 seq 的 data 行 / until(line) 为真；结束后关闭迭代器。"""
    lines: list[str] = []
    seen = 0
    gen = resp.body_iterator
    try:
        async with asyncio.timeout(timeout):
            async for chunk in gen:
                for line in chunk.splitlines():
                    lines.append(line)
                    if until is not None and until(line):
                        return lines
                    if line.startswith("data: ") and '"seq"' in line:
                        seen += 1
                        if target is not None and seen >= target:
                            return lines
    finally:
        await gen.aclose()
    return lines


async def test_list_events_json_replay(env):
    c, maker = env
    task_id = (await c.post("/api/research/tasks", json={"question": "回放测试"})).json()["id"]
    await _record(maker, task_id, 3)

    resp = await c.get(f"/api/research/tasks/{task_id}/events")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["seq"] for e in body] == [1, 2, 3]
    assert body[0]["type"] == "control"

    resp = await c.get(f"/api/research/tasks/{task_id}/events", params={"after_seq": 2})
    assert [e["seq"] for e in resp.json()] == [3]

    resp = await c.get("/api/research/tasks/99999/events")
    assert resp.status_code == 404


async def test_sse_http_headers_for_terminal_task(env):
    """HTTP 层验证：终态任务流自然结束，200 + text/event-stream + 禁缓存头。"""
    c, maker = env
    task_id = (await c.post("/api/research/tasks", json={"question": "HTTP 头测试"})).json()["id"]
    await _record(maker, task_id, 1)
    await _set_status(maker, task_id, TaskStatus.done)

    resp = await c.get(f"/api/research/tasks/{task_id}/events/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert "event: end" in resp.text

    resp = await c.get("/api/research/tasks/99999/events/stream")
    assert resp.status_code == 404


async def test_sse_replay_then_end_for_terminal_task(env):
    c, maker = env
    task_id = (await c.post("/api/research/tasks", json={"question": "终态流测试"})).json()["id"]
    await _record(maker, task_id, 2)
    await _set_status(maker, task_id, TaskStatus.done)

    resp, session = await _open_stream(maker, task_id)
    try:
        lines = await _collect(resp, until=lambda ln: ln.startswith('data: {"status"'))
        assert _seqs(lines) == [1, 2]
        end_idx = lines.index("event: end")
        assert json.loads(lines[end_idx + 1][6:])["status"] == "done"
    finally:
        await _close_stream(resp, session)


async def test_sse_live_event_and_heartbeat(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "sse_heartbeat_s", 0.2)
    c, maker = env
    task_id = (await c.post("/api/research/tasks", json={"question": "实时流测试"})).json()["id"]

    resp, session = await _open_stream(maker, task_id)
    lines: list[str] = []

    async def consume():
        async for chunk in resp.body_iterator:
            lines.extend(chunk.splitlines())
            if any('"live-marker"' in ln for ln in lines):
                break

    try:
        reader = asyncio.create_task(consume())
        await asyncio.sleep(0.6)  # 等流建立（订阅/回放完成）
        await _record(maker, task_id, 1, marker="live-marker")
        await asyncio.wait_for(reader, timeout=5)

        assert _seqs(lines) == [1]
        assert any(ln == ": heartbeat" for ln in lines), "应有心跳注释行"
    finally:
        await _close_stream(resp, session)


async def test_sse_reconnect_last_event_id_incremental(env, monkeypatch):
    """验收核心：断开重连后按序号增量补发，界面不丢动作。"""
    monkeypatch.setattr(get_settings(), "sse_heartbeat_s", 0.2)
    c, maker = env
    task_id = (await c.post("/api/research/tasks", json={"question": "重连测试"})).json()["id"]
    await _record(maker, task_id, 2)

    async def read(after_seq=None, headers=None, target=None):
        resp, session = await _open_stream(maker, task_id, after_seq=after_seq, headers=headers)
        try:
            return await _collect(resp, target=target)
        finally:
            await _close_stream(resp, session)

    # 第一连接：收到 1..2 后断开
    assert _seqs(await read(target=2)) == [1, 2]

    # 断开期间新事件 3..4 落库
    await _record(maker, task_id, 2)

    # 重连一：Last-Event-ID 请求头（EventSource 自动重连语义）
    assert _seqs(await read(headers={"Last-Event-ID": "2"}, target=2)) == [3, 4]

    # 重连二：after_seq 查询参数（显式指定）
    assert _seqs(await read(after_seq=2, target=2)) == [3, 4]

    # 非法 Last-Event-ID 回退为 0：全量重放
    assert _seqs(await read(headers={"Last-Event-ID": "abc"}, target=4)) == [1, 2, 3, 4]
