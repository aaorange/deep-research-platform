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
