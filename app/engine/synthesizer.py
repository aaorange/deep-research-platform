"""报告综合：deepseek-reasoner 基于 W1 笔记产出带引用的 markdown 报告。

笔记正文中的 [N] 锚点已被 persister 重写为信源库 id；报告沿用同一编号体系。
生成后按首次出现顺序重编号为 [1..M]，citation_map 记录展示编号→信源 id 的
映射（报告阅读页据此渲染信源卡）。锚点合法性由 pydantic validator 约束，
编造的引用编号会触发 instructor reask 重试。
"""

import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.engine.schemas import NoteUsage
from app.llm.client import SCHEMA_RETRIES, get_reasoner_instructor

ANCHOR_RE = re.compile(r"\[(\d+)\]")

REPORT_SYSTEM = "你是一名资深研究分析师，撰写严谨、可溯源的研究报告。"

REPORT_PROMPT = """基于以下子任务笔记撰写最终研究报告，输出 JSON：{{"markdown": "<报告全文>"}}

主问题：{question}
研究背景：{background}

## 子任务笔记（[N] 为信源编号，引用时必须原样保留）

{notes}

## 信源清单

{sources}

撰写要求：
1. 结构：# 报告标题；标题后一段摘要（100-200 字）；## 各章节按主题组织（不按子任务罗列）；## 结论与展望
2. 每个关键论断句末标注信源编号 [N]；同一论断多信源可并列 [N][M]
3. 数字忠实：数量、金额、比例、日期必须与笔记原文一致，禁止四舍五入或改写
4. 只用上述笔记材料；矛盾结论如实呈现分歧并标注各自信源
5. 若笔记中存在未覆盖的方面，追加「## 信息缺口」章节如实列出，不允许无声遗漏
6. [N] 编号必须且只能来自信源清单中存在的编号，禁止编造"""  # noqa: E501

NOTE_BLOCK = "### {title}\n{content}"
SOURCE_LINE = "[{id}] {title} — {domain}（可信度 {credibility}/5）"


class ReportDraft(BaseModel):
    """重编号后的报告草稿：markdown 用展示编号，citation_map 为展示编号→信源 id。"""

    markdown: str
    citation_map: dict[int, int]

    @property
    def n_citations(self) -> int:
        return len(self.citation_map)


def make_report_schema(valid_ids: set[int]) -> type[BaseModel]:
    """动态构造带锚点校验的 response_model：非法编号触发 instructor reask。"""

    class ReportSchema(BaseModel):
        markdown: str = Field(description="研究报告 markdown 全文，论断带 [N] 信源编号标注")

        @model_validator(mode="after")
        def check_anchors(self) -> "ReportSchema":
            bad = {int(m) for m in ANCHOR_RE.findall(self.markdown)} - valid_ids
            if bad:
                raise ValueError(
                    f"引用编号 {sorted(bad)} 不在信源清单中；只能使用: {sorted(valid_ids)}"
                )
            return self

    return ReportSchema


def renumber_citations(markdown: str) -> tuple[str, dict[int, int]]:
    """信源库 id 锚点 → 按首次出现顺序的展示编号 [1..M]。

    单次遍历 + 回调替换，避免 [2]→[1] 后再被 [1]→[2] 二次改写的碰撞。
    """

    seen: dict[int, int] = {}

    def repl(m: re.Match) -> str:
        sid = int(m.group(1))
        if sid not in seen:
            seen[sid] = len(seen) + 1
        return f"[{seen[sid]}]"

    return ANCHOR_RE.sub(repl, markdown), {n: sid for sid, n in seen.items()}


async def write_report(
    question: str, background: str | None, notes: list[dict], sources: list[dict]
) -> tuple[ReportDraft, NoteUsage | None]:
    valid_ids = {s["id"] for s in sources}
    prompt = REPORT_PROMPT.format(
        question=question,
        background=background or "无",
        notes="\n\n".join(NOTE_BLOCK.format(title=n["title"], content=n["content"]) for n in notes),
        sources="\n".join(
            SOURCE_LINE.format(
                id=s["id"],
                title=s.get("title") or s["url"],
                domain=s.get("domain") or "未知域名",
                credibility=s.get("credibility") if s.get("credibility") is not None else "-",
            )
            for s in sources
        ),
    )

    client = get_reasoner_instructor()
    report, completion = await client.chat.completions.create_with_completion(
        model=get_settings().llm_model_reasoner,
        response_model=make_report_schema(valid_ids),
        messages=[
            {"role": "system", "content": REPORT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_retries=SCHEMA_RETRIES,
    )

    markdown, citation_map = renumber_citations(report.markdown)
    usage: Any = completion.usage
    if usage is None:
        return ReportDraft(markdown=markdown, citation_map=citation_map), None
    return ReportDraft(markdown=markdown, citation_map=citation_map), NoteUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )
