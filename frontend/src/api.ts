import type {
  AgentEvent,
  ChatMessage,
  Depth,
  ReportOut,
  SourceOut,
  TaskDetail,
  TaskOut,
} from "./types";

const BASE = "/api/research/tasks";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status}: ${text}`);
  }
  return resp.json() as Promise<T>;
}

export const listTasks = () => req<TaskOut[]>("");

export const getTask = (id: number) => req<TaskDetail>(`/${id}`);

export const createTask = (question: string, depth: Depth, tokenBudget?: number) =>
  req<TaskOut>("", {
    method: "POST",
    body: JSON.stringify({ question, depth, token_budget: tokenBudget }),
  });

export const controlTask = (id: number, action: "pause" | "resume" | "stop") =>
  req<TaskOut>(`/${id}/control`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });

export const listSources = (id: number) => req<SourceOut[]>(`/${id}/sources`);

/** 报告阅读页数据：markdown + citation_map + 带摘录的信源卡（无报告时 404）。 */
export const getReport = (id: number) => req<ReportOut>(`/${id}/report`);

export const listEvents = (id: number, afterSeq = 0) =>
  req<AgentEvent[]>(`/${id}/events?after_seq=${afterSeq}`);

/** SSE 流地址：EventSource 原生断线自动重连并携带 Last-Event-ID，后端按 seq 增量补发。 */
export const sseUrl = (id: number) => `${BASE}/${id}/events/stream`;

/** 追问历史（报告页进入时加载对话）。 */
export const listChat = (id: number) => req<ChatMessage[]>(`/${id}/chat`);

/** 发送追问：仅基于已收集笔记回答（不联网）。 */
export const sendChat = (id: number, text: string) =>
  req<ChatMessage>(`/${id}/chat`, { method: "POST", body: JSON.stringify({ text }) });

/** 报告导出地址：md / html / pdf（Content-Disposition 附件下载）。 */
export const exportUrl = (reportId: number, format: "md" | "html" | "pdf") =>
  `/api/reports/${reportId}/export?format=${format}`;
