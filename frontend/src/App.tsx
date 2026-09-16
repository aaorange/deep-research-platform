import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import { ActionStream } from "./components/ActionStream";
import { CostPanel } from "./components/CostPanel";
import { DepthSelect } from "./components/DepthSelect";
import { ReportView } from "./components/ReportView";
import { StatsView } from "./components/StatsView";
import { StatusBadge, TaskList } from "./components/TaskList";
import { SourcesPanel } from "./components/SourcesPanel";
import { SubTaskChecklist } from "./components/SubTaskChecklist";
import { useTaskStream } from "./useTaskStream";
import type { Depth, SourceOut, TaskDetail, TaskOut } from "./types";

/** 事件到达后需要刷新详情/信源的事件类型（子任务状态与信源表是 DB 侧落库；
 *  control 含 job 围栏 abort / resume 跳过重规划等控制信号，状态变化需即时反映） */
const REFRESH_TYPES = new Set(["plan", "note", "budget", "synthesize", "control"]);

export default function App() {
  const [tasks, setTasks] = useState<TaskOut[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [sources, setSources] = useState<SourceOut[]>([]);
  const [question, setQuestion] = useState("");
  const [depth, setDepth] = useState<Depth>("std");
  const [creating, setCreating] = useState(false);
  const [showReport, setShowReport] = useState(false);
  const [showStats, setShowStats] = useState(false);
  const refreshTimer = useRef<number | null>(null);
  // 本次选中期间是否见过运行态：只有「观看中转 done」才自动打开报告，
  // 历史 done 任务选中时 SSE 秒发 end，不应跳页
  const sawLiveRef = useRef(false);

  const { connected, ended, events } = useTaskStream(selectedId);

  const loadTasks = useCallback(async () => {
    try {
      const list = await api.listTasks();
      setTasks(list);
      return list;
    } catch {
      return [];
    }
  }, []);

  const loadDetail = useCallback(async (id: number) => {
    try {
      const [d, s] = await Promise.all([api.getTask(id), api.listSources(id)]);
      setDetail(d);
      setSources(s);
    } catch {
      /* 后端未启动等场景：保留旧数据 */
    }
  }, []);

  useEffect(() => {
    loadTasks().then((list) => {
      if (list.length > 0 && selectedId === null) {
        setSelectedId(list[0].id);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (selectedId === null) return;
    setDetail(null);
    setSources([]);
    setShowReport(false);
    sawLiveRef.current = false;
    loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  // SSE 事件驱动：plan/note/budget/synthesize → debounce 刷新详情与信源
  useEffect(() => {
    if (selectedId === null || events.length === 0) return;
    const last = events[events.length - 1];
    if (!REFRESH_TYPES.has(last.type)) return;
    if (refreshTimer.current != null) window.clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(() => {
      loadDetail(selectedId);
      loadTasks();
    }, 400);
  }, [events, selectedId, loadDetail, loadTasks]);

  // 终态：最终刷新（成本/报告落库）；研究完成自动进入报告阅读页（仅限观看中的运行任务）
  useEffect(() => {
    if (ended && selectedId !== null) {
      loadDetail(selectedId);
      loadTasks();
      if (ended === "done" && sawLiveRef.current) setShowReport(true);
    }
  }, [ended, selectedId, loadDetail, loadTasks]);

  useEffect(() => {
    if (["queued", "running", "paused"].includes(detail?.status ?? "")) sawLiveRef.current = true;
  }, [detail?.status]);

  const createTask = async () => {
    const q = question.trim();
    if (q.length < 2 || creating) return;
    setCreating(true);
    try {
      const t = await api.createTask(q, depth);
      setQuestion("");
      setShowReport(false);
      setSelectedId(t.id);
      await loadTasks();
    } finally {
      setCreating(false);
    }
  };

  const control = async (action: "pause" | "resume" | "stop") => {
    if (selectedId === null) return;
    try {
      await api.controlTask(selectedId, action);
    } catch {
      /* 状态已变的竞态（如 409）：刷新取真实状态即可 */
    } finally {
      await loadDetail(selectedId);
      await loadTasks();
    }
  };

  const removeTask = async (t: TaskOut) => {
    if (!window.confirm(`删除任务「${t.question.slice(0, 40)}」及其全部数据？不可恢复。`)) return;
    try {
      await api.deleteTask(t.id);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "删除失败");
      return;
    }
    const list = await loadTasks();
    if (selectedId === t.id) {
      setSelectedId(list.length > 0 ? list[0].id : null);
      setDetail(null);
      setSources([]);
      setShowReport(false);
    }
  };

  const status = detail?.status ?? "queued";
  const canPause = status === "running" || status === "queued";
  const canResume = status === "paused" || status === "failed";
  const canStop = ["queued", "running", "paused"].includes(status);
  const canReport = status === "done";
  const doneSubs = detail?.sub_tasks.filter((s) => s.status === "done").length ?? 0;

  // 非终态任务 5s 轮询兜底：queued→running 的翻转不产生 SSE 事件，
  // SSE 断连/事件间隙（LLM 长调用）也要保持状态与成本新鲜
  const liveStatus = ["queued", "running", "paused"].includes(status);
  useEffect(() => {
    if (selectedId === null || !liveStatus) return;
    const t = window.setInterval(() => {
      loadDetail(selectedId);
      loadTasks();
    }, 5000);
    return () => window.clearInterval(t);
  }, [selectedId, liveStatus, loadDetail, loadTasks]);

  return (
    <>
      <header className="topbar">
        <div className="brand">
          Deep Research<span>深度研究工作台</span>
        </div>
        <div className="create-form">
          <input
            type="text"
            placeholder="输入研究问题，如：2025 年国产大模型在医疗行业的落地情况"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && createTask()}
          />
          <DepthSelect value={depth} onChange={setDepth} />
          <button className="btn primary" onClick={createTask} disabled={creating || question.trim().length < 2}>
            {creating ? "提交中…" : "开始研究"}
          </button>
          {showStats ? (
            <button className="btn back-toggle" onClick={() => setShowStats(false)} title="返回研究工作台">
              ← 返回工作台
            </button>
          ) : (
            <button
              className="btn stats-toggle"
              onClick={() => setShowStats(true)}
              title="成本看板：总成本 / 缓存节省 / 模型拆分 / 趋势"
            >
              成本看板
            </button>
          )}
        </div>
      </header>

      {showStats ? (
        <StatsView onBack={() => setShowStats(false)} />
      ) : showReport && selectedId !== null ? (
        <ReportView taskId={selectedId} onBack={() => setShowReport(false)} />
      ) : (
        <div className="workbench">
        <div className="column">
          <div className="col-header">
            <span className="col-title">任务清单</span>
            <span className="col-count">{tasks.length} 个</span>
          </div>
          <div className="col-body">
            <TaskList tasks={tasks} selectedId={selectedId} onSelect={setSelectedId} onDelete={removeTask} />
          </div>
          <div className="col-header">
            <span className="col-title">子任务</span>
            <span className="col-count">
              {detail ? `${doneSubs}/${detail.sub_tasks.length}` : "-"}
            </span>
          </div>
          <div className="col-body" style={{ maxHeight: "45%" }}>
            <SubTaskChecklist subTasks={detail?.sub_tasks ?? []} />
          </div>
        </div>

        <div className="column">
          <div className="col-header">
            <div className="stream-toolbar">
              <span className={`conn-dot ${connected ? "on" : ""}`} title={connected ? "SSE 已连接" : "SSE 断开（自动重连中）"} />
              <span className="col-title">动作流</span>
              <span className="col-count">{events.length} 条</span>
            </div>
            {detail ? (
              <div className="ctrl">
                <StatusBadge status={status} />
                {canReport && (
                  <button className="btn" onClick={() => setShowReport(true)}>
                    查看报告
                  </button>
                )}
                {canPause && (
                  <button className="btn warn" onClick={() => control("pause")}>
                    ⏸ 暂停
                  </button>
                )}
                {canResume && (
                  <button className="btn" onClick={() => control("resume")}>
                    ▶ 恢复
                  </button>
                )}
                {canStop && (
                  <button className="btn danger" onClick={() => control("stop")}>
                    ⏹ 终止
                  </button>
                )}
              </div>
            ) : null}
          </div>
          <div className="col-body">
            <ActionStream events={events} />
          </div>
        </div>

        <div className="column">
          <div className="col-header">
            <span className="col-title">信源与成本</span>
            <span className="col-count">{sources.length} 个信源</span>
          </div>
          <CostPanel detail={detail} events={events} />
          <div className="col-body">
            <SourcesPanel sources={sources} />
          </div>
        </div>
        </div>
      )}
    </>
  );
}
