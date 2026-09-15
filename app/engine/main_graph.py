"""主图（W2）：plan（大纲生成）→ execute（并行 fan-out）。

D8 范围只实现前两个节点，D9 在 execute 后接 synthesize、D10 加 reflect，
接口按四节点主图设计，后续节点在此图上扩展。

并行策略：asyncio.gather 在 execute 节点内 fan-out，每个 worker 跑 W1 子图，
结果由 merge_worker_results（"笔记 reducer"）合并——不走 LangGraph Send，
规避并行分支的 reducer 语义坑（见执行方案第 6 章风险预案）。

resume 语义：execute 每次进入从 store 读 pending 子任务，已完成/已失败的不重跑；
崩溃残留的 running 状态视为待执行。检查点由调用方（CLI/D9 worker）注入。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.db import EventType
from app.engine.planner import DEPTH_SUBTASK_COUNT, PlanOutline, default_outline

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 3
DEFAULT_MAX_PARALLEL = 5


class MainState(TypedDict, total=False):
    task_id: int
    question: str
    background: str | None
    depth: str
    max_pages: int

    # plan 节点输出
    sub_tasks: list[dict]  # [{id, title, keywords}]
    sub_task_count: int
    plan_fallback: bool
    plan_tokens: int

    # execute 节点输出（reducer 合并）
    executed: int
    notes: list[dict]
    sources: list[dict]
    worker_errors: list[dict]

    # synthesize 节点输出
    report_id: int | None
    report_chars: int
    citations: int


class PlanFn(Protocol):
    async def __call__(
        self, question: str, background: str | None, depth: str
    ) -> tuple[PlanOutline, object | None]: ...


class WorkerRunner(Protocol):
    async def __call__(self, task_id: int, sub_task: dict, max_pages: int) -> dict: ...


class ReportFn(Protocol):
    async def __call__(
        self, question: str, background: str | None, notes: list[dict], sources: list[dict]
    ) -> tuple[object, object | None]: ...


class SubTaskStoreProtocol(Protocol):
    async def create_sub_tasks(self, task_id: int, sub_tasks: list[dict]) -> list[dict]: ...

    async def pending_sub_tasks(self, task_id: int) -> list[dict]: ...

    async def synthesis_inputs(self, task_id: int) -> tuple[list[dict], list[dict]]: ...

    async def persist_report(
        self, task_id: int, markdown: str, citation_map: dict[int, int], token_total: int
    ) -> int: ...

    async def add_usage(
        self, task_id: int, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None: ...


class RecorderProtocol(Protocol):
    async def record(
        self,
        task_id: int,
        type: EventType,
        payload: dict | None = None,
        *,
        sub_task_id: int | None = None,
        tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> object: ...


@dataclass
class OrchestratorDeps:
    write_plan: PlanFn
    run_worker: WorkerRunner
    write_report: ReportFn
    recorder: RecorderProtocol
    store: SubTaskStoreProtocol
    max_parallel: int = DEFAULT_MAX_PARALLEL


def merge_worker_results(results: list[dict]) -> dict:
    """并行 worker 输出的 reducer：笔记、信源、错误按序合并，不丢不重。"""
    notes: list[dict] = []
    sources: list[dict] = []
    worker_errors: list[dict] = []
    for r in results:
        if r.get("note"):
            notes.append(
                {
                    "sub_task_id": r["sub_task_id"],
                    "title": r["title"],
                    "note": r["note"],
                    "note_tokens": r.get("note_tokens", 0),
                    "note_db_id": r.get("note_db_id"),
                }
            )
        sources.extend(r.get("sources", []))
        if r.get("error"):
            worker_errors.append(
                {"sub_task_id": r["sub_task_id"], "title": r["title"], "error": r["error"]}
            )
    return {
        "executed": len(results),
        "notes": notes,
        "sources": sources,
        "worker_errors": worker_errors,
    }


def build_main_graph(deps: OrchestratorDeps, checkpointer=None):
    async def plan_node(state: MainState) -> dict:
        task_id = state["task_id"]
        fallback = False
        usage = None
        try:
            outline, usage = await deps.write_plan(
                state["question"], state.get("background"), state["depth"]
            )
        except Exception as e:  # noqa: BLE001
            if state["depth"] not in DEPTH_SUBTASK_COUNT:
                raise
            logger.warning("plan node failed, using default outline: %s", e)
            await deps.recorder.record(
                task_id,
                EventType.control,
                {"stage": "plan", "error": f"{type(e).__name__}: {e}"},
            )
            outline = default_outline(state["question"], DEPTH_SUBTASK_COUNT[state["depth"]])
            fallback = True

        sub_tasks = await deps.store.create_sub_tasks(
            task_id, [st.model_dump() for st in outline.sub_tasks]
        )
        tokens = usage.total_tokens if usage else 0
        if usage is not None:
            await deps.store.add_usage(
                task_id,
                get_settings().llm_model_chat,
                usage.prompt_tokens,
                usage.completion_tokens,
            )
        await deps.recorder.record(
            task_id,
            EventType.plan,
            {
                "depth": state["depth"],
                "count": len(sub_tasks),
                "titles": [st["title"] for st in sub_tasks],
                "fallback": fallback,
            },
            tokens=tokens or None,
        )
        return {
            "sub_tasks": sub_tasks,
            "sub_task_count": len(sub_tasks),
            "plan_fallback": fallback,
            "plan_tokens": tokens,
        }

    async def execute_node(state: MainState) -> dict:
        task_id = state["task_id"]
        max_pages = state.get("max_pages", DEFAULT_MAX_PAGES)
        pending = await deps.store.pending_sub_tasks(task_id)

        if not pending:
            await deps.recorder.record(
                task_id,
                EventType.control,
                {"stage": "execute", "info": "no pending sub tasks"},
            )
            return {"executed": 0, "notes": [], "sources": [], "worker_errors": []}

        sem = asyncio.Semaphore(deps.max_parallel)

        async def run_one(sub_task: dict) -> dict:
            async with sem:
                return await deps.run_worker(task_id, sub_task, max_pages)

        results = await asyncio.gather(*[run_one(st) for st in pending])
        merged = merge_worker_results(list(results))

        await deps.recorder.record(
            task_id,
            EventType.control,
            {
                "stage": "execute",
                "ran": len(pending),
                "ok": len(merged["notes"]),
                "failed": len(merged["worker_errors"]),
            },
        )
        return merged

    def after_plan(state: MainState) -> Literal["execute", "__end__"]:
        return "execute" if state.get("sub_tasks") else "__end__"

    async def synthesize_node(state: MainState) -> dict:
        task_id = state["task_id"]
        # 从 DB 读全量笔记/信源（非 state）：resume 后续跑也拿得到历史轮产物
        notes, sources = await deps.store.synthesis_inputs(task_id)

        if not notes:
            await deps.recorder.record(
                task_id,
                EventType.control,
                {"stage": "synthesize", "info": "no notes, skip report"},
            )
            return {"report_id": None, "report_chars": 0, "citations": 0}

        draft, usage = await deps.write_report(
            state["question"], state.get("background"), notes, sources
        )
        report_id = await deps.store.persist_report(
            task_id, draft.markdown, draft.citation_map, usage.total_tokens if usage else 0
        )
        if usage is not None:
            await deps.store.add_usage(
                task_id,
                get_settings().llm_model_reasoner,
                usage.prompt_tokens,
                usage.completion_tokens,
            )

        await deps.recorder.record(
            task_id,
            EventType.synthesize,
            {
                "report_id": report_id,
                "chars": len(draft.markdown),
                "citations": draft.n_citations,
                "notes": len(notes),
                "sources": len(sources),
            },
            tokens=usage.total_tokens if usage else None,
        )
        return {
            "report_id": report_id,
            "report_chars": len(draft.markdown),
            "citations": draft.n_citations,
        }

    graph = StateGraph(MainState)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", after_plan)
    graph.add_edge("execute", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile(checkpointer=checkpointer)
