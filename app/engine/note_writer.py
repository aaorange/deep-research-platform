"""子任务压缩笔记：instructor 约束 schema，DeepSeek 生成。

材料页按 [N] 编号喂给模型，笔记中的 [N] 引用锚点与 sources.idx 一一对应。
目标：整份笔记（summary+facts+gaps）控制在 800 token 内（约 500 汉字 + 10 条事实）。
"""

from app.config import get_settings
from app.engine.schemas import NoteUsage, WorkerNote
from app.llm.client import SCHEMA_RETRIES, get_instructor

NOTE_SYSTEM = "你是一名严谨的研究助理，只基于给定材料写压缩笔记，不编造事实，不引用不存在的序号。"

MATERIAL_BLOCK = "[{idx}] {title}（域名信誉 {credibility}/5，发布 {date}）\n{text}"

NOTE_PROMPT = """针对子任务「{title}」，基于以下网页材料撰写压缩笔记：

{materials}

要求（总预算 800 token，宁缺毋滥）：
1. summary：120-250 字核心发现；只陈述材料支持的事实，每个事实句末标注引用序号 [N]；保留矛盾结论并注明来源分歧
2. facts：关键事实与数据点，最多 8 条，每条带 citation；**数字锚点**：凡出现数量、金额、比例、增速、日期，必须保留材料原始数字（如「97.2%」「120 家」「2025 年 3 月」），禁止四舍五入或改写为「多数」「大量」
3. gaps：材料未覆盖、值得后续补充的具体问题，最多 3 条"""  # noqa: E501


async def write_note(title: str, pages: list[dict]) -> tuple[WorkerNote, NoteUsage | None]:
    materials = "\n\n".join(
        MATERIAL_BLOCK.format(
            idx=p["idx"],
            title=p["title"],
            credibility=p["credibility"],
            date=p.get("published_date") or "未知",
            text=p["text"],
        )
        for p in pages
    )
    prompt = NOTE_PROMPT.format(title=title, materials=materials)

    client = get_instructor()
    note, completion = await client.chat.completions.create_with_completion(
        model=get_settings().llm_model_chat,
        response_model=WorkerNote,
        messages=[
            {"role": "system", "content": NOTE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_retries=SCHEMA_RETRIES,
    )
    usage = completion.usage
    if usage is None:
        return note, None
    return note, NoteUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )
