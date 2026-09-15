"""read_page 工具：Jina → crawl4ai → trafilatura 三级降级读页。

- L1 Jina Reader：免 key 返回干净 Markdown，限流约 20 RPM；
- L2 crawl4ai：本地 Chromium 渲染，兜住 JS 动态页；
- L3 trafilatura：httpx 直拉 + 抽正文，无 JS 能力但最稳。

任一级失败降级下一级，降级轨迹（tier/error/fell_to）随 PageContent 返回，
由调用方（D5 子图）通过 EventRecorder 记 degrade 事件。
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from app.config import get_settings
from app.tools.redis_client import get_redis

logger = logging.getLogger(__name__)

JINA_URL_PREFIX = "https://r.jina.ai/"
JINA_TIMEOUT = 15.0
JINA_RETRIES = 2
JINA_BACKOFF = 1.0
CRAWL4AI_PAGE_TIMEOUT = 30_000
DIRECT_TIMEOUT = 15.0
PAGE_CACHE_TTL = 24 * 3600
CACHE_PREFIX = "page:v1"

TIER_ORDER = ["jina", "crawl4ai", "trafilatura"]


@dataclass
class PageContent:
    url: str
    provider: str  # cache / jina / crawl4ai / trafilatura
    title: str | None
    text: str
    published_date: str | None = None
    latency_ms: int = 0
    degradations: list[dict] = field(default_factory=list)


class ReadPageError(RuntimeError):
    def __init__(self, url: str, degradations: list[dict]):
        super().__init__(f"all read tiers failed for {url}")
        self.url = url
        self.degradations = degradations


def _cache_key(url: str) -> str:
    return f"{CACHE_PREFIX}:{hashlib.md5(url.encode()).hexdigest()}"


def _clip(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars]


def _parse_jina(raw: str) -> tuple[str | None, str]:
    """Jina 返回格式：Title:/URL Source:/Markdown Content: 三行头 + 正文。"""
    title = None
    body = raw
    if raw.startswith("Title:"):
        lines = raw.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("Title:"):
                title = line[len("Title:") :].strip() or None
            if line.startswith("Markdown Content:"):
                body = "\n".join(lines[i + 1 :]).strip()
                break
    return title, body


async def _jina_fetch(url: str, client: httpx.AsyncClient) -> dict:
    settings = get_settings()
    headers = {"Accept": "text/plain"}
    if settings.jina_api_key:
        headers["Authorization"] = f"Bearer {settings.jina_api_key}"

    last_error: Exception | None = None
    for attempt in range(1, JINA_RETRIES + 1):
        try:
            resp = await client.get(JINA_URL_PREFIX + url, headers=headers, timeout=JINA_TIMEOUT)
            resp.raise_for_status()
            title, body = _parse_jina(resp.text)
            if not body.strip():
                raise ValueError("jina: empty body")
            return {"title": title, "text": body}
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as e:
            last_error = e
            if attempt < JINA_RETRIES:
                await asyncio.sleep(JINA_BACKOFF * attempt)
    raise RuntimeError(f"jina failed after {JINA_RETRIES} retries") from last_error


async def _crawl4ai_fetch(url: str, _client: httpx.AsyncClient) -> dict:
    from crawl4ai import (
        AsyncWebCrawler,
        BrowserConfig,
        CacheMode,
        CrawlerRunConfig,
    )

    browser = BrowserConfig(headless=True)
    run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=CRAWL4AI_PAGE_TIMEOUT)
    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url=url, config=run_cfg)
    if not result.success:
        raise RuntimeError(f"crawl4ai: {result.error_message}")
    md = result.markdown
    text = getattr(md, "raw_markdown", None) if not isinstance(md, str) else md
    if not text or not text.strip():
        raise ValueError("crawl4ai: empty markdown")
    title = (result.metadata or {}).get("title")
    return {"title": title, "text": text}


def _trafilatura_extract(html: str) -> dict:
    import trafilatura

    try:
        data = trafilatura.extract(html, output_format="json", with_metadata=True)
        if data:
            parsed = json.loads(data)
            text = parsed.get("text") or ""
            if text.strip():
                return {
                    "title": parsed.get("title"),
                    "text": text,
                    "published_date": parsed.get("date"),
                }
    except Exception:  # noqa: BLE001
        logger.debug("trafilatura json extract failed, fallback to plain")
    text = trafilatura.extract(html) or ""
    if not text.strip():
        raise ValueError("trafilatura: no main content extracted")
    return {"title": None, "text": text, "published_date": None}


async def _trafilatura_fetch(url: str, client: httpx.AsyncClient) -> dict:
    resp = await client.get(url, timeout=DIRECT_TIMEOUT)
    resp.raise_for_status()
    return await asyncio.to_thread(_trafilatura_extract, resp.text)


class _null_context:
    def __init__(self, obj):
        self.obj = obj

    async def __aenter__(self):
        return self.obj

    async def __aexit__(self, *exc):
        return False


async def read_page(
    url: str,
    *,
    max_chars: int = 12_000,
    min_chars: int = 200,
    force_tier: str | None = None,
    cache: object | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> PageContent:
    """读页并缓存。force_tier 用于单级测试（不读缓存、不降级）。"""
    start = time.perf_counter()

    tiers = {"jina": _jina_fetch, "crawl4ai": _crawl4ai_fetch, "trafilatura": _trafilatura_fetch}
    order = [force_tier] if force_tier else TIER_ORDER
    if any(name not in tiers for name in order):
        raise ValueError(f"unknown tier: {order}")

    cache_impl = cache if cache is not None else get_redis()
    key = _cache_key(url)

    if force_tier is None:
        cached = await cache_impl.get(key)
        if cached:
            payload = json.loads(cached)
            payload["latency_ms"] = int((time.perf_counter() - start) * 1000)
            return PageContent(url=url, provider="cache", degradations=[], **payload)

    degradations: list[dict] = []
    client_ctx = (
        _null_context(http_client)
        if http_client is not None
        else httpx.AsyncClient(follow_redirects=True)
    )

    async with client_ctx as client:
        for idx, name in enumerate(order):
            try:
                fetched = await tiers[name](url, client)
                text = _clip(fetched["text"], max_chars)
                if len(text.strip()) < min_chars:
                    raise ValueError(f"{name}: content too short ({len(text.strip())} chars)")
                page = PageContent(
                    url=url,
                    provider=name,
                    title=fetched.get("title"),
                    text=text,
                    published_date=fetched.get("published_date"),
                    latency_ms=int((time.perf_counter() - start) * 1000),
                    degradations=degradations,
                )
                if force_tier is None:
                    payload = {
                        "title": page.title,
                        "text": page.text,
                        "published_date": page.published_date,
                    }
                    await cache_impl.set(
                        key, json.dumps(payload, ensure_ascii=False), ex=PAGE_CACHE_TTL
                    )
                return page
            except Exception as e:  # noqa: BLE001
                fell_to = order[idx + 1] if idx + 1 < len(order) else None
                degradations.append(
                    {
                        "tier": name,
                        "fell_to": fell_to,
                        "error": f"{type(e).__name__}: {e}"[:300],
                    }
                )
                logger.warning("read_page degrade %s -> %s: %s", name, fell_to, e)

    raise ReadPageError(url, degradations)


def page_to_event(page: PageContent) -> dict:
    """生成 fetch 事件的 payload（D5 子图使用）。"""
    return {
        "url": page.url,
        "provider": page.provider,
        "title": page.title,
        "chars": len(page.text),
        "latency_ms": page.latency_ms,
    }


def degradation_events(page: PageContent) -> list[dict]:
    """降级轨迹转 degrade 事件 payload 列表（D5 子图使用）。"""
    return [{"from": d["tier"], "to": d["fell_to"], "error": d["error"]} for d in page.degradations]


__all__ = [
    "PageContent",
    "ReadPageError",
    "read_page",
    "page_to_event",
    "degradation_events",
]
