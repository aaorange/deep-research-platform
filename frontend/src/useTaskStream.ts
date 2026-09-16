import { useEffect, useRef, useState } from "react";
import { sseUrl } from "./api";
import type { AgentEvent, TaskStatus } from "./types";

export interface StreamState {
  connected: boolean;
  ended: TaskStatus | null;
  events: AgentEvent[];
}

/** 订阅任务 SSE 事件流。

- 浏览器 EventSource 断线自动重连并携带 Last-Event-ID，后端按 seq 增量补发；
- 事件按 seq 去重合并（重连窗口期可能重复收到同一条）；
- 收到 `end`（任务终态）后关闭连接。
 */
export function useTaskStream(taskId: number | null): StreamState {
  const [connected, setConnected] = useState(false);
  const [ended, setEnded] = useState<TaskStatus | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const lastSeqRef = useRef(0);

  useEffect(() => {
    setConnected(false);
    setEnded(null);
    setEvents([]);
    lastSeqRef.current = 0;
    if (taskId === null) return;

    const es = new EventSource(sseUrl(taskId));
    es.onopen = () => setConnected(true);

    es.onerror = () => setConnected(false);

    const onEvent = (e: MessageEvent<string>) => {
      setConnected(true);
      try {
        const ev = JSON.parse(e.data) as AgentEvent;
        if (ev.seq <= lastSeqRef.current) return; // 重连补发去重
        lastSeqRef.current = ev.seq;
        setEvents((prev) => [...prev, ev]);
      } catch {
        // 忽略无法解析的数据帧
      }
    };

    const types = [
      "plan",
      "search",
      "fetch",
      "note",
      "reflect",
      "degrade",
      "budget",
      "synthesize",
      "control",
    ];
    for (const t of types) es.addEventListener(t, onEvent as EventListener);

    es.addEventListener("end", ((e: MessageEvent<string>) => {
      try {
        const { status } = JSON.parse(e.data) as { status: TaskStatus };
        setEnded(status);
      } catch {
        setEnded("done");
      }
      es.close();
      setConnected(false);
    }) as EventListener);

    return () => es.close();
  }, [taskId]);

  return { connected, ended, events };
}
