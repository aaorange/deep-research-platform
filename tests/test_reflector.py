"""reflector 单测：Reflection schema 一致性校验 + prompt 组装 + instructor 重试。

mock openai client（instructor.TOOLS 模式与 planner 同路），不发真实请求。
"""

import json
from unittest.mock import AsyncMock

import instructor
import pytest
from instructor.core import InstructorRetryException
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_tool_call import Function
from openai.types.completion_usage import CompletionUsage

from app.config import get_settings
from app.engine import reflector
from app.engine.reflector import (
    Reflection,
    SupplementaryTask,
    instruction_to_sub_task,
    reflect_on_coverage,
)


def tool_completion(payload, pt=400, ct=120):
    tool = ChatCompletionMessageToolCall(
        id="call-1",
        type="function",
        function=Function(name="Reflection", arguments=json.dumps(payload, ensure_ascii=False)),
    )
    message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[tool])
    return ChatCompletion(
        id="cmpl-1",
        model="deepseek-chat",
        object="chat.completion",
        created=1,
        choices=[Choice(index=0, message=message, finish_reason="tool_calls")],
        usage=CompletionUsage(prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct),
    )


def make_client(*side_effects):
    client = AsyncOpenAI(api_key="test", base_url="http://127.0.0.1:1")
    client.chat.completions.create = AsyncMock(side_effect=list(side_effects))
    return client


def make_inputs():
    sub_tasks = [
        {"id": 1, "title": "市场规模", "keywords": "规模", "status": "done", "round_no": 1},
        {"id": 2, "title": "政策监管", "keywords": "监管", "status": "failed", "round_no": 1},
    ]
    notes = [
        {"sub_task_id": 1, "title": "市场规模", "content": "2025 年规模 120 亿元 [11]。"},
    ]
    sources = [
        {"id": 11, "url": "https://a.com/1", "title": "行业报告", "domain": "a.com"},
        {"id": 12, "url": "https://b.com/2", "title": "数据", "domain": "b.com"},
    ]
    return sub_tasks, notes, sources


def test_reflection_schema_rejects_inconsistent():
    with pytest.raises(ValueError, match="至少一个补搜子问题"):
        Reflection(has_gaps=True, assessment="缺数据", gaps=[])
    with pytest.raises(ValueError, match="不应给出补搜子问题"):
        Reflection(
            has_gaps=False,
            assessment="足够",
            gaps=[SupplementaryTask(title="t", keywords="k")],
        )
    with pytest.raises(ValueError, match="最多"):
        Reflection(
            has_gaps=True,
            assessment="缺",
            gaps=[SupplementaryTask(title=f"t{i}", keywords="k") for i in range(4)],
        )


def test_instruction_to_sub_task_truncates():
    st = instruction_to_sub_task("重点补充 2025 年成本数据 " * 30)
    assert len(st["title"]) <= 200
    assert len(st["keywords"]) <= 500


async def test_reflect_no_gaps(monkeypatch):
    sub_tasks, notes, sources = make_inputs()
    client = make_client(tool_completion({"has_gaps": False, "assessment": "材料充分", "gaps": []}))
    monkeypatch.setattr(reflector, "get_instructor", lambda: instructor.from_openai(client))

    reflection, usage = await reflect_on_coverage("问题", None, sub_tasks, notes, sources)

    assert reflection.has_gaps is False
    assert reflection.gaps == []
    assert usage.total_tokens == 520

    prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "检查清单" in prompt
    assert "市场规模" in prompt
    assert "[failed] 政策监管（执行失败）" in prompt
    assert "共 2 条" in prompt


async def test_reflect_with_gaps_returns_supplementary_tasks(monkeypatch):
    sub_tasks, notes, sources = make_inputs()
    payload = {
        "has_gaps": True,
        "assessment": "政策监管维度无笔记",
        "gaps": [{"title": "监管政策处罚案例", "keywords": "监管 处罚 案例"}],
    }
    client = make_client(tool_completion(payload))
    monkeypatch.setattr(reflector, "get_instructor", lambda: instructor.from_openai(client))

    reflection, usage = await reflect_on_coverage("问题", "背景", sub_tasks, notes, sources)

    assert reflection.has_gaps is True
    assert reflection.gaps[0].title == "监管政策处罚案例"
    assert usage.prompt_tokens == 400
    assert get_settings().llm_model_chat == client.chat.completions.create.call_args.kwargs["model"]


async def test_reflect_retries_on_inconsistent_payload(monkeypatch):
    """has_gaps=true 但 gaps 空 → validator 报错 → instructor reask 重试。"""
    sub_tasks, notes, sources = make_inputs()
    client = make_client(
        tool_completion({"has_gaps": True, "assessment": "缺", "gaps": []}),
        tool_completion(
            {"has_gaps": True, "assessment": "缺", "gaps": [{"title": "补搜", "keywords": "k"}]}
        ),
    )
    monkeypatch.setattr(reflector, "get_instructor", lambda: instructor.from_openai(client))

    reflection, usage = await reflect_on_coverage("问题", None, sub_tasks, notes, sources)

    assert client.chat.completions.create.call_count == 2
    assert reflection.gaps[0].title == "补搜"
    # 重试的失败调用也计费（instructor 跨重试累计 usage）
    assert usage.total_tokens == 2 * (400 + 120)


async def test_reflect_all_retries_fail_raises(monkeypatch):
    sub_tasks, notes, sources = make_inputs()
    client = make_client(*[tool_completion({"has_gaps": True, "assessment": "缺", "gaps": []})] * 3)
    monkeypatch.setattr(reflector, "get_instructor", lambda: instructor.from_openai(client))

    with pytest.raises(InstructorRetryException):
        await reflect_on_coverage("问题", None, sub_tasks, notes, sources)
    assert client.chat.completions.create.call_count >= 2
