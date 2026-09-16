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

export interface ChartSeries {
  name: string;
  data: number[];
}

/** ECharts 图表规格：与后端 ChartSpec 契约一致（bar/line/pie） */
export interface ChartSpec {
  id: string;
  title: string;
  type: "bar" | "line" | "pie";
  x: string[];
  series: ChartSeries[];
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
  chart_specs: ChartSpec[];
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  /** 回答引用的信源库 id（按出现顺序去重） */
  cited_source_ids: number[];
  created_at: string;
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

export interface StatsModelUsage {
  model: string;
  role: string;
  prompt_tokens: number;
  completion_tokens: number;
  tokens: number;
  cost_cny: number;
  calls: number;
}

export interface StatsTaskPoint {
  id: number;
  question: string;
  depth: Depth;
  status: TaskStatus;
  cost_cny: number;
  token_used: number;
  created_at: string | null;
}

export interface StatsOut {
  days: number;
  summary: {
    total_cost_cny: number | null;
    task_count: number | null;
    total_tokens: number | null;
    prompt_tokens: number | null;
    completion_tokens: number | null;
  };
  cache: {
    hit_rate: number | null;
    cache_hits: number | null;
    total_calls: number | null;
    saved_cny: number | null;
  };
  by_model: StatsModelUsage[];
  daily: { date: string; cost_cny: number; tokens: number; by_model: Record<string, number> }[];
  avg_by_depth: Record<string, { count: number; avg_cost_cny: number }>;
  tasks: StatsTaskPoint[];
  pricing_note: string;
}
