import asyncio
import time
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.db import EventType
from app.engine.main_graph import (
    OrchestratorDeps,
    build_main_graph,
    merge_worker_results,
)
from app.engine.planner import PlanOutline, PlanSubTask
from app.engine.reflector import Reflection, SupplementaryTask


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
    def __init__(self, instructions: list[dict] | None = None):
        self.rows: list[dict] = []
        self.created: list[dict] = []
        self.usage_calls: list[tuple] = []
        self.reports: list[dict] = []
        self.synthesis_notes: list[dict] = []  # synthesize/reflect 节点读到的笔记
        self.instructions: list[dict] = instructions or []  # 追加指示信箱
        self.consumed_rounds: list[int] = []
        self._next_id = 1
        self._next_report_id = 1

    async def create_sub_tasks(self, task_id, sub_tasks, round_no=1):
        out = []
        for s in sub_tasks:
            row = {
                "id": self._next_id,
                "task_id": task_id,
                "title": s["title"],
                "keywords": s.get("keywords"),
                "status": "pending",
                "round_no": round_no,
            }
            self._next_id += 1
            self.rows.append(row)
            self.created.append(dict(row))
            out.append({"id": row["id"], "title": row["title"], "keywords": row["keywords"]})
        return out

    async def all_sub_tasks(self, task_id):
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "keywords": r["keywords"],
                "status": r["status"],
                "round_no": r["round_no"],
            }
            for r in self.rows
        ]

    async def pending_sub_tasks(self, task_id):
        return [
            {"id": r["id"], "title": r["title"], "keywords": r["keywords"]}
            for r in self.rows
            if r["status"] in ("pending", "running")
        ]

    async def pending_instructions(self, task_id):
        return [i for i in self.instructions if i.get("consumed_round") is None]

    async def mark_instructions_consumed(self, task_id, round_no):
        self.consumed_rounds.append(round_no)
        for i in self.instructions:
            if i.get("consumed_round") is None:
                i["consumed_round"] = round_no

    async def synthesis_inputs(self, task_id):
        sources = [
            {
                "id": 100 + i,
                "url": f"https://s.com/{i}",
                "title": "s",
                "domain": "s.com",
                "credibility": 3,
            }
            for i in range(len(self.synthesis_notes))
        ]
        return self.synthesis_notes, sources

    async def persist_report(self, task_id, markdown, citation_map, token_total):
        report_id = self._next_report_id
        self._next_report_id += 1
        self.reports.append(
            {
                "id": report_id,
                "task_id": task_id,
                "markdown": markdown,
                "citation_map": citation_map,
                "token_total": token_total,
            }
        )
        return report_id

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


def make_reflect(has_gaps=False, gap_titles=(), error=None, always=False):
    """fake reflect：has_gaps=True 仅首轮报缺口；always=True 每轮都报。"""
    calls = []
    gap_list = [
        SupplementaryTask(title=t, keywords=f"补搜关键词{i}") for i, t in enumerate(gap_titles, 1)
    ]

    async def reflect(question, background, sub_tasks, notes, sources):
        nth = len(calls) + 1
        calls.append((question, background, len(sub_tasks), len(notes), len(sources)))
        if error:
            raise error
        gaps = gap_list if (always or (has_gaps and nth == 1)) else []
        return Reflection(
            has_gaps=bool(gaps), assessment="覆盖度评估" if gaps else "材料充分", gaps=gaps
        ), SimpleNamespace(prompt_tokens=200, completion_tokens=50, total_tokens=250)

    reflect.calls = calls
    return reflect


def make_worker(delay=0.0, fail_ids=frozenset(), store=None):
    """worker 成功时把笔记写入 store.synthesis_notes 并置子任务行状态（模拟 DB 落库与状态机）。"""
    calls = []

    async def run_worker(task_id, sub_task, max_pages):
        calls.append((task_id, dict(sub_task), max_pages))
        if delay:
            await asyncio.sleep(delay)
        error = "boom" if sub_task["id"] in fail_ids else None
        if store is not None:
            for r in store.rows:
                if r["id"] == sub_task["id"]:
                    r["status"] = "failed" if error else "done"
            if not error:
                note = {"title": sub_task["title"], "content": "核心发现 [1]"}
                store.synthesis_notes.append(note)
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


def make_report(error=None):
    calls = []

    async def write_report(question, background, notes, sources):
        calls.append((question, background, len(notes), len(sources)))
        if error:
            raise error
        draft = SimpleNamespace(
            markdown=f"# {question}\n\n结论 [1]。", citation_map={1: 101}, n_citations=1
        )
        return draft, SimpleNamespace(prompt_tokens=800, completion_tokens=1500, total_tokens=2300)

    write_report.calls = calls
    return write_report


def make_deps(
    plan=None, worker=None, report=None, reflect=None, store=None, recorder=None, **kwargs
):
    store = store or FakeStore()
    return OrchestratorDeps(
        write_plan=plan or make_plan(make_outline(5)),
        run_worker=worker or make_worker(store=store),
        write_report=report or make_report(),
        reflect=reflect or make_reflect(),
        recorder=recorder or FakeRecorder(),
        store=store,
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


async def test_full_flow_plan_execute_reflect_synthesize():
    store, recorder = FakeStore(), FakeRecorder()
    plan = make_plan(make_outline(5))
    worker = make_worker(store=store)
    report = make_report()
    deps = make_deps(
        plan=plan,
        worker=worker,
        report=report,
        reflect=make_reflect(),
        store=store,
        recorder=recorder,
    )

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
    # 反思无缺口：1 轮评估，未补充
    assert final["reflect_rounds"] == 1
    assert final["supplemented"] == 0
    assert final["gap_history"] == [{"round": 1, "assessment": "材料充分", "created": 0}]

    assert len(store.created) == 5
    assert all(r["round_no"] == 1 for r in store.created)
    # plan（chat）+ 反思（chat）+ 报告（reasoner）三笔计费
    assert store.usage_calls == [
        (1, get_settings().llm_model_chat, 500, 80),
        (1, get_settings().llm_model_chat, 200, 50),
        (1, get_settings().llm_model_reasoner, 800, 1500),
    ]

    # worker 拿到落库后的 id 与 max_pages
    assert [c[1]["id"] for c in worker.calls] == [1, 2, 3, 4, 5]
    assert all(c[2] == 7 for c in worker.calls)
    assert all(c[1]["keywords"] == f"关键词{i}" for i, c in enumerate(worker.calls, 1))

    # reflect 从 store 读到 5 份笔记与 5 条信源
    assert deps.reflect.calls == [("问题", "背景", 5, 5, 5)]

    # synthesize：从 store 读到 5 份笔记，报告落库并回填 state
    assert report.calls == [("问题", "背景", 5, 5)]
    assert final["report_id"] == 1
    assert final["report_chars"] > 0
    assert final["citations"] == 1
    assert store.reports[0]["markdown"] == "# 问题\n\n结论 [1]。"
    assert store.reports[0]["citation_map"] == {1: 101}
    assert store.reports[0]["token_total"] == 2300

    types = [e["type"] for e in recorder.events]
    assert types == [EventType.plan, EventType.control, EventType.reflect, EventType.synthesize]
    plan_ev = recorder.events[0]
    assert plan_ev["payload"]["count"] == 5
    assert plan_ev["payload"]["fallback"] is False
    assert plan_ev["tokens"] == 580
    exec_ev = recorder.events[1]
    assert exec_ev["payload"] == {"stage": "execute", "ran": 5, "ok": 5, "failed": 0}
    ref_ev = recorder.events[2]
    assert ref_ev["payload"]["has_gaps"] is False
    assert ref_ev["payload"]["created"] == 0
    assert ref_ev["tokens"] == 250
    syn_ev = recorder.events[3]
    assert syn_ev["payload"] == {
        "report_id": 1,
        "chars": final["report_chars"],
        "citations": 1,
        "notes": 5,
        "sources": 5,
    }
    assert syn_ev["tokens"] == 2300


async def test_synthesize_skips_when_no_notes():
    """全部 worker 失败 → DB 无笔记 → 反思跳过评估 → 不生成报告，图正常结束。"""
    store, recorder = FakeStore(), FakeRecorder()
    reflect = make_reflect()
    deps = make_deps(
        plan=make_plan(make_outline(3)),
        worker=make_worker(fail_ids={1, 2, 3}),
        report=make_report(),
        reflect=reflect,
        store=store,
        recorder=recorder,
    )

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "quick"})

    assert final["report_id"] is None
    assert store.reports == []
    assert reflect.calls == []  # 无笔记时省掉 LLM 调用
    types = [e["type"] for e in recorder.events]
    assert types[-1] == EventType.control
    assert recorder.events[-1]["payload"]["stage"] == "synthesize"
    # reflect 记录了 skip 事件
    ref_ev = [e for e in recorder.events if e["type"] == EventType.reflect][0]
    assert ref_ev["payload"] == {"round": 1, "skip": "no_notes", "created": 0}


async def test_gap_detection_triggers_supplementary_round():
    """缺数据场景：reflect 判定缺口 → 补搜子任务（round 2）→ execute 重入 → 综合。"""
    store, recorder = FakeStore(), FakeRecorder()
    worker = make_worker(store=store)
    report = make_report()
    reflect = make_reflect(has_gaps=True, gap_titles=["缺口A", "缺口B"])
    deps = make_deps(
        plan=make_plan(make_outline(2)),
        worker=worker,
        report=report,
        reflect=reflect,
        store=store,
        recorder=recorder,
    )

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "quick"})

    # 轮次：R1 执行 2 + R2 补充 2；reflect 评估 2 轮
    assert final["executed"] == 2  # 末轮 state
    assert final["reflect_rounds"] == 2
    assert final["supplemented"] == 2
    assert final["report_id"] == 1

    # 补充子任务 round_no=2，标题来自缺口
    round2 = [r for r in store.created if r["round_no"] == 2]
    assert [r["title"] for r in round2] == ["缺口A", "缺口B"]
    # 补充轮后全部子任务 4 个都被执行过
    assert len(worker.calls) == 4
    assert deps.reflect.calls == [
        ("q", None, 2, 2, 2),  # R1 后：2 子任务、2 笔记、2 信源
        ("q", None, 4, 4, 4),  # R2 后：累计 4 子任务、4 笔记、4 信源（store 全量）
    ]

    # 反思事件两轮，第二轮无缺口收尾
    ref_evs = [e for e in recorder.events if e["type"] == EventType.reflect]
    assert [e["payload"]["round"] for e in ref_evs] == [1, 2]
    assert ref_evs[0]["payload"]["gap_titles"] == ["缺口A", "缺口B"]
    assert ref_evs[0]["payload"]["next_round"] == 2
    assert ref_evs[1]["payload"]["created"] == 0

    # 补搜笔记参与综合（DB 全量）
    assert report.calls == [("q", None, 4, 4)]
    # 补充轮新子任务也被 worker 执行过
    assert sorted(c[1]["id"] for c in worker.calls) == [1, 2, 3, 4]


async def test_instruction_enters_next_reflect_round():
    """追加指示：reflect 消费信箱 → 转补搜子任务 → 下一轮 execute 执行。"""
    store, recorder = (
        FakeStore(
            instructions=[
                {"id": 1, "text": "重点补充成本数据", "created_at": "t", "consumed_round": None}
            ]
        ),
        FakeRecorder(),
    )
    worker = make_worker(store=store)
    report = make_report()
    deps = make_deps(
        plan=make_plan(make_outline(1)),
        worker=worker,
        report=report,
        reflect=make_reflect(),
        store=store,
        recorder=recorder,
    )

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "quick"})

    assert final["reflect_rounds"] == 2
    assert final["supplemented"] == 1
    assert final["report_id"] == 1

    # 指示转为 round 2 子任务，信箱消费标记
    round2 = [r for r in store.created if r["round_no"] == 2]
    assert len(round2) == 1
    assert round2[0]["title"] == "重点补充成本数据"
    assert round2[0]["keywords"] == "重点补充成本数据"
    assert store.consumed_rounds == [2]
    assert store.instructions[0]["consumed_round"] == 2

    # 两轮 execute：首轮 1 个 + 指示 1 个
    assert len(worker.calls) == 2
    # 报告基于 DB 全量 2 份笔记
    assert report.calls == [("q", None, 2, 2)]


async def test_reflect_rounds_capped_at_two_supplements():
    """每轮都判缺口 → 最多 2 个补充轮，第 3 次 reflect 直接收尾。"""
    store = FakeStore()
    worker = make_worker(store=store)
    deps = make_deps(
        plan=make_plan(make_outline(1)),
        worker=worker,
        reflect=make_reflect(always=True, gap_titles=["永远缺"]),
        store=store,
    )

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "quick"})

    assert final["reflect_rounds"] == 2  # 上限生效
    assert final["supplemented"] == 2
    assert final["report_id"] == 1  # 仍正常出报告
    assert len(worker.calls) == 3  # R1 1 个 + R2/R3 各 1 个补搜


async def test_reflect_llm_failure_degrades_to_synthesize():
    """reflect LLM 失败不阻断：control 事件记录后照常综合。"""
    store, recorder = FakeStore(), FakeRecorder()
    deps = make_deps(
        plan=make_plan(make_outline(2)),
        worker=make_worker(store=store),
        reflect=make_reflect(error=RuntimeError("deepseek 503")),
        store=store,
        recorder=recorder,
    )

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "quick"})

    assert final["report_id"] == 1
    assert final["supplemented"] == 0
    control = [e for e in recorder.events if e["type"] == EventType.control]
    assert control[-1]["payload"] == {
        "stage": "reflect",
        "error": "RuntimeError: deepseek 503",
    }
    # plan 一笔计费，reflect 失败调用不落 usage（reasoner 报告计费另算）
    assert store.usage_calls == [
        (1, get_settings().llm_model_chat, 500, 80),
        (1, get_settings().llm_model_reasoner, 800, 1500),
    ]


async def test_reflect_instruction_without_notes_still_runs():
    """首轮全失败但有追加指示：跳过 LLM 评估但消费指示补搜（不放弃）。"""
    store = FakeStore(
        instructions=[{"id": 1, "text": "换个角度重查", "created_at": "t", "consumed_round": None}]
    )
    reflect = make_reflect()
    deps = make_deps(
        plan=make_plan(make_outline(1)),
        worker=make_worker(fail_ids={1}, store=store),
        reflect=reflect,
        store=store,
    )

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "q", "depth": "quick"})

    assert final["supplemented"] == 1
    assert store.consumed_rounds == [2]
    # 首轮无笔记省掉 LLM，仅 R2 补搜成功后评估一次（2 子任务、1 笔记、1 信源）
    assert reflect.calls == [("q", None, 2, 1, 1)]
    assert final["report_id"] is not None  # 补搜轮成功出笔记 → 有报告


async def test_synthesize_failure_raises_out_of_graph():
    deps = make_deps(report=make_report(error=RuntimeError("reasoner 503")))

    graph = build_main_graph(deps)
    with pytest.raises(RuntimeError, match="reasoner 503"):
        await graph.ainvoke({"task_id": 1, "question": "q", "depth": "std"})


async def test_plan_failure_falls_back_to_default_outline():
    store, recorder = FakeStore(), FakeRecorder()
    deps = make_deps(
        plan=make_plan(error=RuntimeError("deepseek 503")),
        worker=make_worker(store=store),
        store=store,
        recorder=recorder,
    )

    graph = build_main_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "question": "AI 芯片竞争格局", "depth": "std"})

    assert final["plan_fallback"] is True
    assert final["sub_task_count"] == 5
    titles = [st["title"] for st in final["sub_tasks"]]
    assert "AI 芯片竞争格局的发展现状与整体规模" in titles
    # 兜底 plan 不计费：chat 一笔来自 reflect 评估，reasoner 一笔来自报告
    assert store.usage_calls == [
        (1, get_settings().llm_model_chat, 200, 50),
        (1, get_settings().llm_model_reasoner, 800, 1500),
    ]

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
        {
            "id": 1,
            "task_id": 1,
            "title": "已完成",
            "keywords": None,
            "status": "done",
            "round_no": 1,
        },
        {
            "id": 2,
            "task_id": 1,
            "title": "也完成",
            "keywords": None,
            "status": "done",
            "round_no": 1,
        },
        {
            "id": 3,
            "task_id": 1,
            "title": "失败过",
            "keywords": None,
            "status": "failed",
            "round_no": 1,
        },
        {
            "id": 4,
            "task_id": 1,
            "title": "残留运行",
            "keywords": None,
            "status": "running",
            "round_no": 1,
        },
        {
            "id": 5,
            "task_id": 1,
            "title": "待运行",
            "keywords": None,
            "status": "pending",
            "round_no": 1,
        },
    ]
    store._next_id = 6
    worker = make_worker(store=store)
    deps = make_deps(plan=make_plan(make_outline(2)), worker=worker, store=store)

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
