import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, ResearchTask, TaskStatus
from app.db.base import get_session
from app.main import app

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"


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

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_create_and_list_tasks(client: AsyncClient):
    resp = await client.post(
        "/api/research/tasks",
        json={"question": "2025 年国产大模型在医疗行业的落地情况", "depth": "std"},
    )
    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "queued"
    assert task["token_budget"] == 80_000

    resp = await client.post("/api/research/tasks", json={"question": "深", "depth": "std"})
    assert resp.status_code == 422

    resp = await client.get("/api/research/tasks")
    assert resp.status_code == 200
    assert any(t["id"] == task["id"] for t in resp.json())


async def test_get_task_detail(client: AsyncClient):
    resp = await client.post("/api/research/tasks", json={"question": "固态电池产业化进展"})
    task_id = resp.json()["id"]

    resp = await client.get(f"/api/research/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["sub_tasks"] == []

    resp = await client.get("/api/research/tasks/99999")
    assert resp.status_code == 404


async def test_control_transitions(client: AsyncClient):
    maker = async_sessionmaker(create_async_engine(TEST_DB_URL), expire_on_commit=False)
    async with maker() as session:
        task = ResearchTask(question="状态机测试", status=TaskStatus.running, token_budget=80000)
        session.add(task)
        await session.commit()
        task_id = task.id

    resp = await client.post(f"/api/research/tasks/{task_id}/control", json={"action": "pause"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"

    resp = await client.post(f"/api/research/tasks/{task_id}/control", json={"action": "resume"})
    assert resp.json()["status"] == "running"

    resp = await client.post(f"/api/research/tasks/{task_id}/control", json={"action": "pause"})
    assert resp.status_code == 200

    resp = await client.post(f"/api/research/tasks/{task_id}/control", json={"action": "stop"})
    assert resp.json()["status"] == "stopped"

    resp = await client.post(f"/api/research/tasks/{task_id}/control", json={"action": "resume"})
    assert resp.status_code == 409
