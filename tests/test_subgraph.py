from app.db import EventType
from app.engine.schemas import NoteFact, NoteUsage, WorkerNote
from app.engine.subgraph import WorkerDeps, build_worker_graph
from app.tools.read_page import PageContent, ReadPageError
from app.tools.web_search import SearchHit, SearchResult


class FakeRecorder:
    def __init__(self):
        self.events: list[dict] = []

    async def record(
        self,
        task_id,
        type,
        payload=None,
        *,
        sub_task_id=None,
        tokens=None,
        latency_ms=None,
    ):
        self.events.append(
            {
                "task_id": task_id,
                "type": type,
                "payload": payload or {},
                "sub_task_id": sub_task_id,
                "tokens": tokens,
                "latency_ms": latency_ms,
            }
        )


def make_search(hits=None, error=None):
    calls = []

    async def search(query, count):
        calls.append((query, count))
        if error:
            raise error
        return SearchResult(
            query=query,
            provider="bocha",
            hits=hits or [],
            latency_ms=800,
        )

    search.calls = calls
    return search


def make_read(pages_by_url=None, error_urls=None):
    pages_by_url = pages_by_url or {}
    error_urls = error_urls or set()

    async def read_page(url):
        if url in error_urls:
            raise ReadPageError(
                url,
                degradations=[
                    {"tier": "jina", "fell_to": "crawl4ai", "error": "RuntimeError: x"},
                    {"tier": "crawl4ai", "fell_to": "trafilatura", "error": "RuntimeError: y"},
                    {"tier": "trafilatura", "fell_to": None, "error": "ValueError: no content"},
                ],
            )
        return pages_by_url.get(url) or PageContent(
            url=url,
            provider="jina",
            title=f"页面 {url}",
            text=f"{url} 的正文内容。" * 60,
            latency_ms=500,
        )

    return read_page


def make_note(note=None, usage=None, error=None):
    calls = []

    async def write_note(title, pages):
        calls.append((title, pages))
        if error:
            raise error
        return note or WorkerNote(
            summary="核心发现，含 [1] 引用。", facts=[NoteFact(claim="事实", citation=1)], gaps=[]
        ), usage or NoteUsage(prompt_tokens=3000, completion_tokens=400, total_tokens=3400)

    write_note.calls = calls
    return write_note


def make_deps(search, read, note, recorder=None):
    return WorkerDeps(
        search=search, read_page=read, write_note=note, recorder=recorder or FakeRecorder()
    )


async def test_happy_path_full_flow():
    hits = [
        SearchHit(title="医疗大模型报告", url="https://www.thepaper.cn/a", snippet="s1"),
        SearchHit(title="医院落地案例", url="https://36kr.com/b", snippet="s2"),
    ]
    recorder = FakeRecorder()
    deps = make_deps(make_search(hits), make_read(), make_note(), recorder)

    state = {
        "task_id": 1,
        "sub_task_id": 7,
        "title": "国产大模型在医疗行业的落地案例",
    }
    graph = build_worker_graph(deps)
    final = await graph.ainvoke(state)

    assert final.get("error") is None
    assert final["note"]["summary"].startswith("核心发现")
    assert final["note_tokens"] == 3400
    assert final["search_provider"] == "bocha"
    assert len(final["pages"]) == 2
    assert final["pages"][0]["idx"] == 1
    assert final["sources"][0]["url"] == "https://www.thepaper.cn/a"
    assert final["sources"][0]["credibility"] == 4  # thepaper.cn 主流媒体
    assert final["sources"][1]["credibility"] == 3  # 36kr 科技媒体

    types = [e["type"] for e in recorder.events]
    assert types == [EventType.search, EventType.fetch, EventType.fetch, EventType.note]

    search_ev = recorder.events[0]
    assert search_ev["payload"]["query"] == "国产大模型在医疗行业的落地案例"
    assert search_ev["sub_task_id"] == 7
    fetch_ev = recorder.events[1]
    assert fetch_ev["payload"]["credibility"] == 4
    assert fetch_ev["payload"]["provider"] == "jina"
    note_ev = recorder.events[3]
    assert note_ev["tokens"] == 3400
    assert note_ev["payload"]["summary_chars"] > 0


async def test_keywords_preferred_over_title():
    search = make_search([SearchHit(title="t", url="https://a.com/1", snippet="s")])
    deps = make_deps(search, make_read(), make_note())

    graph = build_worker_graph(deps)
    final = await graph.ainvoke(
        {"task_id": 1, "sub_task_id": 7, "title": "标题", "keywords": "指定关键词"}
    )

    assert search.calls[0][0] == "指定关键词"
    assert final.get("error") is None


async def test_search_no_hits_routes_to_end():
    recorder = FakeRecorder()
    deps = make_deps(make_search(hits=[]), make_read(), make_note(), recorder)

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "sub_task_id": 7, "title": "冷门话题"})

    assert "no search hits" in final["error"]
    types = [e["type"] for e in recorder.events]
    assert types == [EventType.search, EventType.control]
    assert recorder.events[1]["payload"]["stage"] == "search"


async def test_search_exception_routes_to_end():
    recorder = FakeRecorder()
    deps = make_deps(
        make_search(error=RuntimeError("bocha quota exceeded")),
        make_read(),
        make_note(),
        recorder,
    )

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "sub_task_id": 7, "title": "标题"})

    assert "search failed" in final["error"]
    types = [e["type"] for e in recorder.events]
    assert types == [EventType.control]
    assert "quota exceeded" in recorder.events[0]["payload"]["error"]


async def test_partial_read_failure_still_notes():
    hits = [
        SearchHit(title="好页", url="https://a.com/ok", snippet="s"),
        SearchHit(title="死链", url="https://b.com/dead", snippet="s"),
    ]
    recorder = FakeRecorder()
    deps = make_deps(
        make_search(hits),
        make_read(error_urls={"https://b.com/dead"}),
        make_note(),
        recorder,
    )

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "sub_task_id": 7, "title": "标题"})

    assert final.get("error") is None
    assert len(final["pages"]) == 1
    assert final["sources"][0]["url"] == "https://a.com/ok"

    degrade_events = [e for e in recorder.events if e["type"] == EventType.degrade]
    assert len(degrade_events) == 3  # 三级全失败的降级轨迹
    assert degrade_events[0]["payload"]["from"] == "jina"
    assert degrade_events[-1]["payload"]["to"] is None

    fetch_events = [e for e in recorder.events if e["type"] == EventType.fetch]
    assert len(fetch_events) == 1


async def test_all_reads_fail_routes_to_end():
    hits = [
        SearchHit(title="a", url="https://a.com/1", snippet="s"),
        SearchHit(title="b", url="https://b.com/2", snippet="s"),
    ]
    recorder = FakeRecorder()
    deps = make_deps(
        make_search(hits),
        make_read(error_urls={"https://a.com/1", "https://b.com/2"}),
        make_note(),
        recorder,
    )

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "sub_task_id": 7, "title": "标题"})

    assert final["error"] == "all pages failed to read"
    degrade_events = [e for e in recorder.events if e["type"] == EventType.degrade]
    assert len(degrade_events) == 6  # 2 页 × 3 级
    control = [e for e in recorder.events if e["type"] == EventType.control]
    assert control[0]["payload"]["stage"] == "read"


async def test_url_dedup():
    hits = [
        SearchHit(title="同页", url="https://a.com/same", snippet="s"),
        SearchHit(title="重复", url="https://a.com/same", snippet="s"),
    ]
    deps = make_deps(make_search(hits), make_read(), make_note())

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "sub_task_id": 7, "title": "标题"})

    assert len(final["pages"]) == 1
    assert len(final["sources"]) == 1


async def test_content_hash_dedup():
    hits = [
        SearchHit(title="镜像 1", url="https://mirror1.com/x", snippet="s"),
        SearchHit(title="镜像 2", url="https://mirror2.com/x", snippet="s"),
    ]
    same_text = "完全相同的内容。" * 80

    async def read_page(url):
        return PageContent(
            url=url, provider="jina", title=f"镜像 {url}", text=same_text, latency_ms=100
        )

    deps = make_deps(make_search(hits), read_page, make_note())

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "sub_task_id": 7, "title": "标题"})

    assert len(final["pages"]) == 1
    assert final["sources"][0]["url"] == "https://mirror1.com/x"


async def test_note_failure_routes_to_end():
    hits = [SearchHit(title="t", url="https://a.com/1", snippet="s")]
    recorder = FakeRecorder()
    deps = make_deps(
        make_search(hits),
        make_read(),
        make_note(error=RuntimeError("deepseek 503")),
        recorder,
    )

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "sub_task_id": 7, "title": "标题"})

    assert "note generation failed" in final["error"]
    control = [e for e in recorder.events if e["type"] == EventType.control]
    assert control[0]["payload"]["stage"] == "note"
    assert "503" in control[0]["payload"]["error"]


async def test_max_pages_limit():
    hits = [SearchHit(title=f"页 {i}", url=f"https://a.com/{i}", snippet="s") for i in range(8)]
    note = make_note()
    deps = make_deps(make_search(hits), make_read(), note)

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "sub_task_id": 7, "title": "标题", "max_pages": 2})

    assert len(final["pages"]) == 2
    assert len(note.calls[0][1]) == 2  # write_note 只收到 2 页


class FakePersister:
    def __init__(self):
        self.calls = []

    async def persist_sources(self, task_id, sources):
        self.calls.append(("sources", task_id, sources))
        return {s["idx"]: 100 + s["idx"] for s in sources}

    async def persist_note(self, task_id, sub_task_id, note, idx_to_id):
        self.calls.append(("note", task_id, sub_task_id, note, idx_to_id))
        return 999

    async def add_usage(self, task_id, model, prompt_tokens, completion_tokens):
        self.calls.append(("usage", task_id, model, prompt_tokens, completion_tokens))


async def test_persister_wired_into_note_node():
    hits = [
        SearchHit(title="甲", url="https://www.thepaper.cn/a", snippet="s"),
        SearchHit(title="乙", url="https://36kr.com/b", snippet="s"),
    ]
    persister = FakePersister()
    deps = WorkerDeps(
        search=make_search(hits),
        read_page=make_read(),
        write_note=make_note(),
        recorder=FakeRecorder(),
        persister=persister,
    )

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 5, "sub_task_id": 9, "title": "标题"})

    assert final["note_db_id"] == 999

    kinds = [c[0] for c in persister.calls]
    assert kinds == ["sources", "note", "usage"]

    sources_call = persister.calls[0]
    assert sources_call[1] == 5
    assert len(sources_call[2]) == 2
    for s in sources_call[2]:
        assert "freshness" in s
        assert "freshness_basis" in s

    usage_call = persister.calls[2]
    assert usage_call[2] == "deepseek-chat"
    assert usage_call[3] == 3000  # FakeNote usage.prompt_tokens
    assert usage_call[4] == 400

    note_call = persister.calls[1]
    assert note_call[2] == 9
    assert note_call[4] == {1: 101, 2: 102}  # idx_to_id 传给锚点重写


async def test_no_persister_skips_persist():
    deps = make_deps(
        make_search([SearchHit(title="t", url="https://a.com/1", snippet="s")]),
        make_read(),
        make_note(),
    )

    graph = build_worker_graph(deps)
    final = await graph.ainvoke({"task_id": 1, "sub_task_id": 7, "title": "标题"})

    assert final.get("error") is None
    assert final["note_db_id"] is None
