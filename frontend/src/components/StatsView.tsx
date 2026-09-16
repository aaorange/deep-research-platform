import { useEffect, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { getStats } from "../api";
import type { StatsOut } from "../types";

echarts.use([BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const RANGES = [
  { days: 7, label: "近 7 天" },
  { days: 30, label: "近 30 天" },
  { days: 365, label: "近一年" },
] as const;

const DEPTH_LABEL: Record<string, string> = { quick: "快速", std: "标准", deep: "深度" };
const DEPTHS = ["quick", "std", "deep"] as const;
const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "研究中",
  paused: "已暂停",
  done: "完成",
  failed: "失败",
  canceled: "已取消",
  stopped: "已终止",
};
const TEXT2 = "#5c6470";
const PALETTE = ["#2563eb", "#7c3aed", "#16a34a", "#d97706"];

function fmtTokens(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function fmtCny(n: number | null): string {
  return n == null ? "—" : `¥ ${n.toFixed(2)}`;
}

function useChart<T extends HTMLElement>(build: (el: T) => echarts.EChartsCoreOption, dep: unknown) {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const chart = echarts.init(el);
    chart.setOption(build(el));
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dep]);
  return ref;
}

function Card({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export function StatsView({ onBack }: { onBack: () => void }) {
  const [days, setDays] = useState<number>(7);
  const [data, setData] = useState<StatsOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    getStats(days)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e instanceof Error ? e.message : "加载失败"));
    return () => {
      alive = false;
    };
  }, [days]);

  const modelRef = useChart<HTMLDivElement>(
    () => ({
      color: PALETTE,
      tooltip: { trigger: "axis", textStyle: { fontSize: 12 } },
      grid: { left: 8, right: 24, top: 8, bottom: 0, containLabel: true },
      xAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#f0f1f3" } },
        axisLabel: {
          color: TEXT2,
          fontSize: 11,
          formatter: (v: number) => `¥${v}`,
        },
      },
      yAxis: {
        type: "category",
        data: (data?.by_model ?? []).map((m) => `${m.model}（${m.role}）`),
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { color: TEXT2, fontSize: 11.5 },
      },
      series: [
        {
          type: "bar",
          data: (data?.by_model ?? []).map((m) => m.cost_cny),
          barMaxWidth: 22,
          itemStyle: { borderRadius: [0, 4, 4, 0] },
          label: {
            show: true,
            position: "right",
            color: TEXT2,
            fontSize: 11,
            formatter: (p: { value: number }) => `¥${p.value}`,
          },
        },
      ],
    }),
    data?.by_model,
  );

  const daily = data?.daily ?? [];
  const modelNames = (data?.by_model ?? []).map((m) => m.model);
  const singleDay = daily.length === 1;
  const trendRef = useChart<HTMLDivElement>(
    () => ({
      color: [...PALETTE, "#16a34a"],
      tooltip: { trigger: "axis", textStyle: { fontSize: 12 } },
      legend: {
        bottom: 0,
        left: "center",
        itemWidth: 14,
        itemHeight: 8,
        icon: "roundRect",
        textStyle: { color: TEXT2, fontSize: 11.5 },
      },
      grid: { left: 8, right: 8, top: 22, bottom: 34, containLabel: true },
      xAxis: {
        type: "category",
        data: daily.map((p) => p.date.slice(5)),
        boundaryGap: false,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: "#e8eaed" } },
        axisLabel: { color: TEXT2, fontSize: 11 },
      },
      yAxis: [
        {
          type: "value",
          splitLine: { lineStyle: { color: "#f0f1f3" } },
          axisLabel: { color: TEXT2, fontSize: 11, formatter: (v: number) => `¥${v}` },
        },
        {
          type: "value",
          splitLine: { show: false },
          axisLabel: { color: TEXT2, fontSize: 11, formatter: (v: number) => fmtTokens(v) },
        },
      ],
      series: [
        ...modelNames.map((name) => ({
          name,
          type: "line" as const,
          smooth: true,
          symbolSize: singleDay ? 7 : 4,
          data: daily.map((p) => p.by_model?.[name] ?? 0),
          areaStyle: { opacity: 0.08 },
        })),
        {
          name: "Token 总量",
          type: "line" as const,
          yAxisIndex: 1,
          smooth: true,
          symbol: "none",
          lineStyle: { type: "dashed", width: 1.5 },
          data: daily.map((p) => p.tokens),
        },
      ],
    }),
    daily,
  );

  if (error) {
    return (
      <div className="stats-view">
        <div className="stats-error">看板加载失败：{error}</div>
      </div>
    );
  }

  const s = data?.summary;
  const cache = data?.cache;
  const depthRows = DEPTHS.map((d) => {
    const v = data?.avg_by_depth?.[d];
    return {
      label: DEPTH_LABEL[d],
      cost: v ? `¥${v.avg_cost_cny.toFixed(2)}` : "—",
      count: v?.count ?? 0,
    };
  });

  return (
    <div className="stats-view">
      <div className="stats-head">
        <div className="stats-head-left">
          <button className="btn stats-back" onClick={onBack} title="返回研究工作台">
            ← 返回
          </button>
          <div className="stats-title">成本看板</div>
        </div>
        <div className="range-switch">
          {RANGES.map((r) => (
            <button
              key={r.days}
              className={`range-btn ${days === r.days ? "on" : ""}`}
              onClick={() => setDays(r.days)}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      <div className="stat-cards">
        <Card
          label="总成本"
          value={fmtCny(s?.total_cost_cny ?? null)}
          sub={s?.task_count != null ? `${s.task_count} 次研究` : undefined}
        />
        <Card
          label="缓存节省"
          value={fmtCny(cache?.saved_cny ?? null)}
          sub={
            cache?.hit_rate != null
              ? `命中率 ${cache.hit_rate}%（${cache.cache_hits}/${cache.total_calls} 次调用）`
              : undefined
          }
        />
        <div className="stat-card">
          <div className="stat-label">平均单次研究</div>
          <div className="depth-rows">
            {depthRows.map((r) => (
              <div className="depth-row" key={r.label}>
                <span className="d">{r.label}</span>
                <span className="v">{r.cost}</span>
                <span className="c">{r.count > 0 ? `×${r.count}` : ""}</span>
              </div>
            ))}
          </div>
          <div className="stat-sub">按深度档分别统计</div>
        </div>
        <Card
          label="Token 总量"
          value={fmtTokens(s?.total_tokens ?? null)}
          sub={
            s?.prompt_tokens != null
              ? `输入 ${fmtTokens(s.prompt_tokens)} · 输出 ${fmtTokens(s.completion_tokens)}`
              : undefined
          }
        />
      </div>

      <div className="stats-charts">
        <div className="stats-panel">
          <div className="stats-panel-h">按模型拆分的成本构成</div>
          <div ref={modelRef} className="stats-chart" />
          {(data?.by_model.length ?? 0) === 0 && <div className="stats-empty">范围内暂无模型调用</div>}
        </div>
        <div className="stats-panel">
          <div className="stats-panel-h">每日成本趋势</div>
          <div ref={trendRef} className="stats-chart" />
          {(data?.daily.length ?? 0) === 0 && <div className="stats-empty">范围内暂无消耗记录</div>}
        </div>
      </div>

      <div className="stats-panel tasks-panel">
        <div className="stats-panel-h">
          研究任务（指标构成明细）<span className="panel-h-sub">最近 {(data?.tasks ?? []).slice(0, 10).length} 条</span>
        </div>
        <table className="stats-tasks">
          <thead>
            <tr>
              <th>问题</th>
              <th>深度</th>
              <th>状态</th>
              <th className="num">成本</th>
              <th className="num">Token</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {(data?.tasks ?? []).slice(0, 10).map((t) => (
              <tr key={t.id}>
                <td className="q" title={t.question}>{t.question}</td>
                <td>{DEPTH_LABEL[t.depth] ?? t.depth}</td>
                <td>
                  <span className={`tst ${t.status}`}>{STATUS_LABEL[t.status] ?? t.status}</span>
                </td>
                <td className="num">¥ {t.cost_cny.toFixed(2)}</td>
                <td className="num">{fmtTokens(t.token_used)}</td>
                <td className="time">
                  {t.created_at ? t.created_at.slice(0, 16).replace("T", " ") : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {(data?.tasks.length ?? 0) === 0 && <div className="stats-empty">范围内暂无任务</div>}
      </div>

      <div className="stats-note">{data?.pricing_note}</div>
    </div>
  );
}
