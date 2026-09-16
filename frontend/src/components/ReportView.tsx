import { useEffect, useMemo, useRef, useState } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { exportUrl, getReport } from "../api";
import { ChatPanel } from "./ChatPanel";
import type { ChartSpec, ReportOut, ReportSource } from "../types";

echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const CITE_RE = /\[(\d+)\]/g;
const CHART_RE = /<!--\s*chart:(c\d+)\s*-->/g;
const DEPTH_LABEL: Record<string, string> = { quick: "快速", std: "标准", deep: "深度" };
const PALETTE = ["#2563eb", "#7c3aed", "#16a34a", "#d97706", "#dc2626", "#0891b2", "#db2777"];
const TEXT2 = "#5c6470";

/** markdown → 安全 HTML；先插图表容器再转 [N] 角标（保留 inline HTML）。 */
function renderMarkdown(md: string): string {
  const withCharts = md.replace(
    CHART_RE,
    (_m, id: string) => `<div class="chart-block" data-chart="${id}"><div class="chart-title"></div><div class="chart-canvas"></div></div>`,
  );
  const withCites = withCharts.replace(
    CITE_RE,
    (_m, n: string) => `<sup class="cite" data-n="${n}">${n}</sup>`,
  );
  return DOMPurify.sanitize(marked.parse(withCites, { gfm: true, async: false }), {
    ADD_ATTR: ["data-n", "data-chart"],
  });
}

function buildOption(spec: ChartSpec): echarts.EChartsCoreOption {
  if (spec.type === "pie") {
    return {
      color: PALETTE,
      tooltip: { trigger: "item", textStyle: { fontSize: 12 } },
      legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { color: TEXT2, fontSize: 11 } },
      series: [
        {
          type: "pie",
          radius: ["40%", "64%"],
          center: ["50%", "42%"],
          itemStyle: { borderRadius: 4, borderColor: "#fff", borderWidth: 2 },
          label: { color: TEXT2, fontSize: 11 },
          data: spec.x.map((name, i) => ({ name, value: spec.series[0].data[i] })),
        },
      ],
    };
  }
  const multi = spec.series.length > 1;
  return {
    color: PALETTE,
    tooltip: { trigger: "axis", textStyle: { fontSize: 12 } },
    legend: multi
      ? { top: 0, right: 0, itemWidth: 12, itemHeight: 8, textStyle: { color: TEXT2, fontSize: 11 } }
      : undefined,
    grid: { left: 8, right: 16, top: multi ? 34 : 16, bottom: 0, containLabel: true },
    xAxis: {
      type: "category",
      data: spec.x,
      axisTick: { show: false },
      axisLine: { lineStyle: { color: "#e8eaed" } },
      axisLabel: { color: TEXT2, fontSize: 11 },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#f0f1f3" } },
      axisLabel: { color: TEXT2, fontSize: 11 },
    },
    series: spec.series.map((s) => ({
      name: s.name,
      type: spec.type,
      data: s.data,
      smooth: spec.type === "line",
      symbolSize: spec.type === "line" ? 6 : undefined,
      barMaxWidth: 30,
      itemStyle: spec.type === "bar" ? { borderRadius: [3, 3, 0, 0] } : undefined,
    })),
  };
}

function chipCls(n: number | null): string {
  if (n == null) return "";
  if (n >= 4) return "c4";
  if (n >= 3) return "c3";
  return "";
}

export function ReportView({ taskId, onBack }: { taskId: number; onBack: () => void }) {
  const [report, setReport] = useState<ReportOut | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "empty">("loading");
  const [hoverNo, setHoverNo] = useState<number | null>(null);
  const [activeNo, setActiveNo] = useState<number | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const exportRef = useRef<HTMLDivElement>(null);

  const html = useMemo(
    () => (report ? renderMarkdown(report.markdown) : ""),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [report?.markdown],
  );

  useEffect(() => {
    let alive = true;
    setState("loading");
    setReport(null);
    setActiveNo(null);
    setHoverNo(null);
    getReport(taskId)
      .then((r) => {
        if (!alive) return;
        setReport(r);
        setState("ready");
      })
      .catch(() => {
        if (alive) setState("empty");
      });
    return () => {
      alive = false;
    };
  }, [taskId]);

  // activeNo 同步到正文角标（dangerouslySetInnerHTML 内容，直接操作 DOM class）
  useEffect(() => {
    const root = bodyRef.current;
    if (!root) return;
    root.querySelectorAll(".cite.active").forEach((el) => el.classList.remove("active"));
    if (activeNo != null) {
      root
        .querySelectorAll(`.cite[data-n="${activeNo}"]`)
        .forEach((el) => el.classList.add("active"));
    }
  }, [activeNo, html]);

  // 图表：markdown 落 DOM 后，按占位 data-chart 匹配规格并挂 ECharts；
  // innerHTML 重建（换报告）时先 dispose 旧实例
  useEffect(() => {
    const root = bodyRef.current;
    if (!root || !report) return;
    const byId = new Map(report.chart_specs.map((c) => [c.id, c]));
    const cleanup: (() => void)[] = [];
    root.querySelectorAll<HTMLElement>(".chart-block").forEach((block) => {
      const spec = byId.get(block.getAttribute("data-chart") ?? "");
      if (!spec) return;
      const titleEl = block.querySelector<HTMLElement>(".chart-title");
      const canvasEl = block.querySelector<HTMLElement>(".chart-canvas");
      if (!canvasEl) return;
      if (titleEl) titleEl.textContent = spec.title;
      const chart = echarts.init(canvasEl);
      chart.setOption(buildOption(spec));
      const ro = new ResizeObserver(() => chart.resize());
      ro.observe(canvasEl);
      cleanup.push(() => {
        ro.disconnect();
        chart.dispose();
      });
    });
    return () => cleanup.forEach((fn) => fn());
  }, [html, report]);

  // 导出下拉：点击外部关闭
  useEffect(() => {
    if (!exportOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (!exportRef.current?.contains(e.target as Node)) setExportOpen(false);
    };
    document.addEventListener("click", onDoc);
    return () => document.removeEventListener("click", onDoc);
  }, [exportOpen]);

  const scrollToCard = (no: number) => {
    document
      .getElementById(`rcard-${no}`)
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  /** 正文角标：hover 高亮信源卡（不滚动），点击高亮 + 滚动信源卡（再次点击取消） */
  const onMdMouseOver = (e: React.MouseEvent<HTMLDivElement>) => {
    const cite = (e.target as HTMLElement).closest(".cite");
    setHoverNo(cite ? Number(cite.getAttribute("data-n")) : null);
  };
  const onMdMouseOut = (e: React.MouseEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest(".cite")) setHoverNo(null);
  };
  const onMdClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const cite = (e.target as HTMLElement).closest(".cite");
    if (!cite) return;
    const no = Number(cite.getAttribute("data-n"));
    setActiveNo((prev) => (prev === no ? null : no));
    scrollToCard(no);
  };

  /** 信源卡：点击高亮正文全部 [n] 角标并滚动到第一处引用 */
  const onCardClick = (no: number) => {
    setActiveNo((prev) => (prev === no ? null : no));
    bodyRef.current
      ?.querySelector(`.cite[data-n="${no}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  /** 追问面板角标/chip：高亮并滚动信源卡 */
  const onJumpSource = (no: number) => {
    setActiveNo(no);
    scrollToCard(no);
  };

  if (state === "loading") {
    return (
      <div className="report-view">
        <div className="rpt-loading">报告加载中…</div>
      </div>
    );
  }

  if (state === "empty" || !report) {
    return (
      <div className="report-view">
        <div className="rpt-loading">
          <div className="empty">该任务暂无报告（可能尚未生成或中途失败）</div>
          <button className="btn" style={{ marginTop: 12 }} onClick={onBack}>
            返回工作台
          </button>
        </div>
      </div>
    );
  }

  const cardOn = (s: ReportSource) => hoverNo === s.no || activeNo === s.no;

  return (
    <div className="report-view">
      <div className="rpt-head">
        <button className="btn" onClick={onBack}>
          ← 返回工作台
        </button>
        <div className="rpt-head-main">
          <div className="rpt-q">{report.question}</div>
          <div className="rpt-meta">
            <span>{DEPTH_LABEL[report.depth] ?? report.depth}研究</span>
            <span>信源 {report.sources.length} 篇</span>
            <span>报告 {report.markdown.length.toLocaleString()} 字</span>
            {report.chart_specs.length > 0 && <span>图表 {report.chart_specs.length} 张</span>}
            <span>综合 {report.token_total.toLocaleString()} tokens</span>
            <span>¥ {report.cost_cny.toFixed(2)}</span>
          </div>
        </div>
        <div className="rpt-export" ref={exportRef}>
          <button className="btn" onClick={() => setExportOpen((v) => !v)}>
            导出 ▾
          </button>
          {exportOpen && (
            <div className="rpt-export-menu">
              <a href={exportUrl(report.report_id, "md")} onClick={() => setExportOpen(false)}>
                Markdown (.md)
              </a>
              <a href={exportUrl(report.report_id, "html")} onClick={() => setExportOpen(false)}>
                网页 (.html)
              </a>
              <a href={exportUrl(report.report_id, "pdf")} onClick={() => setExportOpen(false)}>
                PDF (.pdf)
              </a>
            </div>
          )}
        </div>
      </div>

      <div className="rpt-body">
        <div
          className="rpt-md"
          ref={bodyRef}
          onMouseOver={onMdMouseOver}
          onMouseOut={onMdMouseOut}
          onClick={onMdClick}
        >
          <div className="rpt-inner" dangerouslySetInnerHTML={{ __html: html }} />
        </div>

        <aside className="rpt-srcs">
          <div className="rpt-srcs-h">
            <span className="col-title">引用信源</span>
            <span className="col-count">{report.sources.length} 篇 · 点击编号溯源</span>
          </div>
          <div className="rpt-srcs-list">
            {report.sources.map((s) => (
              <div
                key={s.no}
                id={`rcard-${s.no}`}
                className={`rcard ${cardOn(s) ? "on" : ""}`}
                onClick={() => onCardClick(s.no)}
              >
                <div className="rcard-top">
                  <span className="no">[{s.no}]</span>
                  <span className={`chip ${chipCls(s.credibility)}`}>
                    <i />
                    {s.credibility != null ? `${s.credibility}/5` : "未评"}
                  </span>
                </div>
                <div className="rcard-title">{s.title ?? "(无标题)"}</div>
                <a
                  className="rcard-url"
                  href={s.url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => e.stopPropagation()}
                >
                  {s.url}
                </a>
                <div className="rcard-meta">
                  <span>{s.domain}</span>
                  {s.freshness != null ? (
                    <span className="fresh">新鲜 {(s.freshness * 100).toFixed(0)}%</span>
                  ) : null}
                </div>
                {s.excerpts.length > 0 && (
                  <div className="rcard-ex">
                    {s.excerpts.map((x, i) => (
                      <p key={i}>{x}</p>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </aside>

        <ChatPanel taskId={taskId} sources={report.sources} onJumpSource={onJumpSource} />
      </div>
    </div>
  );
}
