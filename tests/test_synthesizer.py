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
from app.engine import synthesizer
from app.engine.schemas import NoteUsage
from app.engine.synthesizer import (
    ReportDraft,
    make_report_schema,
    renumber_citations,
    write_report,
)


def json_completion(payload, pt=3000, ct=20000):
    message = ChatCompletionMessage(
        role="assistant", content=json.dumps(payload, ensure_ascii=False)
    )
    return ChatCompletion(
        id="cmpl-1",
        model="deepseek-reasoner",
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
    notes = [
        {"sub_task_id": 1, "title": "市场规模", "content": "2025 年市场规模 120 亿元 [11]。"},
        {"sub_task_id": 2, "title": "竞争格局", "content": "头部三家占 70% 份额 [7][12]。"},
    ]
    sources = [
        {
            "id": 11,
            "url": "https://a.com/1",
            "title": "行业报告",
            "domain": "a.com",
            "credibility": 4,
        },
        {
            "id": 7,
            "url": "https://b.com/2",
            "title": "格局分析",
            "domain": "b.com",
            "credibility": 3,
        },
        {
            "id": 12,
            "url": "https://c.com/3",
            "title": "补充数据",
            "domain": "c.com",
            "credibility": 3,
        },
    ]
    return notes, sources


def test_renumber_first_appearance_order():
    md = "甲 [7] 乙 [12] 丙 [7] 丁 [11]"
    out, cmap = renumber_citations(md)
    assert out == "甲 [1] 乙 [2] 丙 [1] 丁 [3]"
    assert cmap == {1: 7, 2: 12, 3: 11}


def test_renumber_no_collision_swap():
    """[2] 先于 [1] 出现时不得被二次改写。"""
    out, cmap = renumber_citations("先 [2] 后 [1]")
    assert out == "先 [1] 后 [2]"
    assert cmap == {1: 2, 2: 1}


def test_renumber_ignores_non_anchor_brackets():
    out, cmap = renumber_citations("见 [附录A] 与 [注释]")
    assert out == "见 [附录A] 与 [注释]"
    assert cmap == {}


def test_schema_validator_rejects_invented_anchor():
    schema = make_report_schema({11, 7, 12})
    with pytest.raises(Exception, match="9"):
        schema.model_validate({"markdown": "编造 [9]"})
    ok = schema.model_validate({"markdown": "合法 [11][7]"})
    assert ok.markdown == "合法 [11][7]"


# ---- 图表规格（D16） ----


def chart_payload(**over):
    payload = {
        "id": "c1",
        "title": "市场规模",
        "type": "bar",
        "x": ["2023", "2024", "2025"],
        "series": [{"name": "亿元", "data": [42, 58, 82]}],
    }
    payload.update(over)
    return payload


def test_chart_spec_shape_validation():
    schema = make_report_schema({11})
    ok = schema.model_validate(
        {"markdown": "数据段 [11]\n<!-- chart:c1 -->", "charts": [chart_payload()]}
    )
    assert ok.charts[0].type == "bar"
    assert ok.charts[0].series[0].data == [42, 58, 82]

    # 系列长度与类目轴不一致
    with pytest.raises(Exception, match="不一致"):
        schema.model_validate(
            {"markdown": "<!-- chart:c1 -->", "charts": [chart_payload(x=["2023", "2024"])]}
        )
    # pie 只允许单系列
    with pytest.raises(Exception, match="pie"):
        schema.model_validate(
            {
                "markdown": "<!-- chart:c1 -->",
                "charts": [
                    chart_payload(
                        type="pie",
                        series=[
                            {"name": "a", "data": [1, 2, 3]},
                            {"name": "b", "data": [1, 2, 3]},
                        ],
                    )
                ],
            }
        )
    # 空类目轴
    with pytest.raises(Exception, match="类目轴"):
        schema.model_validate({"markdown": "<!-- chart:c1 -->", "charts": [chart_payload(x=[])]})


def test_schema_validator_chart_placeholder_consistency():
    schema = make_report_schema({11})
    # 占位符无规格
    with pytest.raises(Exception, match="缺少图表规格"):
        schema.model_validate({"markdown": "数据段 [11]\n<!-- chart:c1 -->", "charts": []})
    # 规格无占位符
    with pytest.raises(Exception, match="未在 markdown 中放置占位符"):
        schema.model_validate({"markdown": "数据段 [11]", "charts": [chart_payload()]})
    # id 重复
    with pytest.raises(Exception, match="重复"):
        schema.model_validate(
            {
                "markdown": "<!-- chart:c1 --><!-- chart:c1 -->",
                "charts": [chart_payload(), chart_payload()],
            }
        )


async def test_write_report_passes_chart_specs(monkeypatch):
    notes, sources = make_inputs()
    client = make_client(
        json_completion(
            {
                "markdown": "# 报告\n趋势数据 [11]。\n<!-- chart:c1 -->",
                "charts": [
                    {
                        "id": "c1",
                        "title": "市场规模",
                        "type": "bar",
                        "x": ["2023", "2024", "2025"],
                        "series": [{"name": "亿元", "data": [42, 58, 82]}],
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(
        synthesizer,
        "get_reasoner_instructor",
        lambda: instructor.from_openai(client, mode=instructor.Mode.JSON),
    )

    draft, _ = await write_report("问题", None, notes, sources)
    assert draft.markdown.endswith("<!-- chart:c1 -->")
    assert draft.chart_specs == [
        {
            "id": "c1",
            "title": "市场规模",
            "type": "bar",
            "x": ["2023", "2024", "2025"],
            "series": [{"name": "亿元", "data": [42.0, 58.0, 82.0]}],
        }
    ]


async def test_write_report_happy_path(monkeypatch):
    notes, sources = make_inputs()
    client = make_client(
        json_completion({"markdown": "# 报告\n规模 120 亿元 [11]，格局 70% [7][12]。"})
    )
    monkeypatch.setattr(
        synthesizer,
        "get_reasoner_instructor",
        lambda: instructor.from_openai(client, mode=instructor.Mode.JSON),
    )

    draft, usage = await write_report("国产大模型医疗落地", "2026 视角", notes, sources)

    assert isinstance(draft, ReportDraft)
    assert draft.markdown == "# 报告\n规模 120 亿元 [1]，格局 70% [2][3]。"
    assert draft.citation_map == {1: 11, 2: 7, 3: 12}
    assert draft.n_citations == 3
    assert usage is not None
    assert isinstance(usage, NoteUsage)
    assert usage.total_tokens == 3000 + 20000
    # reasoner 模型名传入
    call = client.chat.completions.create.call_args.kwargs
    assert call["model"] == get_settings().llm_model_reasoner


async def test_write_report_retries_on_invented_anchor(monkeypatch):
    """编造引用编号 → validator 失败 → instructor reask 后成功。"""
    notes, sources = make_inputs()
    client = make_client(
        json_completion({"markdown": "编造引用 [99]"}),
        json_completion({"markdown": "正确 [11]"}),
    )
    monkeypatch.setattr(
        synthesizer,
        "get_reasoner_instructor",
        lambda: instructor.from_openai(client, mode=instructor.Mode.JSON),
    )

    draft, usage = await write_report("问题", None, notes, sources)

    assert client.chat.completions.create.call_count == 2
    assert draft.markdown == "正确 [1]"
    assert draft.citation_map == {1: 11}
    # 重试的失败调用也计费（instructor 跨重试累计 usage）
    assert usage.total_tokens == 2 * (3000 + 20000)


async def test_write_report_all_retries_fail_raises(monkeypatch):
    notes, sources = make_inputs()
    client = make_client(*[json_completion({"markdown": f"始终编造 [{i}]"}) for i in range(3)])
    monkeypatch.setattr(
        synthesizer,
        "get_reasoner_instructor",
        lambda: instructor.from_openai(client, mode=instructor.Mode.JSON),
    )

    with pytest.raises(InstructorRetryException):
        await write_report("问题", None, notes, sources)
    assert client.chat.completions.create.call_count >= 2
