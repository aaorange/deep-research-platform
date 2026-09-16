import type { SubTask } from "../types";

const CHECK: Record<string, string> = {
  done: "✓",
  error: "✕",
  skipped: "—",
};

/** running 在新样式里叫 run */
const CLS: Record<string, string> = { running: "run" };

export function SubTaskChecklist({ subTasks }: { subTasks: SubTask[] }) {
  if (subTasks.length === 0) {
    return <div className="empty">等待规划……</div>;
  }
  return (
    <>
      {subTasks.map((s) => (
        <div key={s.id} className={`sub ${CLS[s.status] ?? s.status}`}>
          <div className="box">{CHECK[s.status] ?? ""}</div>
          <div className="t">
            {s.title}
            {s.round_no > 1 ? <span className="r"> · R{s.round_no}</span> : null}
          </div>
        </div>
      ))}
    </>
  );
}
