"""报告追问：基于已收集笔记的多轮问答（不联网、不触发新搜索）。

回答锚定信源库 id（笔记正文的 [N] 锚点体系），锚点合法性由 pydantic
validator 约束，编造引用触发 instructor reask。UI 据 cited_source_ids
标注信源范围并支持跳转信源卡。
"""

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.engine.schemas import NoteUsage
from app.engine.synthesizer import ANCHOR_RE, NOTE_BLOCK, SOURCE_LINE
from app.llm.client import SCHEMA_RETRIES, get_instructor

CHAT_SYSTEM = "你是研究助理，仅依据给定的研究笔记回答追问，不引入任何外部知识。"

CHAT_PROMPT = """基于以下研究笔记回答用户追问，输出 JSON：{{"answer": "<回答全文>"}}

主问题：{question}

## 研究笔记（[N] 为信源编号，引用时必须原样保留）

{notes}

## 信源清单

{sources}

## 已有对话

{history}

回答要求：
1. 直接回答追问，简洁准确，2-5 句为宜；涉及数字/结论时句末标注信源编号 [N]
2. 只用上述笔记材料；笔记未覆盖的内容如实回答「本次研究未覆盖该方面」，禁止编造
3. 不联网、不搜索：不提示用户去查询更多信息
4. [N] 编号必须且只能来自信源清单中存在的编号"""


class ChatReply(BaseModel):
    """追问回答：answer 带信源库 id 锚点，cited_ids 按出现顺序去重。"""

    answer: str
    cited_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def collect_citations(self) -> "ChatReply":
        seen: list[int] = []
        for m in ANCHOR_RE.findall(self.answer):
            sid = int(m)
            if sid not in seen:
                seen.append(sid)
        self.cited_ids = seen
        return self


def make_chat_schema(valid_ids: set[int]) -> type[BaseModel]:
    """动态构造带锚点校验的 response_model：非法编号触发 instructor reask。"""

    class ChatSchema(BaseModel):
        answer: str = Field(description="追问回答全文，论断带 [N] 信源编号标注")

        @model_validator(mode="after")
        def check_anchors(self) -> "ChatSchema":
            bad = {int(m) for m in ANCHOR_RE.findall(self.answer)} - valid_ids
            if bad:
                raise ValueError(
                    f"引用编号 {sorted(bad)} 不在信源清单中；只能使用: {sorted(valid_ids)}"
                )
            return self

    return ChatSchema


def _format_history(history: list[dict]) -> str:
    if not history:
        return "无"
    lines = []
    for m in history:
        who = "用户" if m["role"] == "user" else "研究助理"
        lines.append(f"{who}: {m['content']}")
    return "\n".join(lines)


async def write_chat_reply(
    question: str,
    notes: list[dict],
    sources: list[dict],
    history: list[dict],
    followup: str,
) -> tuple[ChatReply, NoteUsage | None]:
    """生成追问回答：上下文 = 主问题 + 全部笔记 + 信源清单 + 对话历史。"""
    valid_ids = {s["id"] for s in sources}
    prompt = CHAT_PROMPT.format(
        question=question,
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
        history=_format_history(history),
    )

    client = get_instructor()
    reply, completion = await client.chat.completions.create_with_completion(
        model=get_settings().llm_model_chat,
        response_model=make_chat_schema(valid_ids),
        messages=[
            {"role": "system", "content": CHAT_SYSTEM},
            {"role": "user", "content": prompt + f"\n\n## 用户追问\n{followup}"},
        ],
        max_retries=SCHEMA_RETRIES,
    )
    usage: Any = completion.usage
    answer = ChatReply(answer=reply.answer)  # 触发 cited_ids 收集
    if usage is None:
        return answer, None
    return answer, NoteUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )
