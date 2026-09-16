import { useEffect, useRef, useState } from "react";
import type { AgentEvent, TaskDetail } from "../types";

/** 成本面板：token 计数随动作跳动（事件 tokens 累加 + budget 事件权威值校正）。 */
export function CostPanel({
  detail,
  events,
}: {
  detail: TaskDetail | null;
  events: AgentEvent[];
}) {
  const [flash, setFlash] = useState(false);
  const prevUsed = useRef(0);

  const lastBudget = [...events].reverse().find((e) => e.type === "budget");
  // 事件流累加为实时值；budget 事件（每阶段一条）权威校正，detail 拉取兜底
  const eventSum = events.reduce((acc, e) => acc + (e.tokens ?? 0), 0);
  const used =
    lastBudget != null
      ? Number((lastBudget.payload as Record<string, unknown>).used ?? 0)
      : Math.max(detail?.token_used ?? 0, eventSum);
  const budget = detail?.token_budget ?? 0;
  const degraded = events.some(
    (e) => e.type === "degrade" && (e.payload as Record<string, unknown>).stage != null
  );

  useEffect(() => {
    if (used > prevUsed.current) {
      setFlash(true);
      const t = setTimeout(() => setFlash(false), 800);
      return () => clearTimeout(t);
    }
    prevUsed.current = used;
  }, [used]);

  const pct = budget > 0 ? Math.min(100, (used / budget) * 100) : 0;
  const meterCls = pct >= 80 ? "over" : pct >= 60 ? "warn" : "";

  return (
    <div className="cost-panel">
      <div className="cost-row">
        <span>成本</span>
        <span>¥ {(detail?.cost_cny ?? 0).toFixed(2)}</span>
      </div>
      <div className={`cost-num ${flash ? "flash" : ""}`}>{used.toLocaleString()}</div>
      <div className="cost-row" style={{ color: "var(--text-dim)" }}>
        <span>tokens</span>
        <span>/ {budget.toLocaleString()}</span>
      </div>
      <div className="meter">
        <div className={`meter-fill ${meterCls}`} style={{ width: `${pct}%` }} />
        <div className="meter-threshold" title="80% 降级阈值" />
      </div>
      {degraded ? (
        <div className="degrade-note">⚠ 已触发预算降级（80% 阈值）</div>
      ) : (
        <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
          ｜ 80% 降级线 · {pct.toFixed(0)}%
        </div>
      )}
    </div>
  );
}
