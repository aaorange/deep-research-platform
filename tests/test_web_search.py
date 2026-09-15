import json

import httpx
import pytest

import app.tools.web_search as ws
from app.tools.web_search import SearchHit, web_search


class FakeCache:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _bocha_response(status_code=200, n_hits=2):
    hits = [
        {
            "name": f"结果 {i}",
            "url": f"https://example.com/{i}",
            "snippet": f"摘要 {i}",
            "siteName": "example",
        }
        for i in range(n_hits)
    ]
    payload = {"code": 200, "data": {"webPages": {"value": hits}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_bocha_success_and_cache_write():
    cache = FakeCache()
    client = _client(_bocha_response())
    result = await web_search("大模型 医疗", 5, cache=cache, http_client=client)

    assert result.provider == "bocha"
    assert len(result.hits) == 2
    assert result.hits[0].url == "https://example.com/0"
    assert result.latency_ms >= 0
    assert len(cache.store) == 1
    stored = json.loads(next(iter(cache.store.values())))
    assert stored[0]["title"] == "结果 0"


async def test_cache_hit_returns_without_request():
    cache = FakeCache()
    key = ws._cache_key("大模型 医疗", 5)
    cache.store[key] = json.dumps(
        [{"title": "缓存项", "url": "https://cached.com", "snippet": "s"}]
    )

    def fail_handler(request):
        raise AssertionError("cache hit must not reach network")

    client = _client(fail_handler)
    result = await web_search("大模型 医疗", 5, cache=cache, http_client=client)

    assert result.provider == "cache"
    assert result.hits[0].url == "https://cached.com"


async def test_bocha_retry_then_success(monkeypatch):
    monkeypatch.setattr(ws, "BOCHA_BACKOFF", 0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"code": 500})
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "webPages": {
                        "value": [{"name": "重试成功", "url": "https://ok.com", "snippet": "s"}]
                    }
                },
            },
        )

    cache = FakeCache()
    client = _client(handler)
    result = await web_search("重试", 5, cache=cache, http_client=client)

    assert result.provider == "bocha"
    assert calls["n"] == 3


async def test_fallback_to_ddg(monkeypatch):
    monkeypatch.setattr(ws, "BOCHA_BACKOFF", 0)

    async def fake_ddg(query, count):
        return [SearchHit(title="DDG 结果", url="https://ddg.com", snippet="s")]

    monkeypatch.setattr(ws, "_ddg_search", fake_ddg)

    def handler(request):
        return httpx.Response(500, json={"code": 500})

    cache = FakeCache()
    client = _client(handler)
    result = await web_search("兜底", 5, cache=cache, http_client=client)

    assert result.provider == "ddg"
    assert result.hits[0].url == "https://ddg.com"
    assert len(cache.store) == 1


async def test_all_channels_failed(monkeypatch):
    monkeypatch.setattr(ws, "BOCHA_BACKOFF", 0)

    async def fake_ddg(query, count):
        raise ValueError("ddg returned no usable hits")

    monkeypatch.setattr(ws, "_ddg_search", fake_ddg)

    def handler(request):
        return httpx.Response(401, json={"code": 401, "msg": "bad key"})

    cache = FakeCache()
    client = _client(handler)
    with pytest.raises(ValueError, match="ddg returned no usable hits"):
        await web_search("全失败", 5, cache=cache, http_client=client)
    assert len(cache.store) == 0
