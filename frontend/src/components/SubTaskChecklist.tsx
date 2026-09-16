import type { SubTask } from "../types";

const CHECK: Record<string, string> = {
  done: "✓",
  error: "✕",
  skipped: "-",
};

export function SubTaskChecklist({ subTasks }: { subTasks: SubTask[] }) {
  if (subTasks.length === 0) {
    return <div className="empty">等待规划……</div>;
  }
  return (
    <>
      {subTasks.map((s) => (
        <div key={s.id} className={`subtask-item ${s.status}`}>
          <div className="checkbox">{CHECK[s.status] ?? ""}</div>
          <div className="title">
            {s.title}
            {s.round_no > 1 ? <span className="round"> · R{s.round_no}</span> : null}
          </div>
        </div>
      ))}
    </>
  );
}
