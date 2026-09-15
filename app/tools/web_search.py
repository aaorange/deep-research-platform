"""web_search 工具：博查主通道 + DDG 兜底 + Redis 24h 缓存。

调用链：cache 命中直接返回；否则博查（超时 10s，网络错误/5xx 指数退避重试 3 次），
重试耗尽切 DDG；任一通道成功后写缓存。provider 字段供上层记录动作流事件。
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass

import httpx

from app.config import get_settings
from app.tools.redis_client import get_redis

logger = logging.getLogger(__name__)

BOCHA_URL = "https://api.bochaai.com/v1/web-search"
CACHE_TTL = 24 * 3600
CACHE_PREFIX = "search:v1"
BOCHA_TIMEOUT = 10.0
BOCHA_RETRIES = 3
BOCHA_BACKOFF = 0.5


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    site_name: str | None = None


@dataclass
class SearchResult:
    query: str
    provider: str  # cache / bocha / ddg
    hits: list[SearchHit]
    latency_ms: int


def _cache_key(query: str, count: int) -> str:
    digest = hashlib.md5(f"{query}|{count}".encode()).hexdigest()
    return f"{CACHE_PREFIX}:{digest}"


def _parse_bocha(data: dict) -> list[SearchHit]:
    pages = (data.get("data") or {}).get("webPages") or {}
    hits = []
    for item in pages.get("value") or []:
        title = item.get("name") or ""
        url = item.get("url") or ""
        snippet = item.get("snippet") or ""
        if title and url:
            hits.append(
                SearchHit(
                    title=title,
                    url=url,
                    snippet=snippet,
                    site_name=item.get("siteName"),
                )
            )
    return hits


async def _bocha_search(query: str, count: int, client: httpx.AsyncClient) -> list[SearchHit]:
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.bocha_api_key}"}
    body = {"query": query, "count": count, "summary": False}

    last_error: Exception | None = None
    for attempt in range(1, BOCHA_RETRIES + 1):
        try:
            resp = await client.post(BOCHA_URL, json=body, headers=headers, timeout=BOCHA_TIMEOUT)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"bocha {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            hits = _parse_bocha(resp.json())
            if not hits:
                raise ValueError("bocha returned no usable hits")
            return hits
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as e:
            last_error = e
            if attempt < BOCHA_RETRIES:
                await asyncio.sleep(BOCHA_BACKOFF * (2 ** (attempt - 1)))
    raise RuntimeError(f"bocha failed after {BOCHA_RETRIES} retries") from last_error


def _ddg_search_sync(query: str, count: int) -> list[SearchHit]:
    from ddgs import DDGS

    with DDGS(timeout=BOCHA_TIMEOUT) as ddgs:
        raw = ddgs.text(query, max_results=count)
    hits = []
    for item in raw:
        url = item.get("href") or ""
        title = item.get("title") or ""
        if url and title:
            hits.append(SearchHit(title=title, url=url, snippet=item.get("body") or ""))
    if not hits:
        raise ValueError("ddg returned no usable hits")
    return hits


async def _ddg_search(query: str, count: int) -> list[SearchHit]:
    return await asyncio.to_thread(_ddg_search_sync, query, count)


async def web_search(
    query: str,
    count: int = 5,
    *,
    cache: object | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> SearchResult:
    """搜索并缓存。cache/http_client 仅测试注入用。"""
    start = time.perf_counter()
    cache_impl = cache if cache is not None else get_redis()
    key = _cache_key(query, count)

    cached = await cache_impl.get(key)
    if cached:
        hits = [SearchHit(**h) for h in json.loads(cached)]
        return SearchResult(
            query=query,
            provider="cache",
            hits=hits,
            latency_ms=int((time.perf_counter() - start) * 1000),
        )

    provider = "bocha"
    client_ctx = _null_context(http_client) if http_client is not None else httpx.AsyncClient()
    try:
        try:
            async with client_ctx as client:
                hits = await _bocha_search(query, count, client)
        except Exception as e:
            logger.warning("bocha degraded, falling back to ddg: %s", e)
            provider = "ddg"
            hits = await _ddg_search(query, count)
    except Exception:
        logger.exception("all search channels failed: query=%r", query)
        raise

    payload = json.dumps([asdict(h) for h in hits], ensure_ascii=False)
    await cache_impl.set(key, payload, ex=CACHE_TTL)

    return SearchResult(
        query=query,
        provider=provider,
        hits=hits,
        latency_ms=int((time.perf_counter() - start) * 1000),
    )


class _null_context:
    def __init__(self, obj):
        self.obj = obj

    async def __aenter__(self):
        return self.obj

    async def __aexit__(self, *exc):
        return False
