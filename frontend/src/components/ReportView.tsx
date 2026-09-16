import { useEffect, useMemo, useRef, useState } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { getReport } from "../api";
import type { ReportOut, ReportSource } from "../types";

const CITE_RE = /\[(\d+)\]/g;
const DEPTH_LABEL: Record<string, string> = { quick: "快速", std: "标准", deep: "深度" };

/** markdown → 安全 HTML；[N] 先替换为交互角标 sup.cite 再交给 marked（保留 inline HTML）。 */
function renderMarkdown(md: string): string {
  const withCites = md.replace(
    CITE_RE,
    (_m, n: string) => `<sup class="cite" data-n="${n}">${n}</sup>`,
  );
  return DOMPurify.sanitize(marked.parse(withCites, { gfm: true, async: false }), {
    ADD_ATTR: ["data-n"],
  });
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
  const bodyRef = useRef<HTMLDivElement>(null);

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
            <span>综合 {report.token_total.toLocaleString()} tokens</span>
            <span>¥ {report.cost_cny.toFixed(2)}</span>
          </div>
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
      </div>
    </div>
  );
}
