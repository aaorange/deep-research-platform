"""研究大纲生成：instructor 约束 schema + 深度档位 + 默认三段式兜底。

深度档位（quick/std/deep → 3/5/8 个子任务）与 token 预算是全平台共用的档位常量，
API 层与引擎层均从此处引用。write_plan 失败时由主图 plan 节点捕获并走 default_outline。
"""

from pydantic import BaseModel, Field

from app.config import get_settings
from app.engine.schemas import NoteUsage
from app.llm.client import SCHEMA_RETRIES, get_instructor

DEPTH_SUBTASK_COUNT = {"quick": 3, "std": 5, "deep": 8}
DEPTH_BUDGETS = {"quick": 30_000, "std": 80_000, "deep": 150_000}


class PlanSubTask(BaseModel):
    title: str = Field(description="子问题标题：一句具体、可独立检索的问题")
    keywords: str = Field(description="搜索引擎友好的检索关键词组合")


class PlanOutline(BaseModel):
    """研究大纲：N 个互不重叠、合起来完整覆盖主问题的子问题。"""

    sub_tasks: list[PlanSubTask]


PLAN_SYSTEM = "你是一名研究规划专家，擅长把复杂问题拆解为互不重叠、可独立检索的子问题。"

PLAN_PROMPT = """把主问题拆解为恰好 {count} 个子问题。

主问题：{question}
背景：{background}

要求：
1. 每个子问题独立可检索，含明确的对象/地域/时间限定，禁止空泛（如「相关分析」）
2. 子问题之间 MECE：不重叠，合起来完整覆盖主问题
3. keywords 是给搜索引擎看的查询词：去掉疑问句式，保留关键实体与限定词，多个词用空格分隔"""  # noqa: E501

# 默认兜底模板：LLM/schema 失败时按档位数量取前 N 段
DEFAULT_SECTIONS: list[tuple[str, str]] = [
    ("{q}的发展现状与整体规模", "现状 规模"),
    ("{q}的核心案例与典型实践", "案例 实践"),
    ("{q}的关键数据与量化指标", "数据 指标"),
    ("{q}的主要参与者与竞争格局", "参与者 格局"),
    ("{q}的最新趋势与动态", "趋势 动态"),
    ("{q}面临的挑战与风险", "挑战 风险"),
    ("{q}的争议与不同观点", "争议 观点"),
    ("{q}的未来展望与发展路径", "展望 路径"),
]


def default_outline(question: str, count: int) -> PlanOutline:
    """schema 失败兜底：取默认前 count 段（count 上限 8）。"""
    count = min(count, len(DEFAULT_SECTIONS))
    return PlanOutline(
        sub_tasks=[
            PlanSubTask(title=tpl.format(q=question), keywords=f"{question} {suffix}")
            for tpl, suffix in DEFAULT_SECTIONS[:count]
        ]
    )


def _fit_count(outline: PlanOutline, question: str, expected: int) -> PlanOutline:
    """标题去重后裁剪/补齐到档位数量：多裁少补，补齐段来自默认模板。"""
    seen: set[str] = set()
    unique: list[PlanSubTask] = []
    for st in outline.sub_tasks:
        title = st.title.strip()
        if title and title not in seen:
            seen.add(title)
            unique.append(PlanSubTask(title=title, keywords=st.keywords))

    sub_tasks = unique[:expected]
    if len(sub_tasks) < expected:
        for st in default_outline(question, expected).sub_tasks:
            if len(sub_tasks) >= expected:
                break
            if st.title not in seen:
                seen.add(st.title)
                sub_tasks.append(st)
    return PlanOutline(sub_tasks=sub_tasks)


async def write_plan(
    question: str, background: str | None, depth: str
) -> tuple[PlanOutline, NoteUsage | None]:
    if depth not in DEPTH_SUBTASK_COUNT:
        raise ValueError(f"unknown depth: {depth!r}")

    count = DEPTH_SUBTASK_COUNT[depth]
    prompt = PLAN_PROMPT.format(count=count, question=question, background=background or "无")

    client = get_instructor()
    outline, completion = await client.chat.completions.create_with_completion(
        model=get_settings().llm_model_chat,
        response_model=PlanOutline,
        messages=[
            {"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_retries=SCHEMA_RETRIES,
    )

    usage = completion.usage
    fitted = _fit_count(outline, question, count)
    if usage is None:
        return fitted, None
    return fitted, NoteUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )
