"""成本看板聚合：以 agent_events 为唯一对账基准。

口径：
- LLM 消耗：plan/note/reflect/synthesize/chat 事件，tokens 列为总量；
  payload.prompt_tokens / completion_tokens 为方向拆分（新事件），
  旧事件缺拆分时按 (tokens, 0) 保守折算，缺 model 时按事件类型推断
  （plan/note/reflect/chat → chat 模型，synthesize → reasoner 模型）。
- 缓存命中：search/fetch 事件 payload.provider == "cache"；
  节省金额 = 搜索缓存命中次数 × 博查单价（读页走免费通道，仅计命中率）。
- 金额折算复用 persister.PRICE_PER_M（deepseek 官方定价，页面注明计价口径）。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import AgentEvent, EventType, ResearchTask, TaskStatus
from app.engine.persister import PRICE_PER_M, cost_cny

LLM_EVENT_TYPES = (
    EventType.plan,
    EventType.note,
    EventType.reflect,
    EventType.synthesize,
    EventType.chat,
)
CACHE_EVENT_TYPES = (EventType.search, EventType.fetch)

MODEL_ROLE = {
    "deepseek-chat": "规划 / 执行 / 反思 / 追问",
    "deepseek-reasoner": "报告综合",
}

DEPTHS = ("quick", "std", "deep")


def _since(days: int) -> datetime | None:
    if days <= 0:
        return None
    return datetime.now(UTC) - timedelta(days=days)


def _round2(x: float) -> float:
    return round(x, 2)


def _infer_model(ev_type: EventType) -> str:
    settings = get_settings()
    if ev_type == EventType.synthesize:
        return settings.llm_model_reasoner
    return settings.llm_model_chat


async def collect_stats(session: AsyncSession, days: int) -> dict:
    since = _since(days)

    # ---- LLM 消耗：按模型聚合（prompt/completion 拆分 + 每日趋势） ----
    conditions = [AgentEvent.type.in_(LLM_EVENT_TYPES), AgentEvent.tokens.is_not(None)]
    if since:
        conditions.append(AgentEvent.created_at >= since)
    rows = (
        await session.execute(
            select(
                AgentEvent.type,
                AgentEvent.tokens,
                AgentEvent.created_at,
                AgentEvent.payload,
            ).where(*conditions)
        )
    ).all()

    by_model: dict[str, dict] = {}
    daily: dict[str, dict] = {}
    total_prompt = total_completion = 0

    for ev_type, tokens, created_at, payload in rows:
        payload = payload or {}
        model = payload.get("model") or _infer_model(ev_type)
        prompt = payload.get("prompt_tokens")
        completion = payload.get("completion_tokens")
        # 旧事件无方向拆分：全记 prompt（chat 输入价低于输出价，保守不夸大节省）
        if prompt is None:
            prompt, completion = tokens, 0
        cost = cost_cny(model, prompt, completion)

        agg = by_model.setdefault(
            model,
            {
                "model": model,
                "role": MODEL_ROLE.get(model, model),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "tokens": 0,
                "cost_cny": 0.0,
                "calls": 0,
            },
        )
        agg["prompt_tokens"] += prompt
        agg["completion_tokens"] += completion
        agg["tokens"] += tokens
        agg["cost_cny"] += cost
        agg["calls"] += 1
        total_prompt += prompt
        total_completion += completion

        day = (created_at or datetime.now(UTC)).astimezone().strftime("%Y-%m-%d")
        day_agg = daily.setdefault(day, {"cost_cny": 0.0, "tokens": 0, "by_model": {}})
        day_agg["cost_cny"] += cost
        day_agg["tokens"] += tokens
        day_agg["by_model"][model] = day_agg["by_model"].get(model, 0.0) + cost

    total_cost = _round2(sum(m["cost_cny"] for m in by_model.values()))
    for m in by_model.values():
        m["cost_cny"] = round(m["cost_cny"], 4)

    # ---- 日期轴补齐：范围内空天补 0，保证趋势曲线横跨完整时间窗 ----
    today = datetime.now(UTC).astimezone().date()
    if days > 0:
        start = today - timedelta(days=days - 1)
        span = days
    elif daily:
        start = datetime.strptime(min(daily), "%Y-%m-%d").date()
        span = (today - start).days + 1
    else:
        start, span = None, 0
    if start is not None:
        dates = [(start + timedelta(days=i)).isoformat() for i in range(span)]
        for d in dates:
            if d > today.isoformat():
                break
            daily.setdefault(d, {"cost_cny": 0.0, "tokens": 0, "by_model": {}})

    # ---- 缓存：命中率与节省金额（行数少，Python 聚合，避免参数化 JSONB 分组的方言坑） ----
    cache_conditions = [AgentEvent.type.in_(CACHE_EVENT_TYPES)]
    if since:
        cache_conditions.append(AgentEvent.created_at >= since)
    cache_rows = (
        await session.execute(select(AgentEvent.type, AgentEvent.payload).where(*cache_conditions))
    ).all()
    cache_hits = cache_total = search_hits = 0
    for ev_type, payload in cache_rows:
        cache_total += 1
        if (payload or {}).get("provider") == "cache":
            cache_hits += 1
            if ev_type == EventType.search:
                search_hits += 1

    saved_cny = _round2(search_hits * get_settings().bocha_price_per_call)
    hit_rate = _round2(cache_hits / cache_total * 100) if cache_total else None

    # ---- 任务：次数 + 按档均值 + 下钻列表 ----
    task_conditions = []
    if since:
        task_conditions.append(ResearchTask.created_at >= since)
    task_rows = (
        (
            await session.execute(
                select(ResearchTask).where(*task_conditions).order_by(ResearchTask.id.desc())
            )
        )
        .scalars()
        .all()
    )

    avg_by_depth = {}
    for depth in DEPTHS:
        done_costs = [
            t.cost_cny for t in task_rows if t.depth == depth and t.status == TaskStatus.done
        ]
        if done_costs:
            avg_by_depth[depth] = {
                "count": len(done_costs),
                "avg_cost_cny": _round2(sum(done_costs) / len(done_costs)),
            }

    tasks = [
        {
            "id": t.id,
            "question": t.question,
            "depth": t.depth,
            "status": t.status.value if hasattr(t.status, "value") else str(t.status),
            "cost_cny": _round2(t.cost_cny),
            "token_used": t.token_used,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in task_rows[:50]
    ]

    has_data = bool(rows) or bool(task_rows)

    price_desc = "；".join(f"{m} 输入¥{p}/M·输出¥{c}/M" for m, (p, c) in PRICE_PER_M.items())
    return {
        "days": days,
        "summary": {
            "total_cost_cny": total_cost if has_data else None,
            "task_count": len(task_rows) if has_data else None,
            "total_tokens": (total_prompt + total_completion) if has_data else None,
            "prompt_tokens": total_prompt if has_data else None,
            "completion_tokens": total_completion if has_data else None,
        },
        "cache": {
            "hit_rate": hit_rate,
            "cache_hits": cache_hits if has_data else None,
            "total_calls": cache_total if has_data else None,
            "saved_cny": saved_cny if has_data else None,
        },
        "by_model": sorted(by_model.values(), key=lambda m: -m["cost_cny"]),
        "daily": [
            {
                "date": d,
                "cost_cny": _round2(v["cost_cny"]),
                "tokens": v["tokens"],
                "by_model": {m: round(c, 4) for m, c in v["by_model"].items()},
            }
            for d, v in sorted(daily.items())
        ],
        "avg_by_depth": avg_by_depth,
        "tasks": tasks,
        "pricing_note": (
            f"LLM 计价：{price_desc}；缓存节省按博查 ¥{get_settings().bocha_price_per_call}/次折算"
        ),
    }
