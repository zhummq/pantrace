"""TG 阶段编排。

逐行翻译 pansou `service/search_service.go:1363-1454`（`searchTG`）与
`:1458-1510`（`backfillTGChannels`）。

这里只做编排，判定全部交给已有模块：
- 门：`gate.TG.is_reachable()`，门关时**早返回、不写缓存、不计频道失败**
- 统计：`BatchOutcome`（全失败不写 / 整批零产出不写 / 超时超 1/3 不补）
- 缓存：`SearchCache.put` 首轮、`merge_put` 补齐（合并写，不许覆盖）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from ..base import ext_digest
from ..cache import SearchCache
from ..observe import liveness
from ..outcome import BatchOutcome
from ..types import SearchResult
from .channel import DEFAULT_CHANNEL_REQUEST_TIMEOUT, search_channel
from .gate import TG, TGReachability

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL_TIMEOUT = 4.0
DEFAULT_CHANNEL_CONCURRENCY = 32


def tg_cache_key(keyword: str, channels: list[str]) -> str:
    """`generateTGCacheKey`。频道列表进摘要 —— 换频道必须换键。"""
    return f"tg:{keyword}:{ext_digest({'channels': list(channels)})}"


async def _run_batch(
    factories: list[Callable[[], Awaitable]],
    max_workers: int,
    timeout: float,
) -> list:
    """worker_pool.go:198 `ExecuteBatchWithTimeout`。

    超时后：已完成的照常返回，**其余不取消**（Go 是 `go pool.Shutdown(false)`
    让它们跑完）。这里靠不给 pending 任务 cancel 达到同样效果，只挂一个回调
    吞掉异常——否则会变成 "Task exception was never retrieved" 的噪音。
    """
    if not factories:
        return []

    max_workers = max(1, min(max_workers, len(factories)))
    sem = asyncio.Semaphore(max_workers)

    async def guarded(factory):
        async with sem:
            return await factory()

    tasks = [asyncio.create_task(guarded(f)) for f in factories]
    done, pending = await asyncio.wait(tasks, timeout=timeout)

    for task in pending:
        task.add_done_callback(_swallow)

    return [t.result() for t in done if not t.cancelled() and t.exception() is None]


def _swallow(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("[searchTG] 超时后完成的任务报错（已忽略）: %s", exc)


async def search_tg(
    keyword: str,
    channels: list[str],
    cache: SearchCache | None = None,
    client: httpx.AsyncClient | None = None,
    force_refresh: bool = False,
    channel_timeout: float = DEFAULT_CHANNEL_TIMEOUT,
    request_timeout: float = DEFAULT_CHANNEL_REQUEST_TIMEOUT,
    backfill_enabled: bool = True,
    concurrency: int = DEFAULT_CHANNEL_CONCURRENCY,
    gate: TGReachability | None = None,
) -> list[SearchResult]:
    """search_service.go:1364 `searchTG`。"""
    gate = gate or TG
    cache_key = tg_cache_key(keyword, channels)

    # ---- 缓存命中直接返回，不检查新鲜度（和 Go 一致） ----
    if not force_refresh and cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached.results

    # ---- 可达性门 ----
    # 门关时直接返回：不写缓存（网络不通的空结果写进 TTL 会在恢复后继续骗人），
    # 也不计入频道存活失败（这些频道根本没被试过）。
    if not gate.is_reachable():
        logger.info("[searchTG] %s：t.me 当前不可达，跳过 TG 阶段（%s）", keyword, gate.reason)
        return []

    # ---- 并发搜索所有频道 ----
    if channel_timeout <= 0:
        channel_timeout = request_timeout
    if channel_timeout <= 0:
        channel_timeout = DEFAULT_CHANNEL_TIMEOUT

    def make_factory(ch: str):
        async def run():
            try:
                results = await search_channel(
                    keyword, ch, client=client, timeout=request_timeout
                )
                return ch, results, None
            except Exception as exc:  # 频道级失败不能拖垮整批
                return ch, [], exc

        return run

    factories = [make_factory(ch) for ch in channels]
    task_results = await _run_batch(factories, concurrency, channel_timeout)

    # ---- 合并结果，统计成功 / 失败 / 超时未完成 ----
    outcome = BatchOutcome()
    results: list[SearchResult] = []
    for item in task_results:
        if not isinstance(item, tuple) or len(item) != 3:
            continue
        channel, channel_results, err = item
        outcome.observe(channel, len(channel_results), err)
        if err is None:
            results.extend(channel_results)
        # 频道侧同样累积，便于在 /api/health 里看到哪个频道长期没动静
        liveness.observe_channel(channel, len(channel_results), err)

    outcome.finalize(list(channels))
    logger.info("[searchTG] %s：%s", keyword, outcome.summary())

    # ---- 缓存写入按完整度分流 ----
    if cache is not None:
        should_write, reason = outcome.cache_decision()
        if should_write:
            cache.put(cache_key, results, complete=outcome.complete())
        else:
            logger.info("[searchTG] %s：不写缓存（%s）", keyword, reason)

    # ---- 后台补齐超时未完成的频道 ----
    if (
        cache is not None
        and backfill_enabled
        and outcome.should_backfill(True)
        and outcome.missing
    ):
        asyncio.create_task(
            _backfill(
                cache=cache,
                cache_key=cache_key,
                keyword=keyword,
                missing=list(outcome.missing),
                total=len(channels),
                collected=results,
                client=client,
                request_timeout=request_timeout,
                concurrency=concurrency,
            )
        )

    return results


async def _backfill(
    cache: SearchCache,
    cache_key: str,
    keyword: str,
    missing: list[str],
    total: int,
    collected: list[SearchResult],
    client: httpx.AsyncClient | None,
    request_timeout: float,
    concurrency: int,
) -> None:
    """search_service.go:1458 `backfillTGChannels`。

    补齐成功后走 merge_put：**先与缓存里已有的合并、并按缓存键互斥**。
    原先这里是整块覆盖，而补齐发生在首轮写入之后很久，这期间任何一次重复搜索
    都可能已经把结果并进同一个键 —— 覆盖会把它们吞掉（实测丢了 74%）。
    """
    if not missing:
        return

    if request_timeout <= 0:
        request_timeout = DEFAULT_CHANNEL_REQUEST_TIMEOUT

    # 注意这里是 def 不是 async def：`_run_batch` 要的是"可调用的协程工厂"，
    # 写成 async def 会返回一个协程对象，`guarded()` 里调用时抛 TypeError，
    # 被上层当成频道失败吞掉 —— 后台补齐会永久静默失效。
    def factory(ch: str):
        async def run():
            try:
                return await search_channel(keyword, ch, client=client, timeout=request_timeout)
            except Exception:
                return None

        return run

    backfill_results = await _run_batch(
        [factory(ch) for ch in missing], concurrency, request_timeout
    )

    added = 0
    merged = list(collected)
    for item in backfill_results:
        if not isinstance(item, list):
            continue
        added += 1
        merged.extend(item)

    if added == 0:
        return

    cache.merge_put(cache_key, merged, complete=True)
    logger.info(
        "[searchTG] %s：后台补齐 %d/%d 个超时频道，缓存已更新为完整结果（本次 %d 条 -> 合并后 %d 条）",
        keyword,
        added,
        total,
        len(collected),
        len(merged),
    )
