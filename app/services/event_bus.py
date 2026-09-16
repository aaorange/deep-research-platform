"""Redis pub/sub 事件总线：agent_events 落库后向频道广播，供 SSE 端点实时转发。

publish 永不抛错（连不上 Redis 时对本事件循环静默禁用），
事件持久化以 DB 为准，总线只影响实时性，不影响正确性——
SSE 端点重连时仍按 seq 从 DB 增量回放。

pool 按事件循环键控：测试框架每个用例一个新 loop，
跨 loop 复用连接会报 "Event loop is closed"，按 loop 缓存天然隔离。
"""

import asyncio
import json
import logging

import redis.asyncio as aioredis
from redis.asyncio.client import PubSub

from app.config import get_settings

logger = logging.getLogger(__name__)

_pools: dict[int, aioredis.Redis] = {}
_disabled_loops: set[int] = set()


def channel_for(task_id: int) -> str:
    return f"research:task:{task_id}:events"


async def _get_pool() -> aioredis.Redis | None:
    loop_id = id(asyncio.get_running_loop())
    if loop_id in _disabled_loops:
        return None
    pool = _pools.get(loop_id)
    if pool is None:
        try:
            pool = aioredis.from_url(get_settings().redis_url, decode_responses=True)
            _pools[loop_id] = pool
        except Exception:
            _disabled_loops.add(loop_id)
            logger.warning("event bus: redis connect failed, realtime push disabled")
            return None
    return pool


async def publish_event(task_id: int, event: dict) -> None:
    """落库成功后调用；任何失败只记日志并禁用本 loop，不影响调用方。"""
    pool = await _get_pool()
    if pool is None:
        return
    try:
        await pool.publish(channel_for(task_id), json.dumps(event, ensure_ascii=False))
    except Exception:
        loop_id = id(asyncio.get_running_loop())
        _disabled_loops.add(loop_id)
        _pools.pop(loop_id, None)
        logger.warning("event bus: publish failed, realtime push disabled", exc_info=True)


async def open_subscription(task_id: int) -> PubSub | None:
    pool = await _get_pool()
    if pool is None:
        return None
    pubsub = pool.pubsub()
    await pubsub.subscribe(channel_for(task_id))
    return pubsub


async def close_subscription(pubsub: PubSub | None) -> None:
    if pubsub is not None:
        try:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        except Exception:
            logger.debug("event bus: close subscription error", exc_info=True)
