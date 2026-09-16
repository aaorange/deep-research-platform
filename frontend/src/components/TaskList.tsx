import type { TaskOut } from "../types";

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${status}`}>{status}</span>;
}

export function TaskList({
  tasks,
  selectedId,
  onSelect,
}: {
  tasks: TaskOut[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  if (tasks.length === 0) {
    return <div className="empty">还没有任务</div>;
  }
  return (
    <>
      {tasks.map((t) => (
        <div
          key={t.id}
          className={`task-item ${t.id === selectedId ? "active" : ""}`}
          onClick={() => onSelect(t.id)}
        >
          <div className="q" title={t.question}>
            {t.question}
          </div>
          <div className="meta">
            <span>#{t.id}</span>
            <StatusBadge status={t.status} />
            <span>{t.depth}</span>
            <span>{t.cost_cny.toFixed(2)} 元</span>
          </div>
        </div>
      ))}
    </>
  );
}
