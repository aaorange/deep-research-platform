import json
from unittest.mock import AsyncMock

import instructor
import pytest
from instructor.core import InstructorRetryException
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage

from app.config import get_settings
from app.services import chat as chat_service
from app.services.chat import ChatReply, make_chat_schema, write_chat_reply


def json_completion(payload, pt=1200, ct=300):
    message = ChatCompletionMessage(
        role="assistant", content=json.dumps(payload, ensure_ascii=False)
    )
    return ChatCompletion(
        id="cmpl-1",
        model="deepseek-chat",
        object="chat.completion",
        created=1,
        choices=[Choice(index=0, message=message, finish_reason="stop")],
        usage=CompletionUsage(prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct),
    )


def make_client(*side_effects):
    client = AsyncOpenAI(api_key="test", base_url="http://127.0.0.1:1")
    client.chat.completions.create = AsyncMock(side_effect=list(side_effects))
    return client


def make_inputs():
    notes = [{"sub_task_id": 1, "title": "市场规模", "content": "2025 年规模 120 亿元 [11]。"}]
    sources = [
        {
            "id": 11,
            "url": "https://a.com/1",
            "title": "行业报告",
            "domain": "a.com",
            "credibility": 4,
        }
    ]
    return notes, sources


def test_chat_reply_collects_cited_ids_in_order():
    reply = ChatReply(answer="结论甲 [11]，补充 [12]，再提 [11]。")
    assert reply.cited_ids == [11, 12]


def test_chat_reply_no_citations():
    assert ChatReply(answer="笔记未覆盖该方面。").cited_ids == []


def test_schema_validator_rejects_invented_anchor():
    schema = make_chat_schema({11})
    with pytest.raises(Exception, match="99"):
        schema.model_validate({"answer": "编造 [99]"})
    assert schema.model_validate({"answer": "合法 [11]"}).answer == "合法 [11]"


def patch_json_instructor(monkeypatch, client):
    """chat 生产路径走 TOOLS 模式；测试用 JSON 模式（wire format 差异不影响校验逻辑）。"""
    monkeypatch.setattr(
        chat_service,
        "get_instructor",
        lambda: instructor.from_openai(client, mode=instructor.Mode.JSON),
    )


async def test_write_chat_reply_happy_path(monkeypatch):
    notes, sources = make_inputs()
    client = make_client(json_completion({"answer": "2025 年规模 120 亿元 [11]。"}))
    patch_json_instructor(monkeypatch, client)

    reply, usage = await write_chat_reply(
        "大模型落地", notes, sources, [{"role": "user", "content": "上一问"}], "市场规模多大？"
    )

    assert reply.answer == "2025 年规模 120 亿元 [11]。"
    assert reply.cited_ids == [11]
    assert usage.total_tokens == 1500
    call = client.chat.completions.create.call_args.kwargs
    assert call["model"] == get_settings().llm_model_chat
    # 上下文含笔记与历史
    user_content = call["messages"][1]["content"]
    assert "2025 年规模 120 亿元 [11]" in user_content
    assert "上一问" in user_content
    assert "市场规模多大？" in user_content


async def test_write_chat_reply_retries_on_invented_anchor(monkeypatch):
    notes, sources = make_inputs()
    client = make_client(
        json_completion({"answer": "编造 [99]"}),
        json_completion({"answer": "修正 [11]"}),
    )
    patch_json_instructor(monkeypatch, client)

    reply, _ = await write_chat_reply("问题", notes, sources, [], "追问")
    assert client.chat.completions.create.call_count == 2
    assert reply.cited_ids == [11]


async def test_write_chat_reply_all_retries_fail(monkeypatch):
    notes, sources = make_inputs()
    client = make_client(*[json_completion({"answer": f"编造 [{i}]"}) for i in range(3)])
    patch_json_instructor(monkeypatch, client)

    with pytest.raises(InstructorRetryException):
        await write_chat_reply("问题", notes, sources, [], "追问")
