"""报告综合：deepseek-reasoner 基于 W1 笔记产出带引用的 markdown 报告。

笔记正文中的 [N] 锚点已被 persister 重写为信源库 id；报告沿用同一编号体系。
生成后按首次出现顺序重编号为 [1..M]，citation_map 记录展示编号→信源 id 的
映射（报告阅读页据此渲染信源卡）。锚点合法性由 pydantic validator 约束，
编造的引用编号会触发 instructor reask 重试。

图表（D16）：数据密集段落由 LLM 附带图表规格——markdown 中插 `<!-- chart:ID -->`
占位，charts 数组给出 {id, title, type, x, series}，落 reports.chart_specs，
前端与 HTML 导出用 ECharts 渲染。占位与规格双向一致由 validator 约束。
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.engine.schemas import NoteUsage
from app.llm.client import SCHEMA_RETRIES, get_reasoner_instructor

ANCHOR_RE = re.compile(r"\[(\d+)\]")
CHART_PLACEHOLDER_RE = re.compile(r"<!--\s*chart:(c\d+)\s*-->")

REPORT_SYSTEM = "你是一名资深研究分析师，撰写严谨、可溯源的研究报告。"

REPORT_PROMPT = """基于以下子任务笔记撰写最终研究报告，输出 JSON：{{"markdown": "<报告全文>", "charts": [<图表规格>]}}

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
6. [N] 编号必须且只能来自信源清单中存在的编号，禁止编造
7. 图表：当某段落出现 3 个及以上可对比的数据点（趋势、份额、对比）时，在数据段落末尾
   插入占位符 `<!-- chart:c1 -->`（编号从 c1 递增），并在 charts 数组给出规格：
   {{"id": "c1", "title": "<图表标题>", "type": "bar|line|pie", "x": ["<类目>..."],
   "series": [{{"name": "<系列名>", "data": [<数值>...]}}]}}。
   bar/line 可多系列；pie 恰好一个系列且 data 与 x 等长（扇区名取 x）；
   所有系列 data 长度必须与 x 一致，数值必须与笔记数字一致；
   无可对比数据时不输出任何图表（charts 为空数组）"""  # noqa: E501

NOTE_BLOCK = "### {title}\n{content}"
SOURCE_LINE = "[{id}] {title} — {domain}（可信度 {credibility}/5）"


class ChartSeries(BaseModel):
    name: str = Field(description="系列名（图例）")
    data: list[float] = Field(description="与 x 等长的数值序列")


class ChartSpec(BaseModel):
    """ECharts 图表规格：报告阅读页与 HTML 导出的渲染契约。"""

    id: str = Field(pattern=r"^c\d+$", description="占位符编号，如 c1")
    title: str
    type: Literal["bar", "line", "pie"]
    x: list[str] = Field(default_factory=list, description="类目轴（pie 留空）")
    series: list[ChartSeries] = Field(min_length=1)

    @model_validator(mode="after")
    def check_shape(self) -> "ChartSpec":
        if not self.x:
            raise ValueError(f"{self.type} 图需要非空 x 类目轴")
        n = len(self.x)
        if self.type == "pie" and len(self.series) != 1:
            raise ValueError("pie 图仅允许一个系列")
        for s in self.series:
            if len(s.data) != n:
                raise ValueError(f"系列「{s.name}」长度 {len(s.data)} 与类目轴长度 {n} 不一致")
        return self


class ReportDraft(BaseModel):
    """重编号后的报告草稿：markdown 用展示编号，citation_map 为展示编号→信源 id。"""

    markdown: str
    citation_map: dict[int, int]
    chart_specs: list[dict] = Field(default_factory=list)

    @property
    def n_citations(self) -> int:
        return len(self.citation_map)


def make_report_schema(valid_ids: set[int]) -> type[BaseModel]:
    """动态构造带锚点与图表校验的 response_model：非法输入触发 instructor reask。"""

    class ReportSchema(BaseModel):
        markdown: str = Field(description="研究报告 markdown 全文，论断带 [N] 信源编号标注")
        charts: list[ChartSpec] = Field(default_factory=list, description="数据段落图表规格")

        @model_validator(mode="after")
        def check_anchors(self) -> "ReportSchema":
            bad = {int(m) for m in ANCHOR_RE.findall(self.markdown)} - valid_ids
            if bad:
                raise ValueError(
                    f"引用编号 {sorted(bad)} 不在信源清单中；只能使用: {sorted(valid_ids)}"
                )
            return self

        @model_validator(mode="after")
        def check_charts(self) -> "ReportSchema":
            placed = CHART_PLACEHOLDER_RE.findall(self.markdown)
            spec_ids = [c.id for c in self.charts]
            if len(set(spec_ids)) != len(spec_ids):
                raise ValueError(f"图表 id 重复: {spec_ids}")
            orphan = set(placed) - set(spec_ids)
            if orphan:
                raise ValueError(f"markdown 中占位符 {sorted(orphan)} 缺少图表规格")
            missing = set(spec_ids) - set(placed)
            if missing:
                raise ValueError(f"图表规格 {sorted(missing)} 未在 markdown 中放置占位符")
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
    chart_specs = [c.model_dump() for c in report.charts]
    usage: Any = completion.usage
    if usage is None:
        return (
            ReportDraft(markdown=markdown, citation_map=citation_map, chart_specs=chart_specs),
            None,
        )
    return (
        ReportDraft(markdown=markdown, citation_map=citation_map, chart_specs=chart_specs),
        NoteUsage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        ),
    )
