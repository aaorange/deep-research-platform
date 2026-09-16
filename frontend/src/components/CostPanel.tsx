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
    if (used === prevUsed.current) return;
    const up = used > prevUsed.current;
    prevUsed.current = used;
    if (!up) return;
    setFlash(true);
    const t = setTimeout(() => setFlash(false), 700);
    return () => clearTimeout(t);
  }, [used]);

  const pct = budget > 0 ? Math.min(100, (used / budget) * 100) : 0;
  const meterCls = pct >= 80 ? "over" : pct >= 60 ? "warn" : "";

  return (
    <div className="cost">
      <div className="lbl">
        <span>Token 消耗</span>
        <span>预算 {budget.toLocaleString()}</span>
      </div>
      <div className={`num ${flash ? "flash" : ""}`}>
        {used.toLocaleString()}
        <small>tokens</small>
      </div>
      <div className="meter">
        <div className={`fill ${meterCls}`} style={{ width: `${pct}%` }} />
        <div className="th" />
        <span className="cap">80%</span>
      </div>
      <div className="sub-l">
        {budget === 0 ? (
          <span>无预算限制</span>
        ) : degraded ? (
          <span className="degrade-note">⚠ 已触发预算降级</span>
        ) : (
          <span>
            {pct.toFixed(0)}% · 距降级线 {Math.max(0, 80 - pct).toFixed(0)}%
          </span>
        )}
        <b>¥ {(detail?.cost_cny ?? 0).toFixed(2)}</b>
      </div>
    </div>
  );
}
