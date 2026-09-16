"""主图（W2）：plan（大纲生成）→ execute（并行 fan-out）→ reflect → synthesize。

reflect 在每轮 execute 后对照 checklist 检测缺口：LLM 评估覆盖度 + 消费追加
指示信箱，产出补充轮子任务（round_no ≥ 2）后回到 execute；无缺口或到达
补充轮上限（≤2）进入 synthesize。接口按四节点主图设计。

预算控制（D11）：80% 降级在节点边界生效——execute 入口降级则跳过剩余
子任务（置 skipped），reflect 降级则跳过补充轮（指示留在信箱待续跑消费），
synthesize 照常出报告并附预算受限声明；各阶段落 budget 事件（配额对照），
降级时刻落 degrade 事件。预算判定每次从 DB 新鲜读 token_used。

并行策略：asyncio.gather 在 execute 节点内 fan-out，每个 worker 跑 W1 子图，
结果由 merge_worker_results（"笔记 reducer"）合并——不走 LangGraph Send，
规避并行分支的 reducer 语义坑（见执行方案第 6 章风险预案）。

resume 语义：plan 节点幂等（已有子任务即跳过重规划，避免续跑重复建任务）；
execute 每次进入从 store 读 pending 子任务，已完成/已失败的不重跑，
崩溃残留的 running 状态视为待执行（running 且已落笔记由 store 自愈为
done）。reflect/synthesize 的输入一律从 DB 读（state 只作路由与展示），
崩溃恢复或补充轮重入都能拿到全量历史产物。检查点由调用方注入。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.db import EventType, TaskStatus
from app.engine.budget import budget_payload, disclaimer_md, is_degraded
from app.engine.planner import DEPTH_SUBTASK_COUNT, PlanOutline, default_outline
from app.engine.reflector import Reflection, instruction_to_sub_task

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 3
DEFAULT_MAX_PARALLEL = 5
MAX_REFLECT_ROUNDS = 2  # 补充轮上限（不含首轮 execute）
MAX_GAPS_PER_ROUND = 3


class JobSupersededError(Exception):
    """任务已被暂停/终止，或 run_token 被新一次执行抢占。

    编排器捕获后直接退出：不回写任务状态（状态由控制方或新 job 拥有），
    防止旧 job 与 resume 后的新 job 并发执行同一任务。
    """


class MainState(TypedDict, total=False):
    task_id: int
    question: str
    background: str | None
    depth: str
    max_pages: int
    run_token: str  # job 所有权令牌（编排器生成，围栏检查用）

    # plan 节点输出
    sub_tasks: list[dict]  # [{id, title, keywords}]
    sub_task_count: int
    plan_fallback: bool
    plan_tokens: int

    # execute 节点输出（reducer 合并，仅当前轮次）
    executed: int
    notes: list[dict]
    sources: list[dict]
    worker_errors: list[dict]

    # reflect 节点输出
    reflect_rounds: int  # 已执行的反思轮数
    need_more: bool  # 本轮是否产出了补充子任务（路由 execute/synthesize）
    supplemented: int  # 累计补充子任务数（缺口 + 追加指示）
    gap_history: list[dict]  # 每轮评估摘要 [{round, assessment, created}]

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


class ReflectFn(Protocol):
    async def __call__(
        self,
        question: str,
        background: str | None,
        sub_tasks: list[dict],
        notes: list[dict],
        sources: list[dict],
    ) -> tuple[Reflection, object | None]: ...


class SubTaskStoreProtocol(Protocol):
    async def create_sub_tasks(
        self, task_id: int, sub_tasks: list[dict], round_no: int = 1
    ) -> list[dict]: ...

    async def pending_sub_tasks(self, task_id: int) -> list[dict]: ...

    async def skip_pending_sub_tasks(self, task_id: int) -> int: ...

    async def task_budget_state(self, task_id: int) -> tuple[int, int]: ...

    async def task_run_state(self, task_id: int) -> tuple[object, str | None]: ...

    async def latest_report(self, task_id: int) -> object | None: ...

    async def all_sub_tasks(self, task_id: int) -> list[dict]: ...

    async def pending_instructions(self, task_id: int) -> list[dict]: ...

    async def mark_instructions_consumed(self, task_id: int, round_no: int) -> None: ...

    async def synthesis_inputs(self, task_id: int) -> tuple[list[dict], list[dict]]: ...

    async def persist_report(
        self,
        task_id: int,
        markdown: str,
        citation_map: dict[int, int],
        token_total: int,
        chart_specs: list[dict] | None = None,
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
    reflect: ReflectFn
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
    async def _ensure_job_current(task_id: int, run_token: str | None) -> None:
        """job 围栏：任务已暂停/终止或令牌被新 run 抢占时中止本 job。

        run_token 未提供（单测直跑图/旧调用方）时跳过检查，保持向后兼容。
        """
        if not run_token:
            return
        status, current = await deps.store.task_run_state(task_id)
        if status != TaskStatus.running or current != run_token:
            reason = (
                f"task {getattr(status, 'value', status)}"
                if status != TaskStatus.running
                else "superseded"
            )
            await deps.recorder.record(
                task_id, EventType.control, {"stage": "job", "info": f"abort: {reason}"}
            )
            raise JobSupersededError(reason)

    async def plan_node(state: MainState) -> dict:
        task_id = state["task_id"]
        await _ensure_job_current(task_id, state.get("run_token"))

        # resume 幂等：子任务已落库（前次执行崩溃/被 kill）则跳过重规划，
        # 避免续跑重复建任务重复计费
        existing = await deps.store.all_sub_tasks(task_id)
        if existing:
            await deps.recorder.record(
                task_id,
                EventType.control,
                {"stage": "plan", "info": "resume: sub tasks exist, skip planning"},
            )
            return {
                "sub_tasks": existing,
                "sub_task_count": len(existing),
                "plan_fallback": False,
                "plan_tokens": 0,
            }

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
                "model": get_settings().llm_model_chat,
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
            },
            tokens=tokens or None,
        )
        used, budget = await deps.store.task_budget_state(task_id)
        await deps.recorder.record(task_id, EventType.budget, budget_payload("plan", used, budget))
        return {
            "sub_tasks": sub_tasks,
            "sub_task_count": len(sub_tasks),
            "plan_fallback": fallback,
            "plan_tokens": tokens,
        }

    async def execute_node(state: MainState) -> dict:
        task_id = state["task_id"]
        max_pages = state.get("max_pages", DEFAULT_MAX_PAGES)
        run_token = state.get("run_token")

        await _ensure_job_current(task_id, run_token)

        # 预算降级：入口即超阈值则跳过剩余子任务（首轮极小预算或续跑超支）
        used, budget = await deps.store.task_budget_state(task_id)
        if is_degraded(used, budget):
            skipped = await deps.store.skip_pending_sub_tasks(task_id)
            await deps.recorder.record(
                task_id,
                EventType.degrade,
                {"stage": "execute", "used": used, "budget": budget, "skipped": skipped},
            )
            await deps.recorder.record(
                task_id, EventType.budget, budget_payload("execute", used, budget)
            )
            return {"executed": 0, "notes": [], "sources": [], "worker_errors": []}

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
            # 子任务边界围栏：在拿到并发槽位后、发起 LLM 调用前复查，
            # 暂停/抢占对"已排队未起飞"的子任务同样生效（飞行中的不可中断）
            async with sem:
                await _ensure_job_current(task_id, run_token)
                return await deps.run_worker(task_id, sub_task, max_pages)

        # return_exceptions：等在飞行子任务全部落地后再统一上抛（含围栏中止），
        # 避免孤儿任务继续写库
        results = await asyncio.gather(*[run_one(st) for st in pending], return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r
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
        used, budget = await deps.store.task_budget_state(task_id)
        await deps.recorder.record(
            task_id, EventType.budget, budget_payload("execute", used, budget)
        )
        return merged

    def after_plan(state: MainState) -> Literal["execute", "__end__"]:
        return "execute" if state.get("sub_tasks") else "__end__"

    async def reflect_node(state: MainState) -> dict:
        """缺口检测：LLM 覆盖度评估 + 消费追加指示 → 补充轮子任务。

        输入一律从 DB 读（笔记/信源/子任务状态/信箱），补充轮重入与
        崩溃恢复后拿到的都是全量历史。LLM 失败不阻断主链路——记录后
        照常进入 synthesize（报告仍可基于现有材料产出）。

        预算降级：跳过评估与补充轮，追加指示留在信箱（续跑调预算后可
        再消费），直接进入 synthesize 并在报告尾部声明。
        """
        task_id = state["task_id"]
        rounds = state.get("reflect_rounds", 0)
        await _ensure_job_current(task_id, state.get("run_token"))

        if rounds >= MAX_REFLECT_ROUNDS:
            await deps.recorder.record(
                task_id, EventType.reflect, {"round": rounds, "skip": "max_rounds", "created": 0}
            )
            return {"need_more": False}

        used, budget = await deps.store.task_budget_state(task_id)
        if is_degraded(used, budget):
            await deps.recorder.record(
                task_id,
                EventType.degrade,
                {
                    "stage": "reflect",
                    "used": used,
                    "budget": budget,
                    "action": "skip_reflect_and_supplement",
                },
            )
            await deps.recorder.record(
                task_id, EventType.budget, budget_payload("reflect", used, budget)
            )
            return {
                "need_more": False,
                "supplemented": state.get("supplemented", 0),
                "gap_history": state.get("gap_history", []),
            }

        notes, sources = await deps.store.synthesis_inputs(task_id)
        sub_tasks = await deps.store.all_sub_tasks(task_id)
        instructions = await deps.store.pending_instructions(task_id)

        if not notes and not instructions:
            await deps.recorder.record(
                task_id,
                EventType.reflect,
                {"round": rounds + 1, "skip": "no_notes", "created": 0},
            )
            return {
                "reflect_rounds": rounds + 1,
                "need_more": False,
                "supplemented": state.get("supplemented", 0),
                "gap_history": state.get("gap_history", []),
            }

        reflection: Reflection | None = None
        usage = None
        if notes:
            try:
                reflection, usage = await deps.reflect(
                    state["question"], state.get("background"), sub_tasks, notes, sources
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("reflect node failed, continue to synthesize: %s", e)
                await deps.recorder.record(
                    task_id,
                    EventType.control,
                    {"stage": "reflect", "error": f"{type(e).__name__}: {e}"},
                )
            if usage is not None:
                await deps.store.add_usage(
                    task_id,
                    get_settings().llm_model_chat,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                )

        gap_tasks: list[dict] = []
        if reflection is not None and reflection.has_gaps:
            gap_tasks = [g.model_dump() for g in reflection.gaps[:MAX_GAPS_PER_ROUND]]

        round_no = rounds + 2  # 首轮 execute 为 round 1，补充轮从 2 起
        created: list[dict] = []
        if instructions:
            created += await deps.store.create_sub_tasks(
                task_id, [instruction_to_sub_task(i["text"]) for i in instructions], round_no
            )
            await deps.store.mark_instructions_consumed(task_id, round_no)
        if gap_tasks:
            created += await deps.store.create_sub_tasks(task_id, gap_tasks, round_no)

        await deps.recorder.record(
            task_id,
            EventType.reflect,
            {
                "round": rounds + 1,
                "assessment": reflection.assessment if reflection else "",
                "has_gaps": bool(gap_tasks),
                "gap_titles": [g["title"] for g in gap_tasks],
                "instructions": len(instructions),
                "created": len(created),
                "next_round": round_no if created else None,
                "model": get_settings().llm_model_chat if usage else None,
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
            },
            tokens=usage.total_tokens if usage else None,
        )
        used, budget = await deps.store.task_budget_state(task_id)
        await deps.recorder.record(
            task_id, EventType.budget, budget_payload("reflect", used, budget)
        )
        return {
            "reflect_rounds": rounds + 1,
            "need_more": bool(created),
            "supplemented": state.get("supplemented", 0) + len(created),
            "gap_history": state.get("gap_history", [])
            + [
                {
                    "round": rounds + 1,
                    "assessment": reflection.assessment if reflection else "",
                    "created": len(created),
                }
            ],
        }

    def after_reflect(state: MainState) -> Literal["execute", "synthesize"]:
        # reflect_rounds ≤ 上限才允许再进 execute：否则本轮创建的补充任务
        # 将永远悬挂（第 N+1 次反思才被 skip 拦住）
        if state.get("need_more") and state.get("reflect_rounds", 0) <= MAX_REFLECT_ROUNDS:
            return "execute"
        return "synthesize"

    async def synthesize_node(state: MainState) -> dict:
        task_id = state["task_id"]
        await _ensure_job_current(task_id, state.get("run_token"))

        # 报告幂等：崩溃恢复/并发窗口内已产出报告 → 不再重复调 LLM
        existing = await deps.store.latest_report(task_id)
        if existing is not None:
            await deps.recorder.record(
                task_id,
                EventType.control,
                {"stage": "synthesize", "info": "report exists, skip"},
            )
            return {
                "report_id": existing.id,
                "report_chars": len(existing.markdown),
                "citations": len(existing.citation_map or {}),
            }

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
        # 预算降级：报告尾部追加声明（无引用锚点，不影响 citation_map）
        used, budget = await deps.store.task_budget_state(task_id)
        degraded = is_degraded(used, budget)
        markdown = draft.markdown + (disclaimer_md(used, budget) if degraded else "")
        report_id = await deps.store.persist_report(
            task_id,
            markdown,
            draft.citation_map,
            usage.total_tokens if usage else 0,
            draft.chart_specs,
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
                "chars": len(markdown),
                "citations": draft.n_citations,
                "notes": len(notes),
                "sources": len(sources),
                "budget_degraded": degraded,
                "model": get_settings().llm_model_reasoner if usage else None,
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
            },
            tokens=usage.total_tokens if usage else None,
        )
        used, budget = await deps.store.task_budget_state(task_id)
        await deps.recorder.record(
            task_id, EventType.budget, budget_payload("synthesize", used, budget)
        )
        return {
            "report_id": report_id,
            "report_chars": len(markdown),
            "citations": draft.n_citations,
        }

    graph = StateGraph(MainState)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("reflect", reflect_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", after_plan)
    graph.add_edge("execute", "reflect")
    graph.add_conditional_edges("reflect", after_reflect)
    graph.add_edge("synthesize", END)
    return graph.compile(checkpointer=checkpointer)
