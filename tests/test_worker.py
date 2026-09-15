"""arq worker 与编排器测试：状态回写（queued→running→done/failed）与异常隔离。

execute_research 的 LLM/工具依赖全部 monkeypatch 假实现，数据落 research_test 库。
"""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, Report, ResearchTask, SubTaskStatus, TaskStatus
from app.engine import orchestrator
from app.engine.persister import SubTaskPersister
from app.engine.planner import PlanOutline, PlanSubTask
from app.worker import run_research_task

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"

PLAN_USAGE = SimpleNamespace(prompt_tokens=500, completion_tokens=80, total_tokens=580)
REPORT_USAGE = SimpleNamespace(prompt_tokens=800, completion_tokens=1500, total_tokens=2300)


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


async def _make_task(maker, status=TaskStatus.queued) -> int:
    async with maker() as session:
        task = ResearchTask(
            question="固态电池产业化进展", status=status, token_budget=80000, depth="std"
        )
        session.add(task)
        await session.commit()
        return task.id


def fake_plan(outline):
    async def write_plan(question, background, depth):
        return outline, PLAN_USAGE

    return write_plan


def fake_report(markdown="# 报告\n\n结论 [1]。"):
    async def write_report(question, background, notes, sources):
        return (
            SimpleNamespace(markdown=markdown, citation_map={1: 1}, n_citations=1),
            REPORT_USAGE,
        )

    return write_report


def make_fake_worker(maker, fail_all=False):
    """模拟 runner：子任务状态回写 + 笔记/信源落测试库。"""

    async def run_worker(task_id, sub_task, max_pages):
        async with maker() as session:
            p = SubTaskPersister(session)
            if fail_all:
                await p.mark_sub_task_status(sub_task["id"], SubTaskStatus.failed, "boom")
                return {
                    "sub_task_id": sub_task["id"],
                    "title": sub_task["title"],
                    "note": None,
                    "sources": [],
                    "note_tokens": 0,
                    "error": "boom",
                }
            await p.mark_sub_task_status(sub_task["id"], SubTaskStatus.done)
            idx_to_id = await p.persist_sources(
                task_id,
                [
                    {
                        "idx": 1,
                        "url": f"https://t.com/{sub_task['id']}",
                        "title": "材料",
                        "domain": "t.com",
                        "credibility": 3,
                        "freshness": 0.5,
                        "content_hash": f"h{sub_task['id']}",
                    }
                ],
            )
            await p.persist_note(
                task_id, sub_task["id"], {"summary": f"{sub_task['title']} 的发现 [1]"}, idx_to_id
            )
            return {
                "sub_task_id": sub_task["id"],
                "title": sub_task["title"],
                "note": {"summary": "n"},
                "sources": [
                    {
                        "idx": 1,
                        "url": f"https://t.com/{sub_task['id']}",
                        "title": "材料",
                        "domain": "t.com",
                        "credibility": 3,
                        "freshness": 0.5,
                        "content_hash": f"h{sub_task['id']}",
                    }
                ],
                "note_tokens": 800,
                "error": None,
            }

    return run_worker


def patch_all(monkeypatch, maker, fail_all=False):
    outline = PlanOutline(
        sub_tasks=[PlanSubTask(title=f"子问题{i}", keywords=f"k{i}") for i in range(1, 4)]
    )
    monkeypatch.setattr(orchestrator, "write_plan", fake_plan(outline))
    monkeypatch.setattr(
        orchestrator,
        "make_sub_task_runner",
        lambda verbose=False: make_fake_worker(maker, fail_all),
    )
    monkeypatch.setattr(orchestrator, "write_report", fake_report())
    monkeypatch.setattr(
        orchestrator, "open_graph_checkpointer", lambda: nullcontext(InMemorySaver())
    )


async def test_execute_research_full_lifecycle(session_maker, monkeypatch):
    task_id = await _make_task(session_maker)
    patch_all(monkeypatch, session_maker)

    result = await orchestrator.execute_research(task_id, session_maker=session_maker)

    assert result["status"] == "done"
    assert result["executed"] == 3
    assert result["notes"] == 3
    assert result["sources"] == 3
    assert result["report_id"] == 1
    assert result["citations"] == 1
    # plan + reasoner 两笔计费
    assert result["token_used"] == 580 + 2300

    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        assert task.status == TaskStatus.done
        assert task.thread_id == f"task-{task_id}"
        assert task.finished_at is not None
        report = await session.get(Report, result["report_id"])
        assert report.markdown == "# 报告\n\n结论 [1]。"
        assert report.citation_map == {"1": 1}
        assert report.token_total == 2300


async def test_execute_research_marks_failed_when_no_notes(session_maker, monkeypatch):
    task_id = await _make_task(session_maker)
    patch_all(monkeypatch, session_maker, fail_all=True)

    result = await orchestrator.execute_research(task_id, session_maker=session_maker)

    assert result["status"] == "failed"
    assert result["report_id"] is None

    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        assert task.status == TaskStatus.failed
        assert task.error_msg == "no notes or report produced"


async def test_execute_research_marks_failed_on_exception(session_maker, monkeypatch):
    """synthesize 无兜底：reasoner 失败 → 任务 failed + error_msg，异常向上抛。"""
    task_id = await _make_task(session_maker)
    patch_all(monkeypatch, session_maker)

    async def boom(question, background, notes, sources):
        raise RuntimeError("reasoner 503")

    monkeypatch.setattr(orchestrator, "write_report", boom)

    with pytest.raises(RuntimeError, match="reasoner 503"):
        await orchestrator.execute_research(task_id, session_maker=session_maker)

    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        assert task.status == TaskStatus.failed
        assert "reasoner 503" in task.error_msg


async def test_execute_research_rejects_terminal_status(session_maker):
    task_id = await _make_task(session_maker, status=TaskStatus.done)

    with pytest.raises(ValueError, match="terminal status"):
        await orchestrator.execute_research(task_id, session_maker=session_maker)


async def test_execute_research_missing_task(session_maker):
    with pytest.raises(ValueError, match="not found"):
        await orchestrator.execute_research(99999, session_maker=session_maker)


async def test_arq_job_wraps_success(monkeypatch):
    async def ok(task_id):
        return {"task_id": task_id, "status": "done"}

    monkeypatch.setattr("app.worker.execute_research", ok)
    result = await run_research_task({}, 42)
    assert result == {"task_id": 42, "status": "done"}


async def test_arq_job_swallows_failure(monkeypatch):
    """worker 不让异常炸掉任务循环；状态已由 execute_research 落库。"""

    async def boom(task_id):
        raise RuntimeError("crash")

    monkeypatch.setattr("app.worker.execute_research", boom)
    result = await run_research_task({}, 42)
    assert result == {"task_id": 42, "ok": False}


def test_worker_settings_register_job():
    from app.worker import WorkerSettings

    assert [f.__name__ for f in WorkerSettings.functions] == ["run_research_task"]
