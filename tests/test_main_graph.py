import asyncio
import time
from types import SimpleNamespace

from app.config import get_settings
from app.db import EventType
from app.engine.main_graph import (
    OrchestratorDeps,
    build_main_graph,
    merge_worker_results,
)
from app.engine.planner import PlanOutline, PlanSubTask


class FakeRecorder:
    def __init__(self):
        self.events: list[dict] = []

    async def record(self, task_id, type, payload=None, **kwargs):
        self.events.append(
            {
                "task_id": task_id,
                "type": type,
                "payload": payload or {},
                **kwargs,
            }
        )


class FakeStore:
    def __init__(self):
        self.rows: list[dict] = []
        self.created: list[dict] = []
        self.usage_calls: list[tuple] = []
        self._next_id = 1

    async def create_sub_tasks(self, task_id, sub_tasks):
        out = []
        for s in sub_tasks:
            row = {
                "id": self._next_id,
                "task_id": task_id,
                "title": s["title"],
                "keywords": s.get("keywords"),
                "status": "pending",
            }
            self._next_id += 1
            self.rows.append(row)
            self.created.append(dict(row))
            out.append({"id": row["id"], "title": row["title"], "keywords": row["keywords"]})
        return out

    async def pending_sub_tasks(self, task_id):
        return [
            {"id": r["id"], "title": r["title"], "keywords": r["keywords"]}
            for r in self.rows
            if r["status"] in ("pending", "running")
        ]

    async def add_usage(self, task_id, model, prompt_tokens, completion_tokens):
        self.usage_calls.append((task_id, model, prompt_tokens, completion_tokens))


def make_plan(outline=None, error=None):
    calls = []

    async def write_plan(question, background, depth):
        calls.append((question, background, depth))
        if error:
            raise error
        return outline, SimpleNamespace(prompt_tokens=500, completion_tokens=80, total_tokens=580)

    write_plan.calls = calls
    return write_plan


def make_outline(n):
    return PlanOutline(
        sub_tasks=[PlanSubTask(title=f"子问题{i}", keywords=f"关键词{i}") for i in range(1, n + 1)]
    )


def make_worker(delay=0.0, fail_ids=frozenset()):
    calls = []

    async def run_worker(task_id, sub_task, max_pages):
        calls.append((task_id, dict(sub_task), max_pages))
        if delay:
            await asyncio.sleep(delay)
        error = "boom" if sub_task["id"] in fail_ids else None
        return {
            "sub_task_id": sub_task["id"],
            "title": sub_task["title"],
            "note": None if error else {"summary": f"{sub_task['title']} 的笔记 [1]", "facts": []},
            "sources": []
            if error
            else [{"idx": 1, "url": f"https://a.com/{sub_task['id']}", "title": "s"}],
            "note_tokens": 0 if error else 800,
            "note_db_id": None,
            "error": error,
        }

    run_worker.calls = calls
    return run_worker


def make_deps(plan=None, worker=None, store=None, recorder=None, **kwargs):
    return OrchestratorDeps(
        write_plan=plan or make_plan(make_outline(5)),
        run_worker=worker or make_worker(),
        recorder=recorder or FakeRecorder(),
        store=store or FakeStore(),
        **kwargs,
    )


def test_merge_worker_results_keeps_everything():
    ok = lambda i: {  # noqa: E731
        "sub_task_id": i,
        "title": f"t{i}",
        "note": {"summary": "n"},
        "sources": [{"idx": 1, "url": f"https://a.com/{i}"}],
        "note_tokens": 100,
        "note_db_id": i,
        "error": None,
    }
    failed = {
        "sub_task_id": 9,
        "title": "t9",
        "note": None,
        "sources": [],
        "note_tokens": 0,
        "note_db_id": None,
        "error": "search failed",
    }
    merged = merge_worker_results([ok(1), failed, ok(2)])

    assert merged["executed"] == 3
    assert [n["sub_task_id"] for n in merged["notes"]] == [1, 2]
    assert len(merged["sources"]) == 2
    assert merged["worker_errors"] == [{"sub_task_id": 9, "title": "t9", "error": "search failed"}]


async def test_full_flow_plan_then_execute():
    store, recorder = FakeStore(), FakeRecorder()
    worker = make_worker()
    deps = make_deps(make_plan(make_outline(5)), worker, store, recorder)

    graph = build_main_graph(deps)
    final = await graph.ainvoke(
        {"task_id": 1, "question": "问题", "background": "背景", "depth": "std", "max_pages": 7}
    )

    assert final["sub_task_count"] == 5
    assert final["plan_fallback"] is False
    assert final["plan_tokens"] == 580
    assert final["executed"] == 5
    assert len(final["notes"]) == 5
    assert len(final["sources"]) == 5  # 每 worker 1 条，无丢失
    assert final["worker_errors"] == []

    assert len(store.created) == 5
    assert store.usage_calls == [(1, get_settings().llm_model_chat, 500, 80)]

    # worker 拿到落库后的 id 与 max_pages
    assert [c[1]["id"] for c in worker.calls] == [1, 2, 3, 4, 5]
    assert all(c[2] == 7 for c in worker.calls)
    assert all(c[1]["keywords"] == f"关键词{i}" for i, c in enumerate(worker.calls, 1))

    types = [e["type"] for e in recorder.events]
    assert types == [EventType.plan, EventType.control]
    plan_ev = recorder.events[0]
    assert plan_ev["payload"]["count"] == 5
    assert plan_ev["payload"]["fallback"] is False
    assert plan_ev["tokens"] == 580
    exec_ev = recorder.events[1]
    assert exec_ev["payload"] == {"stage": "execute", "ran": 5, "ok": 5, "failed": 0}


async def test_plan_failure_falls_back_to_default_outline():
    store, recorder = FakeStore(), FakeRecorder()
    worker = make_worker()
    deps = make_deps(make_plan(error=RuntimeError("deepseek 503")), worker, store, recorder)

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "AI 芯片竞争格局", "depth": "std"})

    assert final["plan_fallback"] is True
    assert final["sub_task_count"] == 5
    titles = [st["title"] for st in final["sub_tasks"]]
    assert "AI 芯片竞争格局的发展现状与整体规模" in titles
    assert store.usage_calls == []  # 兜底路径不产生 LLM 计费

    control = recorder.events[0]
    assert control["type"] == EventType.control
    assert control["payload"]["stage"] == "plan"
    assert "503" in control["payload"]["error"]
    plan_ev = recorder.events[1]
    assert plan_ev["type"] == EventType.plan
    assert plan_ev["payload"]["fallback"] is True
    assert final["executed"] == 5  # 兜底大纲照常执行


async def test_execute_runs_in_parallel_not_serial():
    deps = make_deps(make_plan(make_outline(5)), make_worker(delay=0.2))

    graph = build_main_graph(deps)
    start = time.perf_counter()
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "std"})
    elapsed = time.perf_counter() - start

    assert final["executed"] == 5
    assert elapsed < 0.7  # 串行至少 5 × 0.2 = 1.0s


async def test_semaphore_limits_concurrency():
    active = 0
    peak = 0

    async def run_worker(task_id, sub_task, max_pages):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {
            "sub_task_id": sub_task["id"],
            "title": sub_task["title"],
            "note": {"summary": "n"},
            "sources": [],
            "note_tokens": 1,
            "error": None,
        }

    deps = make_deps(make_plan(make_outline(5)), run_worker, max_parallel=2)

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "std"})

    assert final["executed"] == 5
    assert peak == 2


async def test_worker_failure_isolated():
    deps = make_deps(make_plan(make_outline(5)), make_worker(fail_ids={2, 4}))

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "std"})

    assert final["executed"] == 5
    assert len(final["notes"]) == 3
    assert len(final["sources"]) == 3
    assert {e["sub_task_id"] for e in final["worker_errors"]} == {2, 4}


async def test_execute_skips_completed_subtasks():
    """resume 语义：done/failed 不重跑，崩溃残留 running 与 pending 重跑。"""
    store = FakeStore()
    store.rows = [
        {"id": 1, "task_id": 1, "title": "已完成", "keywords": None, "status": "done"},
        {"id": 2, "task_id": 1, "title": "也完成", "keywords": None, "status": "done"},
        {"id": 3, "task_id": 1, "title": "失败过", "keywords": None, "status": "failed"},
        {"id": 4, "task_id": 1, "title": "残留运行", "keywords": None, "status": "running"},
        {"id": 5, "task_id": 1, "title": "待运行", "keywords": None, "status": "pending"},
    ]
    store._next_id = 6
    worker = make_worker()
    deps = make_deps(make_plan(make_outline(2)), worker, store)

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "quick"})

    # 新建 2 + 历史 running/pending 2；done×2 与 failed×1 跳过
    assert final["executed"] == 4
    ran_ids = sorted(c[1]["id"] for c in worker.calls)
    assert ran_ids == [4, 5, 6, 7]


async def test_zero_subtasks_routes_to_end():
    worker = make_worker()
    deps = make_deps(make_plan(PlanOutline(sub_tasks=[])), worker)

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "quick"})

    assert final.get("executed") is None  # execute 节点未运行
    assert worker.calls == []
