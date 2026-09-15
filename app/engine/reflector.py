"""反思节点 LLM 调用：checklist 约束的覆盖度评估与补搜子问题生成。

主图 reflect 节点在每轮 execute 后调用：把子任务执行情况与笔记材料交给
deepseek-chat 对照检查清单评估，产出 Reflection（缺口 + 补搜子问题）。
一致性由 pydantic validator 约束（has_gaps 与 gaps 必须匹配），违反触发
instructor reask 重试。

追加指示（用户中途补充的研究要求）不走 LLM：确定性转换为补搜子任务，
由 main_graph 的 reflect 节点直接落库。
"""

from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.engine.schemas import NoteUsage
from app.llm.client import SCHEMA_RETRIES, get_instructor

MAX_GAPS_PER_ROUND = 3

REFLECT_SYSTEM = (
    "你是一名严谨的研究质量审核员，对照检查清单评估研究材料的覆盖度，只补真正缺的口子。"
)

REFLECT_PROMPT = """评估以下研究材料是否足以撰写最终报告，输出结构化判定。

主问题：{question}
研究背景：{background}

## 检查清单（逐项核对后再给结论）

1. 【覆盖】每个子问题是否都有笔记，且笔记内容能回答该子问题？
2. 【充分】现有笔记能否支撑一篇回答主问题的报告——关键论断有数据或事实支撑？
3. 【缺口】笔记中是否存在明确的信息不足表述，或子问题的核心数据缺失？

## 子任务执行情况

{sub_task_lines}

## 子任务笔记

{note_blocks}

## 信源分布

{source_summary}

判定规则：
- 材料已足够回答主问题时 has_gaps 必须为 false——补搜消耗预算，不允许为补而补
- has_gaps=true 时给出最多 {max_gaps} 个补搜子问题：具体、可独立检索、不与已有子问题重复
- assessment 用一句话总结覆盖度结论"""

SUB_TASK_LINE = "- [{status}] {title}（{note_desc}）"
NOTE_BLOCK = "### {title}\n{content}"
NO_NOTE = "无笔记"


class SupplementaryTask(BaseModel):
    title: str = Field(description="补搜子问题标题：一句具体、可独立检索的问题")
    keywords: str = Field(description="检索关键词组合，搜索引擎友好")


class Reflection(BaseModel):
    """覆盖度评估结论：缺口判定与补搜子问题。"""

    has_gaps: bool = Field(description="是否存在需要补搜的信息缺口")
    assessment: str = Field(description="一句话覆盖度结论")
    gaps: list[SupplementaryTask] = Field(default_factory=list, description="补搜子问题，最多 3 个")

    @model_validator(mode="after")
    def check_consistency(self) -> "Reflection":
        if self.has_gaps and not self.gaps:
            raise ValueError("has_gaps=true 必须给出至少一个补搜子问题")
        if not self.has_gaps and self.gaps:
            raise ValueError("has_gaps=false 不应给出补搜子问题")
        if len(self.gaps) > MAX_GAPS_PER_ROUND:
            raise ValueError(f"补搜子问题最多 {MAX_GAPS_PER_ROUND} 个")
        return self


def instruction_to_sub_task(text: str) -> dict:
    """追加指示 → 补搜子任务（确定性转换，不经 LLM）。"""
    t = text.strip()
    return {"title": t[:200], "keywords": t[:500]}


def _sub_task_lines(sub_tasks: list[dict], notes: list[dict]) -> str:
    """子任务行：状态 + 是否有笔记及笔记长度；无笔记的 failed 单独标注错误。"""
    chars_by_sub = {}
    for n in notes:
        chars_by_sub[n.get("sub_task_id")] = len(n.get("content", ""))

    lines = []
    for st in sub_tasks:
        status = st.get("status", "pending")
        if st["id"] in chars_by_sub:
            note_desc = f"笔记 {chars_by_sub[st['id']]} 字"
        elif status == "failed":
            note_desc = "执行失败"
        else:
            note_desc = NO_NOTE
        lines.append(SUB_TASK_LINE.format(status=status, title=st["title"], note_desc=note_desc))
    return "\n".join(lines) or "（无子任务）"


def _source_summary(sources: list[dict]) -> str:
    total = len(sources)
    if not total:
        return "（无信源）"
    domains: dict[str, int] = {}
    for s in sources:
        d = s.get("domain") or "未知域名"
        domains[d] = domains.get(d, 0) + 1
    parts = ", ".join(f"{d} ×{c}" for d, c in sorted(domains.items(), key=lambda x: -x[1]))
    return f"共 {total} 条：{parts}"


async def reflect_on_coverage(
    question: str,
    background: str | None,
    sub_tasks: list[dict],
    notes: list[dict],
    sources: list[dict],
) -> tuple[Reflection, NoteUsage | None]:
    """对照检查清单评估覆盖度，返回 Reflection 与用量。"""
    prompt = REFLECT_PROMPT.format(
        question=question,
        background=background or "无",
        sub_task_lines=_sub_task_lines(sub_tasks, notes),
        note_blocks="\n\n".join(
            NOTE_BLOCK.format(title=n["title"], content=n["content"]) for n in notes
        )
        or "（无笔记）",
        source_summary=_source_summary(sources),
        max_gaps=MAX_GAPS_PER_ROUND,
    )

    client = get_instructor()
    reflection, completion = await client.chat.completions.create_with_completion(
        model=get_settings().llm_model_chat,
        response_model=Reflection,
        messages=[
            {"role": "system", "content": REFLECT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_retries=SCHEMA_RETRIES,
    )

    usage = completion.usage
    if usage is None:
        return reflection, None
    return reflection, NoteUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )
