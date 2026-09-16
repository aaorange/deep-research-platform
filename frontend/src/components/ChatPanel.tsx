import { useEffect, useMemo, useRef, useState } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { listChat, listSources, sendChat } from "../api";
import type { ChatMessage, ReportSource, SourceOut } from "../types";

const CITE_RE = /\[(\d+)\]/g;

/** 回答正文 [sid] → 信源库 id 锚点转交互角标；有展示编号才可跳转，否则保持原文 */
function renderAnswer(
  md: string,
  sidToNo: Map<number, number>,
): string {
  const withCites = md.replace(CITE_RE, (_m, sid: string) => {
    const no = sidToNo.get(Number(sid));
    return no != null
      ? `<sup class="cite" data-n="${no}">${no}</sup>`
      : `<span class="cite raw">[${sid}]</span>`;
  });
  return DOMPurify.sanitize(marked.parse(withCites, { gfm: true, async: false }), {
    ADD_ATTR: ["data-n"],
  });
}

export function ChatPanel({
  taskId,
  sources,
  onJumpSource,
}: {
  taskId: number;
  /** 被报告引用的信源（有展示编号） */
  sources: ReportSource[];
  /** 点击角标/范围 chip：高亮并滚动到信源卡 */
  onJumpSource: (no: number) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [allSources, setAllSources] = useState<SourceOut[]>([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // 信源库 id → 展示编号（仅在报告引用范围内可跳转）
  const sidToNo = useMemo(() => {
    const m = new Map<number, number>();
    for (const s of sources) m.set(s.id, s.no);
    return m;
  }, [sources]);

  // 全量信源（未进报告引用的信源只能以标题 chip 呈现并链出）
  const sourceById = useMemo(() => {
    const m = new Map<number, SourceOut>();
    for (const s of allSources) m.set(s.id, s);
    return m;
  }, [allSources]);

  useEffect(() => {
    let alive = true;
    setMessages([]);
    setError(null);
    listChat(taskId)
      .then((ms) => alive && setMessages(ms))
      .catch(() => {});
    listSources(taskId)
      .then((ss) => alive && setAllSources(ss))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [taskId]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, sending]);

  const send = async () => {
    const t = text.trim();
    if (t.length < 2 || sending) return;
    setSending(true);
    setError(null);
    setText("");
    const optimistic: ChatMessage = {
      id: -Date.now(),
      role: "user",
      content: t,
      cited_source_ids: [],
      created_at: "",
    };
    setMessages((prev) => [...prev, optimistic]);
    try {
      await sendChat(taskId, t);
      const ms = await listChat(taskId);
      setMessages(ms);
    } catch (e) {
      setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
      setText(t);
      setError(e instanceof Error ? e.message : "发送失败");
    } finally {
      setSending(false);
    }
  };

  /** 正文角标：点击跳转信源卡 */
  const onAnswerClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const cite = (e.target as HTMLElement).closest(".cite[data-n]");
    if (!cite) return;
    onJumpSource(Number(cite.getAttribute("data-n")));
  };

  return (
    <div className="rpt-chat">
      <div className="rpt-chat-h">
        <span className="col-title">报告追问</span>
        <span className="col-count">基于 {allSources.length || sources.length} 篇笔记 · 不联网</span>
      </div>
      <div className="rpt-chat-list" ref={listRef}>
        {messages.length === 0 && !sending && (
          <div className="chat-empty">
            对报告内容有疑问？在此追问，回答仅基于本次研究收集的笔记，论断带信源标注。
          </div>
        )}
        {messages.map((m) =>
          m.role === "user" ? (
            <div key={m.id} className="chat-msg user">
              <div className="chat-bubble">{m.content}</div>
            </div>
          ) : (
            <div key={m.id} className="chat-msg bot">
              <div className="chat-who">研究助理</div>
              <div
                className="chat-bubble md"
                onClick={onAnswerClick}
                dangerouslySetInnerHTML={{ __html: renderAnswer(m.content, sidToNo) }}
              />
              {m.cited_source_ids.length > 0 && (
                <div className="chat-cited">
                  <span className="chat-cited-label">信源</span>
                  {m.cited_source_ids.map((sid) => {
                    const no = sidToNo.get(sid);
                    if (no != null) {
                      return (
                        <button key={sid} className="cchip" onClick={() => onJumpSource(no)}>
                          [{no}]
                        </button>
                      );
                    }
                    const s = sourceById.get(sid);
                    return s ? (
                      <a key={sid} className="cchip out" href={s.url} target="_blank" rel="noreferrer">
                        {s.title || s.domain || `信源 ${sid}`}
                      </a>
                    ) : (
                      <span key={sid} className="cchip ghost">
                        [{sid}]
                      </span>
                    );
                  })}
                </div>
              )}
            </div>
          ),
        )}
        {sending && (
          <div className="chat-msg bot">
            <div className="chat-who">研究助理</div>
            <div className="chat-bubble thinking">
              <span />
              <span />
              <span />
            </div>
          </div>
        )}
        {error && <div className="chat-error">追问失败：{error}</div>}
      </div>
      <div className="rpt-chat-input">
        <textarea
          value={text}
          placeholder="如：报告中提到的数据口径分别来自哪些信源？"
          rows={2}
          maxLength={2000}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button
          className="btn primary"
          onClick={send}
          disabled={sending || text.trim().length < 2}
        >
          {sending ? "回答中…" : "发送"}
        </button>
      </div>
    </div>
  );
}
