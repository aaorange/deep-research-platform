"""CLI 动作流打印：事件落库的同时打印一行人类可读描述。

并行 worker 各持一个实例，行首带子任务编号前缀；print 加 flush 保证
并行输出不串行缓冲。
"""

from app.db import EventType
from app.services.event_recorder import EventRecorder


def _fmt_ms(ms: int | None) -> str:
    return f"{ms}ms" if ms is not None else "-"


class PrintingRecorder(EventRecorder):
    ICONS = {
        EventType.plan: "🧭",
        EventType.search: "🔎",
        EventType.fetch: "📄",
        EventType.degrade: "⚠️ ",
        EventType.note: "📝",
        EventType.reflect: "🔍",
        EventType.budget: "💰",
        EventType.synthesize: "📊",
        EventType.control: "⛔",
    }

    async def record(self, task_id, type, payload=None, **kwargs):
        event = await super().record(task_id, type, payload, **kwargs)
        prefix = f"[子任务{kwargs['sub_task_id']}] " if kwargs.get("sub_task_id") else ""
        line = self._describe(type, payload or {})
        if kwargs.get("latency_ms"):
            line += f"（{_fmt_ms(kwargs['latency_ms'])}）"
        print(f"{prefix}{self.ICONS.get(type, '·')} {line}", flush=True)
        return event

    @staticmethod
    def _describe(type: EventType, payload: dict) -> str:
        if type == EventType.plan:
            fallback = "（默认模板兜底）" if payload.get("fallback") else ""
            return f"大纲生成：{payload.get('count')} 个子任务{fallback}——" + "；".join(
                str(t)[:24] for t in (payload.get("titles") or [])[:3]
            )
        if type == EventType.search:
            return (
                f"搜索「{payload.get('query')}」via {payload.get('provider')}，"
                f"{payload.get('hits')} 条结果"
            )
        if type == EventType.fetch:
            return (
                f"读取 [{payload.get('domain')}] {str(payload.get('title'))[:40]}"
                f"（信誉 {payload.get('credibility')}/5）"
            )
        if type == EventType.degrade:
            to = payload.get("to") or "放弃"
            return f"读页降级 {payload.get('from')}→{to}：{str(payload.get('error'))[:50]}"
        if type == EventType.note:
            gaps = payload.get("gaps") or []
            return (
                f"笔记生成：{payload.get('summary_chars')} 字 / "
                f"{payload.get('facts')} 条事实 / {len(gaps)} 条缺口"
            )
        if type == EventType.control:
            if payload.get("stage") == "execute":
                return (
                    f"execute 汇总：跑 {payload.get('ran')} / 成功 {payload.get('ok')}"
                    f" / 失败 {payload.get('failed')}"
                )
            return f"控制事件：{payload.get('stage')} {payload.get('error') or payload.get('info')}"
        if type == EventType.synthesize:
            return (
                f"报告生成 #{payload.get('report_id')}：{payload.get('chars')} 字 / "
                f"{payload.get('citations')} 个引用 / 基于 {payload.get('notes')} 份笔记"
            )
        return str(payload)[:60]
