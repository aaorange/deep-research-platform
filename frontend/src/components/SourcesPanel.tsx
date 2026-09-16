import type { SourceOut } from "../types";

function chipCls(n: number | null): string {
  if (n == null) return "";
  if (n >= 4) return "c4";
  if (n >= 3) return "c3";
  return "";
}

export function SourcesPanel({ sources }: { sources: SourceOut[] }) {
  if (sources.length === 0) {
    return <div className="empty">信源将随抓取出现在这里</div>;
  }
  return (
    <>
      {sources.map((s) => (
        <div key={s.id} className="src">
          <div className="t">{s.title ?? "(无标题)"}</div>
          <div className="row">
            <a href={s.url} target="_blank" rel="noreferrer">
              {s.url}
            </a>
            <span className={`chip ${chipCls(s.credibility)}`}>
              <i />
              {s.credibility != null ? `${s.credibility}/5` : "未评"}
            </span>
          </div>
          <div className="row">
            <span>{s.domain}</span>
            {s.freshness != null ? (
              <span className="fresh">新鲜 {(s.freshness * 100).toFixed(0)}%</span>
            ) : null}
          </div>
        </div>
      ))}
    </>
  );
}
