import type { SourceOut } from "../types";

function stars(n: number | null): string {
  if (n == null) return "";
  return "★".repeat(n) + "☆".repeat(5 - n);
}

export function SourcesPanel({ sources }: { sources: SourceOut[] }) {
  if (sources.length === 0) {
    return <div className="empty">信源将随抓取出现在这里</div>;
  }
  return (
    <>
      {sources.map((s) => (
        <div key={s.id} className="source-card">
          <div className="s-title">{s.title ?? "(无标题)"}</div>
          <div className="s-meta">
            <span>{s.domain}</span>
            <span className="stars" title={`可信度 ${s.credibility ?? "-"}/5`}>
              {stars(s.credibility)}
            </span>
          </div>
          <div className="s-meta">
            <a href={s.url} target="_blank" rel="noreferrer">
              {s.url}
            </a>
            {s.freshness != null ? <span>新鲜 {(s.freshness * 100).toFixed(0)}%</span> : null}
          </div>
        </div>
      ))}
    </>
  );
}
