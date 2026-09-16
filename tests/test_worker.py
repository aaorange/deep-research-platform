"""arq worker 与编排器测试：状态回写（queued→running→done/failed）与异常隔离。

execute_research 的 LLM/工具依赖全部 monkeypatch 假实现，数据落 research_test 库。
"""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import (
    AgentEvent,
    Base,
    EventType,
    Note,
    Report,
    ResearchTask,
    SubTask,
    SubTaskStatus,
    TaskStatus,
)
from app.engine import orchestrator
from app.engine.persister import SubTaskPersister
from app.engine.planner import PlanOutline, PlanSubTask
from app.engine.reflector import Reflection, SupplementaryTask
from app.worker import run_research_task

TEST_DB_URL = "postgresql+asyncpg://research:research@localhost:5432/research_test"

PLAN_USAGE = SimpleNamespace(prompt_tokens=500, completion_tokens=80, total_tokens=580)
REFLECT_USAGE = SimpleNamespace(prompt_tokens=200, completion_tokens=50, total_tokens=250)
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


async def _make_task(maker, status=TaskStatus.queued, token_budget=80000) -> int:
    async with maker() as session:
        task = ResearchTask(
            question="固态电池产业化进展", status=status, token_budget=token_budget, depth="std"
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
            SimpleNamespace(markdown=markdown, citation_map={1: 1}, n_citations=1, chart_specs=[]),
            REPORT_USAGE,
        )

    return write_report


def make_fake_worker(maker, fail_all=False, crash_ids=frozenset(), usage=None):
    """模拟 runner：子任务状态回写 + 笔记/信源落测试库。

    crash_ids 模拟进程被 kill：置 running 后直接抛异常，状态残留 running；
    usage=(prompt, completion) 模拟笔记 LLM 计费（预算降级 e2e 用）。
    """

    async def run_worker(task_id, sub_task, max_pages):
        async with maker() as session:
            p = SubTaskPersister(session)
            await p.mark_sub_task_status(sub_task["id"], SubTaskStatus.running)
            if sub_task["id"] in crash_ids:
                raise RuntimeError(f"worker killed: {sub_task['title']}")
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
            if usage:
                await p.add_usage(task_id, "deepseek-chat", usage[0], usage[1])
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


def fake_reflect(has_gaps=False):
    async def reflect(question, background, sub_tasks, notes, sources):
        gaps = (
            [SupplementaryTask(title="补搜缺数据维度", keywords="补搜 关键数据")]
            if has_gaps and notes
            else []
        )
        return (
            Reflection(has_gaps=bool(gaps), assessment="缺数据" if gaps else "材料充分", gaps=gaps),
            REFLECT_USAGE,
        )

    return reflect


def patch_all(monkeypatch, maker, fail_all=False, reflect=None, crash_ids=frozenset(), usage=None):
    outline = PlanOutline(
        sub_tasks=[PlanSubTask(title=f"子问题{i}", keywords=f"k{i}") for i in range(1, 4)]
    )
    monkeypatch.setattr(orchestrator, "write_plan", fake_plan(outline))
    monkeypatch.setattr(
        orchestrator,
        "make_sub_task_runner",
        lambda verbose=False, run_token=None: make_fake_worker(maker, fail_all, crash_ids, usage),
    )
    monkeypatch.setattr(orchestrator, "write_report", fake_report())
    monkeypatch.setattr(orchestrator, "reflect_on_coverage", reflect or fake_reflect())
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
    assert result["reflect_rounds"] == 1
    assert result["supplemented"] == 0
    assert result["report_id"] == 1
    assert result["citations"] == 1
    # plan + reflect + reasoner 三笔计费
    assert result["token_used"] == 580 + 250 + 2300

    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        assert task.status == TaskStatus.done
        assert task.thread_id == f"task-{task_id}"
        assert task.finished_at is not None
        report = await session.get(Report, result["report_id"])
        assert report.markdown == "# 报告\n\n结论 [1]。"
        assert report.citation_map == {"1": 1}
        assert report.token_total == 2300
        sub_tasks = list(await session.scalars(select(SubTask).where(SubTask.task_id == task_id)))
        assert all(st.round_no == 1 for st in sub_tasks)
        assert all(st.status == SubTaskStatus.done for st in sub_tasks)


async def test_execute_research_consumes_instruction(session_maker, monkeypatch):
    """追加指示全链路：信箱 → reflect 消费 → round 2 子任务 → 笔记入综合。"""
    task_id = await _make_task(session_maker)
    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        task.extra_instructions = [
            {"id": 1, "text": "重点补充 2025 年融资数据", "created_at": "t", "consumed_round": None}
        ]
        await session.commit()

    patch_all(monkeypatch, session_maker)

    result = await orchestrator.execute_research(task_id, session_maker=session_maker)

    assert result["status"] == "done"
    assert result["supplemented"] == 1
    assert result["reflect_rounds"] == 2
    assert result["notes"] == 4  # 首轮 3 + 指示补搜 1
    assert result["report_id"] == 1

    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        assert task.extra_instructions[0]["consumed_round"] == 2
        rows = list(
            await session.scalars(
                select(SubTask).where(SubTask.task_id == task_id).order_by(SubTask.id)
            )
        )
        assert [st.round_no for st in rows] == [1, 1, 1, 2]
        assert rows[3].title == "重点补充 2025 年融资数据"
        assert rows[3].status == SubTaskStatus.done
        # 反思事件两轮入 agent_events（动作流可见，JSONB 读出即 dict）
        events = list(
            await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.task_id == task_id, AgentEvent.type == EventType.reflect)
                .order_by(AgentEvent.seq)
            )
        )
        assert [e.payload["round"] for e in events] == [1, 2]
        consumed = events[0].payload
        assert consumed["instructions"] == 1
        assert consumed["next_round"] == 2


async def test_execute_research_resume_after_crash(session_maker, monkeypatch):
    """断点续跑 e2e：worker 中途被 kill（子任务残留 running）→ resume 续跑，
    已完成子任务不重跑、笔记不重复、plan 不重复计费。"""
    task_id = await _make_task(session_maker)
    patch_all(monkeypatch, session_maker, crash_ids={2, 3})

    # 第一轮：子任务 1 完成，2/3 崩溃 → gather 异常 → 任务 failed
    with pytest.raises(RuntimeError, match="worker killed"):
        await orchestrator.execute_research(task_id, session_maker=session_maker)

    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        assert task.status == TaskStatus.failed
        assert task.token_used == 580  # 只有 plan 计费

    # resume：正常 worker 续跑（模拟重新入队后的第二次执行）
    patch_all(monkeypatch, session_maker)
    result = await orchestrator.execute_research(task_id, session_maker=session_maker)

    assert result["status"] == "done"
    assert result["notes"] == 3

    async with session_maker() as session:
        sub_tasks = list(
            await session.scalars(
                select(SubTask).where(SubTask.task_id == task_id).order_by(SubTask.id)
            )
        )
        assert len(sub_tasks) == 3  # plan 幂等：无重复子任务
        assert all(st.status == SubTaskStatus.done for st in sub_tasks)
        notes = list(await session.scalars(select(Note).where(Note.task_id == task_id)))
        assert len(notes) == 3  # 子任务 1 的笔记不重复
        task = await session.get(ResearchTask, task_id)
        # plan 只计费一次（resume 跳过重规划）：580 + reflect 250 + report 2300
        assert task.token_used == 580 + 250 + 2300


async def test_execute_research_skips_paused_task(session_maker, monkeypatch):
    """排队真空期暂停：job 取到任务时状态已 paused，直接退出不置 running、不跑图。"""
    task_id = await _make_task(session_maker)
    async with session_maker() as session:
        await session.execute(
            update(ResearchTask).where(ResearchTask.id == task_id).values(status=TaskStatus.paused)
        )
        await session.commit()

    patch_all(monkeypatch, session_maker)
    result = await orchestrator.execute_research(task_id, session_maker=session_maker)

    assert result["status"] == "paused"
    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        assert task.status == TaskStatus.paused  # 未被 job 覆盖为 running
        events = list(
            await session.scalars(select(AgentEvent).where(AgentEvent.task_id == task_id))
        )
        assert events == []  # 图未执行、零事件
        assert task.token_used == 0


async def test_execute_research_budget_degrades(session_maker, monkeypatch):
    """低压预算 e2e：execute 后消耗 80%+ → reflect 跳过 → 报告尾部预算声明。"""
    task_id = await _make_task(session_maker, token_budget=2000)
    patch_all(monkeypatch, session_maker, usage=(500, 100))  # 3×600 + plan 580 = 2380

    async def no_reflect(*args, **kwargs):
        raise AssertionError("degraded task must skip reflect")

    monkeypatch.setattr(orchestrator, "reflect_on_coverage", no_reflect)

    result = await orchestrator.execute_research(task_id, session_maker=session_maker)

    assert result["status"] == "done"
    assert result["budget_degraded"] is True
    assert result["reflect_rounds"] == 0
    assert result["supplemented"] == 0

    async with session_maker() as session:
        report = await session.get(Report, result["report_id"])
        assert "预算受限说明" in report.markdown
        assert report.markdown.endswith("覆盖度可能受限。\n")
        events = list(
            await session.scalars(
                select(AgentEvent).where(
                    AgentEvent.task_id == task_id, AgentEvent.type == EventType.degrade
                )
            )
        )
        assert [e.payload["stage"] for e in events] == ["reflect"]
        budget_events = list(
            await session.scalars(
                select(AgentEvent).where(
                    AgentEvent.task_id == task_id, AgentEvent.type == EventType.budget
                )
            )
        )
        assert [e.payload["stage"] for e in budget_events] == [
            "plan",
            "execute",
            "reflect",
            "synthesize",
        ]


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


def make_interrupting_worker(maker, mode):
    """模拟执行中用户干预：pause=置任务 paused；steal=改写 run_token 模拟 resume 新 job 接管。"""
    ran = []

    async def run_worker(task_id, sub_task, max_pages):
        ran.append(sub_task["id"])
        async with maker() as session:
            p = SubTaskPersister(session)
            await p.mark_sub_task_status(sub_task["id"], SubTaskStatus.done)
            if sub_task["id"] == 1:
                values = (
                    {"status": TaskStatus.paused}
                    if mode == "pause"
                    else {"run_token": "new-job-token"}
                )
                await session.execute(
                    update(ResearchTask).where(ResearchTask.id == task_id).values(**values)
                )
                await session.commit()
        return {
            "sub_task_id": sub_task["id"],
            "title": sub_task["title"],
            "note": {"summary": "n"},
            "sources": [],
            "note_tokens": 0,
            "error": None,
        }

    run_worker.ran = ran
    return run_worker


def patch_interrupting(monkeypatch, maker, mode):
    outline = PlanOutline(
        sub_tasks=[PlanSubTask(title=f"子问题{i}", keywords=f"k{i}") for i in range(1, 4)]
    )
    monkeypatch.setattr(orchestrator, "write_plan", fake_plan(outline))
    monkeypatch.setattr(
        orchestrator,
        "make_sub_task_runner",
        lambda verbose=False, run_token=None: make_interrupting_worker(maker, mode),
    )
    monkeypatch.setattr(orchestrator, "write_report", fake_report())
    monkeypatch.setattr(orchestrator, "reflect_on_coverage", fake_reflect())
    monkeypatch.setattr(
        orchestrator, "open_graph_checkpointer", lambda: nullcontext(InMemorySaver())
    )
    return make_interrupting_worker(maker, mode)


async def test_execute_research_superseded_on_pause(session_maker, monkeypatch):
    """飞行中暂停 e2e：旧 job 围栏退出（不误标 failed），任务保持 paused 等待 resume。"""
    task_id = await _make_task(session_maker)
    patch_interrupting(monkeypatch, session_maker, "pause")

    result = await orchestrator.execute_research(task_id, session_maker=session_maker)

    assert result == {"task_id": task_id, "status": "superseded"}
    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        assert task.status == TaskStatus.paused  # 旧 job 未覆盖为 failed/done
        assert task.error_msg is None
        assert task.finished_at is None
        reports = list(await session.scalars(select(Report).where(Report.task_id == task_id)))
        assert reports == []  # 旧 job 未产出报告（新 job resume 后幂等补齐）


async def test_execute_research_superseded_by_new_job(session_maker, monkeypatch):
    """resume 抢占 e2e：令牌被新 job 改写后，旧 job 退出且不把任务误标终态。"""
    task_id = await _make_task(session_maker)
    patch_interrupting(monkeypatch, session_maker, "steal")

    result = await orchestrator.execute_research(task_id, session_maker=session_maker)

    assert result == {"task_id": task_id, "status": "superseded"}
    async with session_maker() as session:
        task = await session.get(ResearchTask, task_id)
        assert task.status == TaskStatus.running  # 归新 job 所有
        assert task.run_token == "new-job-token"
        assert task.error_msg is None  # 未被旧 job 的 finish_task 抹成 failed
        assert task.finished_at is None


async def test_persist_report_idempotent_on_duplicate(session_maker):
    """reports.task_id 唯一约束兜底：同任务二次落报告返回既有 ID，不抛 IntegrityError。"""
    task_id = await _make_task(session_maker)
    async with session_maker() as session:
        p = SubTaskPersister(session)
        rid1 = await p.persist_report(task_id, "# v1", {1: 101}, 100)
        rid2 = await p.persist_report(task_id, "# v2", {1: 102}, 200)

    assert rid1 == rid2
    async with session_maker() as session:
        rows = list(await session.scalars(select(Report).where(Report.task_id == task_id)))
        assert len(rows) == 1
        assert rows[0].markdown == "# v1"  # 既有报告保留


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
