import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, ResearchTask, TaskStatus
from app.db.base import get_session
from app.main import app
from app.queue import get_queue

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"


class FakeQueue:
    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args))
        return None


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    engine = create_async_engine(TEST_DB_URL)
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
        yield c, fake_queue
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_create_and_list_tasks(client: AsyncClient):
    c, queue = client
    resp = await c.post(
        "/api/research/tasks",
        json={"question": "2025 年国产大模型在医疗行业的落地情况", "depth": "std"},
    )
    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "queued"
    assert task["token_budget"] == 80_000
    # 创建即入队：worker 名与任务 id 传给 arq
    assert queue.jobs == [("run_research_task", (task["id"],))]

    resp = await c.post("/api/research/tasks", json={"question": "深", "depth": "std"})
    assert resp.status_code == 422

    resp = await c.get("/api/research/tasks")
    assert resp.status_code == 200
    assert any(t["id"] == task["id"] for t in resp.json())


async def test_get_task_detail(client: AsyncClient):
    c, _ = client
    resp = await c.post("/api/research/tasks", json={"question": "固态电池产业化进展"})
    task_id = resp.json()["id"]

    resp = await c.get(f"/api/research/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["sub_tasks"] == []

    resp = await c.get("/api/research/tasks/99999")
    assert resp.status_code == 404


async def test_control_transitions(client: AsyncClient):
    c, _ = client
    maker = async_sessionmaker(create_async_engine(TEST_DB_URL), expire_on_commit=False)
    async with maker() as session:
        task = ResearchTask(question="状态机测试", status=TaskStatus.running, token_budget=80000)
        session.add(task)
        await session.commit()
        task_id = task.id

    resp = await c.post(f"/api/research/tasks/{task_id}/control", json={"action": "pause"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"

    resp = await c.post(f"/api/research/tasks/{task_id}/control", json={"action": "resume"})
    assert resp.json()["status"] == "running"

    resp = await c.post(f"/api/research/tasks/{task_id}/control", json={"action": "pause"})
    assert resp.status_code == 200

    resp = await c.post(f"/api/research/tasks/{task_id}/control", json={"action": "stop"})
    assert resp.json()["status"] == "stopped"

    resp = await c.post(f"/api/research/tasks/{task_id}/control", json={"action": "resume"})
    assert resp.status_code == 409
