import json
from unittest.mock import AsyncMock

import instructor
import pytest
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_tool_call import Function
from openai.types.completion_usage import CompletionUsage

from app.engine import planner
from app.engine.planner import (
    DEPTH_BUDGETS,
    DEPTH_SUBTASK_COUNT,
    default_outline,
    write_plan,
)


def fake_completion(payload, prompt_tokens=900, completion_tokens=120):
    tool = ChatCompletionMessageToolCall(
        id="call-1",
        type="function",
        function=Function(name="PlanOutline", arguments=json.dumps(payload, ensure_ascii=False)),
    )
    message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[tool])
    return ChatCompletion(
        id="cmpl-1",
        model="deepseek-chat",
        object="chat.completion",
        created=1,
        choices=[Choice(index=0, message=message, finish_reason="tool_calls")],
        usage=CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def make_client(*side_effects):
    client = AsyncOpenAI(api_key="test", base_url="http://127.0.0.1:1")
    client.chat.completions.create = AsyncMock(side_effect=list(side_effects))
    return client


def outline_payload(n, prefix="子问题"):
    return {
        "sub_tasks": [{"title": f"{prefix}{i}", "keywords": f"关键词{i}"} for i in range(1, n + 1)]
    }


def test_depth_constants():
    assert DEPTH_SUBTASK_COUNT == {"quick": 3, "std": 5, "deep": 8}
    assert DEPTH_BUDGETS["quick"] < DEPTH_BUDGETS["std"] < DEPTH_BUDGETS["deep"]


@pytest.mark.parametrize(("depth", "count"), [("quick", 3), ("std", 5), ("deep", 8)])
async def test_write_plan_three_depths(monkeypatch, depth, count):
    client = make_client(fake_completion(outline_payload(count)))
    monkeypatch.setattr(planner, "get_instructor", lambda: instructor.from_openai(client))

    outline, usage = await write_plan("国产大模型在医疗行业的落地情况", None, depth)

    assert len(outline.sub_tasks) == count
    assert all(st.title and st.keywords for st in outline.sub_tasks)
    assert usage is not None
    assert usage.prompt_tokens == 900
    assert usage.total_tokens == 900 + 120
    assert client.chat.completions.create.call_count == 1


async def test_write_plan_trims_over_count(monkeypatch):
    client = make_client(fake_completion(outline_payload(7)))  # quick 只需要 3
    monkeypatch.setattr(planner, "get_instructor", lambda: instructor.from_openai(client))

    outline, _ = await write_plan("问题", None, "quick")
    assert len(outline.sub_tasks) == 3


async def test_write_plan_pads_under_count(monkeypatch):
    client = make_client(fake_completion(outline_payload(2)))  # std 需要 5
    monkeypatch.setattr(planner, "get_instructor", lambda: instructor.from_openai(client))

    outline, _ = await write_plan("新能源车价格战", None, "std")

    assert len(outline.sub_tasks) == 5
    titles = [st.title for st in outline.sub_tasks]
    assert titles[:2] == ["子问题1", "子问题2"]  # LLM 结果保留在前
    assert "新能源车价格战的发展现状与整体规模" in titles  # 补齐段来自默认模板
    assert all(st.keywords for st in outline.sub_tasks)


async def test_write_plan_dedupes_duplicate_titles(monkeypatch):
    payload = {"sub_tasks": [outline_payload(1)["sub_tasks"][0]] * 4}
    client = make_client(fake_completion(payload))
    monkeypatch.setattr(planner, "get_instructor", lambda: instructor.from_openai(client))

    outline, _ = await write_plan("问题", None, "quick")
    assert len(outline.sub_tasks) == 3  # 去重后 1 + 默认补 2


async def test_write_plan_retries_on_schema_failure(monkeypatch):
    """第一次响应缺 keywords 字段 → pydantic 校验失败 → instructor 自动 reask 重试。"""
    bad = fake_completion({"sub_tasks": [{"title": "只有标题"}]})
    good = fake_completion(outline_payload(5))
    client = make_client(bad, good)
    monkeypatch.setattr(planner, "get_instructor", lambda: instructor.from_openai(client))

    outline, usage = await write_plan("问题", None, "std")

    assert len(outline.sub_tasks) == 5
    assert client.chat.completions.create.call_count == 2
    # 失败的第一次调用也计费：instructor 跨重试累计 usage
    assert usage.prompt_tokens == 2 * 900
    assert usage.total_tokens == 2 * (900 + 120)


async def test_write_plan_client_failure_raises(monkeypatch):
    client = make_client(RuntimeError("deepseek 503"))
    monkeypatch.setattr(planner, "get_instructor", lambda: instructor.from_openai(client))

    with pytest.raises(Exception, match="503"):
        await write_plan("问题", None, "std")


async def test_write_plan_unknown_depth_raises():
    with pytest.raises(ValueError, match="unknown depth"):
        await write_plan("问题", None, "ultra")


def test_default_outline_counts_and_distinct():
    for count in (3, 5, 8):
        outline = default_outline("AI 芯片竞争格局", count)
        assert len(outline.sub_tasks) == count
        titles = [st.title for st in outline.sub_tasks]
        assert len(set(titles)) == count
        assert all(st.keywords for st in outline.sub_tasks)
