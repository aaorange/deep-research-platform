import type { AgentEvent, Depth, SourceOut, TaskDetail, TaskOut } from "./types";

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

export const listEvents = (id: number, afterSeq = 0) =>
  req<AgentEvent[]>(`/${id}/events?after_seq=${afterSeq}`);

/** SSE 流地址：EventSource 原生断线自动重连并携带 Last-Event-ID，后端按 seq 增量补发。 */
export const sseUrl = (id: number) => `${BASE}/${id}/events/stream`;
