import { useEffect, useRef } from "react";
import type { AgentEvent } from "../types";

const TYPE_LABEL: Record<string, string> = {
  plan: "规划",
  search: "搜索",
  fetch: "抓取",
  note: "笔记",
  reflect: "反思",
  degrade: "降级",
  budget: "预算",
  synthesize: "综合",
  control: "控制",
};

function fmtTime(iso: string | null): string {
  if (!iso) return "--:--:--";
  const d = new Date(iso);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function shorten(s: unknown, n = 60): string {
  const str = String(s ?? "");
  return str.length > n ? str.slice(0, n) + "…" : str;
}

/** 从 payload 提取一行人类可读摘要 */
export function eventDesc(ev: AgentEvent): string {
  const p = ev.payload as Record<string, unknown>;
  switch (ev.type) {
    case "plan":
      return p.sub_task_count !== undefined
        ? `生成 ${p.sub_task_count} 个子任务（${p.model ?? "deepseek"}）`
        : p.info
          ? String(p.info)
          : "大纲规划完成";
    case "search":
      return `「${shorten(p.query, 40)}」命中 ${p.hits} 条（${p.provider}）`;
    case "fetch":
      return `${shorten(p.title ?? p.url, 48)} · ${p.domain ?? ""} 可信度${p.credibility ?? "-"}/5`;
    case "note":
      return `笔记 ${p.summary_chars} 字 · ${p.facts} 个事实 · ${p.gaps ?? 0} 缺口`;
    case "reflect":
      return p.evaluation
        ? shorten(p.evaluation, 80)
        : p.round !== undefined
          ? `第 ${p.round} 轮反思`
          : "反思完成";
    case "degrade":
      if (p.stage) return `预算降级：${p.action ?? ""}（已用 ${p.used ?? "?"} / ${p.budget ?? "?"}）`;
      return `抓取降级 ${p.from} → ${p.to ?? "失败"}：${shorten(p.url, 40)}`;
    case "budget":
      return `${p.stage} 阶段 ${Number(p.used ?? 0).toLocaleString()} / ${Number(p.budget ?? 0).toLocaleString()}（配额 ${p.quota}）${p.degraded ? " ⚠已降级" : ""}`;
    case "synthesize":
      return p.report_chars !== undefined
        ? `报告 ${p.report_chars} 字 · ${p.citations ?? 0} 个引用锚点`
        : "报告综合完成";
    case "control":
      return p.error ? `⚠ ${p.stage}: ${shorten(p.error, 60)}` : `${p.stage ?? ""} ${p.info ?? ""}`.trim() || "控制信号";
    default:
      return shorten(JSON.stringify(p), 60);
  }
}

export function ActionStream({ events }: { events: AgentEvent[] }) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [events.length]);

  if (events.length === 0) {
    return (
      <div className="stream">
        <div className="empty">暂无动作——创建任务后实时事件流将在此滚动</div>
      </div>
    );
  }

  return (
    <div className="stream">
      {events.map((ev) => (
        <div key={ev.seq} className={`event-row ${ev.type}`}>
          <span className="t">{fmtTime(ev.created_at)}</span>
          <span className={`et ${ev.type}`}>{TYPE_LABEL[ev.type] ?? ev.type}</span>
          <span className="desc" title={eventDesc(ev)}>
            {eventDesc(ev)}
          </span>
          {ev.tokens ? (
            <span className="tok" title="本次 LLM 调用 token">
              +{ev.tokens.toLocaleString()}tok
            </span>
          ) : null}
          {ev.latency_ms ? <span className="tok">{(ev.latency_ms / 1000).toFixed(1)}s</span> : null}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
