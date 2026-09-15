"""子任务「搜读记」子图：search → read → note。

D5 骨架版：单轮搜索 → 读 top N 页（URL 与正文哈希去重）→ instructor 压缩笔记。
全程事件落库：search / fetch / degrade / note / control。
换关键词重搜与反思循环属 W2 主图职责，不在本子图范围。

工具与记录器通过 WorkerDeps 注入，生产路径传真实实现，测试传 mock。
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.db import EventType
from app.engine.schemas import WorkerNote
from app.tools.credibility import credibility_for_url
from app.tools.read_page import (
    PageContent,
    ReadPageError,
    degradation_events,
    page_to_event,
)
from app.tools.web_search import SearchResult

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_COUNT = 8
DEFAULT_MAX_PAGES = 4


class WorkerState(TypedDict, total=False):
    task_id: int
    sub_task_id: int
    title: str
    keywords: str | None
    max_pages: int

    # 节点输出
    search_query: str
    search_provider: str
    hits: list[dict]
    pages: list[dict]
    sources: list[dict]
    note: dict
    note_tokens: int
    error: str | None


class SearchFn(Protocol):
    async def __call__(self, query: str, count: int) -> SearchResult: ...


class ReadFn(Protocol):
    async def __call__(self, url: str) -> PageContent: ...


class NoteFn(Protocol):
    async def __call__(self, title: str, pages: list[dict]) -> tuple[WorkerNote, object | None]: ...


class RecorderProtocol(Protocol):
    async def record(
        self,
        task_id: int,
        type: EventType,
        payload: dict | None = None,
        *,
        sub_task_id: int | None = None,
        tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> object: ...


@dataclass
class WorkerDeps:
    search: SearchFn
    read_page: ReadFn
    write_note: NoteFn
    recorder: RecorderProtocol


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def build_worker_graph(deps: WorkerDeps):
    async def search_node(state: WorkerState) -> dict:
        task_id = state["task_id"]
        sub_task_id = state.get("sub_task_id")
        query = state.get("keywords") or state["title"]
        try:
            result = await deps.search(query, DEFAULT_SEARCH_COUNT)
        except Exception as e:  # noqa: BLE001
            logger.warning("search node failed: %s", e)
            await deps.recorder.record(
                task_id,
                EventType.control,
                {"stage": "search", "error": f"{type(e).__name__}: {e}"},
                sub_task_id=sub_task_id,
            )
            return {"error": f"search failed: {e}", "search_query": query}

        await deps.recorder.record(
            task_id,
            EventType.search,
            {
                "query": query,
                "provider": result.provider,
                "hits": len(result.hits),
                "titles": [h.title for h in result.hits[:5]],
            },
            sub_task_id=sub_task_id,
            latency_ms=result.latency_ms,
        )
        hits = [{"title": h.title, "url": h.url, "snippet": h.snippet} for h in result.hits]
        if not hits:
            await deps.recorder.record(
                task_id,
                EventType.control,
                {"stage": "search", "error": "no hits"},
                sub_task_id=sub_task_id,
            )
            return {"error": f"no search hits for {query!r}", "search_query": query}
        return {
            "search_query": query,
            "search_provider": result.provider,
            "hits": hits,
        }

    async def read_node(state: WorkerState) -> dict:
        task_id = state["task_id"]
        sub_task_id = state.get("sub_task_id")
        hits = state["hits"][: state.get("max_pages", DEFAULT_MAX_PAGES)]

        async def read_one(hit: dict) -> dict | None:
            url = hit["url"]
            try:
                page = await deps.read_page(url)
            except ReadPageError as e:
                for d in e.degradations:
                    await deps.recorder.record(
                        task_id,
                        EventType.degrade,
                        {"from": d["tier"], "to": d["fell_to"], "error": d["error"], "url": url},
                        sub_task_id=sub_task_id,
                    )
                return None
            except Exception as e:  # noqa: BLE001
                logger.warning("read_page unexpected failure %s: %s", url, e)
                return None

            cred = credibility_for_url(url)
            await deps.recorder.record(
                task_id,
                EventType.fetch,
                {**page_to_event(page), "domain": cred.domain, "credibility": cred.score},
                sub_task_id=sub_task_id,
                latency_ms=page.latency_ms,
            )
            for payload in degradation_events(page):
                await deps.recorder.record(
                    task_id,
                    EventType.degrade,
                    {**payload, "url": url},
                    sub_task_id=sub_task_id,
                )
            return {"page": page, "hit": hit, "cred": cred}

        results = await asyncio.gather(*[read_one(h) for h in hits])

        pages: list[dict] = []
        sources: list[dict] = []
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()
        for r in results:
            if r is None:
                continue
            page, hit, cred = r["page"], r["hit"], r["cred"]
            if page.url in seen_urls:
                continue
            digest = _content_hash(page.text)
            if digest in seen_hashes:
                continue
            seen_urls.add(page.url)
            seen_hashes.add(digest)
            idx = len(pages) + 1
            title = page.title or hit.get("title") or ""
            pages.append({"idx": idx, "title": title, "credibility": cred.score, "text": page.text})
            sources.append(
                {
                    "idx": idx,
                    "url": page.url,
                    "title": title,
                    "domain": cred.domain,
                    "credibility": cred.score,
                    "content_hash": digest,
                }
            )

        if not pages:
            await deps.recorder.record(
                task_id,
                EventType.control,
                {"stage": "read", "error": "all pages failed to read"},
                sub_task_id=sub_task_id,
            )
            return {"error": "all pages failed to read", "pages": [], "sources": []}
        return {"pages": pages, "sources": sources}

    async def note_node(state: WorkerState) -> dict:
        task_id = state["task_id"]
        sub_task_id = state.get("sub_task_id")
        try:
            note, usage = await deps.write_note(state["title"], state["pages"])
        except Exception as e:  # noqa: BLE001
            logger.warning("note node failed: %s", e)
            await deps.recorder.record(
                task_id,
                EventType.control,
                {"stage": "note", "error": f"{type(e).__name__}: {e}"},
                sub_task_id=sub_task_id,
            )
            return {"error": f"note generation failed: {e}"}

        tokens = getattr(usage, "total_tokens", None)
        await deps.recorder.record(
            task_id,
            EventType.note,
            {
                "summary_chars": len(note.summary),
                "facts": len(note.facts),
                "gaps": note.gaps,
                "sources_used": len(state["pages"]),
            },
            sub_task_id=sub_task_id,
            tokens=tokens,
        )
        return {"note": note.model_dump(), "note_tokens": tokens or 0}

    def after_search(state: WorkerState) -> Literal["read", "__end__"]:
        return "__end__" if state.get("error") else "read"

    def after_read(state: WorkerState) -> Literal["note", "__end__"]:
        return "__end__" if state.get("error") else "note"

    graph = StateGraph(WorkerState)
    graph.add_node("search", search_node)
    graph.add_node("read", read_node)
    graph.add_node("note", note_node)
    graph.add_edge(START, "search")
    graph.add_conditional_edges("search", after_search)
    graph.add_conditional_edges("read", after_read)
    graph.add_edge("note", END)
    return graph.compile()
