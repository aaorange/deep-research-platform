export type TaskStatus =
  | "queued"
  | "running"
  | "paused"
  | "done"
  | "failed"
  | "canceled"
  | "stopped";

export type Depth = "quick" | "std" | "deep";

export interface SubTask {
  id: number;
  title: string;
  keywords: string | null;
  status: "pending" | "running" | "done" | "skipped" | "error";
  round_no: number;
}

export interface TaskOut {
  id: number;
  question: string;
  background: string | null;
  depth: Depth;
  status: TaskStatus;
  token_budget: number;
  token_used: number;
  cost_cny: number;
  error_msg: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface TaskDetail extends TaskOut {
  sub_tasks: SubTask[];
  instructions: { id: number; text: string; consumed_round: number | null }[];
}

export interface SourceOut {
  id: number;
  url: string;
  title: string | null;
  domain: string | null;
  credibility: number | null;
  freshness: number | null;
}

export interface ReportSource {
  /** 报告中的展示编号 [n] */
  no: number;
  id: number;
  url: string;
  title: string | null;
  domain: string | null;
  credibility: number | null;
  freshness: number | null;
  /** 从笔记中提取的该信源摘录 */
  excerpts: string[];
}

export interface ReportOut {
  report_id: number;
  task_id: number;
  question: string;
  depth: Depth;
  markdown: string;
  /** 展示编号 → 信源库 id */
  citation_map: Record<string, number>;
  token_total: number;
  cost_cny: number;
  sources: ReportSource[];
}

export interface AgentEvent {
  seq: number;
  task_id: number;
  sub_task_id: number | null;
  type: string;
  payload: Record<string, unknown>;
  tokens: number | null;
  latency_ms: number | null;
  created_at: string | null;
}
