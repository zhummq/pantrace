"""后台工作池。对应 pansou `plugin/plugin.go` 的 acquireWorkerSlot / releaseWorkerSlot。

`defaultMaxBackgroundWorkers = 20`、`defaultMaxBackgroundTasks = 100`，两个上限是分开的：
前者限制"同时跑几个"，后者限制"总共欠着几个没跑完"。

## 为什么不用 asyncio.Semaphore

Go 那边是：

    select {
    case pool <- struct{}{}:   // 拿到槽
    default:                   // 拿不到 → 立刻返回 false，降级跑
    }

Python 没有 `try_acquire`。直觉写法是 `await wait_for(sem.acquire(), timeout=0)`，
但 `wait_for` 在 timeout=0 时**先把 coroutine 包成 task 再立即超时**，
即使槽是空的也会抛 TimeoutError —— 结果是"永远拿不到槽"，降级变成唯一路径。

正确做法是在事件循环里用**不含 await 的同步方法**改计数器：单线程事件循环下
没有 await 就没有让出点，这段必然原子，等价于 Go 的 channel + default。
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("pantrace.search.pool")

# 与 pansou 同值，不要随手调大：这两个数决定的是"打出去的并发量"，不是"性能"
DEFAULT_MAX_WORKERS = 20
DEFAULT_MAX_TASKS = 100


class WorkerPool:
    """非阻塞工作池：拿不到槽返回 False，由调用方降级执行，**不排队**。

    排队会把"第二个用户等第一个"的老问题原样带回来 —— 那正是这次改造要消掉的东西。
    """

    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS, max_tasks: int = DEFAULT_MAX_TASKS):
        self.max_workers = max_workers
        self.max_tasks = max_tasks
        self._in_use = 0          # 正在占用 worker 槽的数量
        self._outstanding = 0     # 已受理但还没跑完的任务总数（含排队概念，这里不排队只计数）
        self.rejected = 0         # 降级次数，进 /api/health 看负载

    def try_acquire(self) -> bool:
        """尝试拿一个槽。同步方法，无 await —— 见模块 docstring。"""
        if self._outstanding >= self.max_tasks:
            self.rejected += 1
            return False
        if self._in_use >= self.max_workers:
            self.rejected += 1
            return False
        self._in_use += 1
        self._outstanding += 1
        return True

    def release(self) -> None:
        if self._in_use > 0:
            self._in_use -= 1
        if self._outstanding > 0:
            self._outstanding -= 1

    def release_task_only(self) -> None:
        """降级路径用：没占 worker 槽，但受理计数要减掉。"""
        if self._outstanding > 0:
            self._outstanding -= 1

    def stats(self) -> dict[str, int]:
        return {
            "in_use": self._in_use,
            "outstanding": self._outstanding,
            "max_workers": self.max_workers,
            "max_tasks": self.max_tasks,
            "rejected": self.rejected,
        }

    async def __aenter__(self) -> bool:
        return self.try_acquire()

    async def __aexit__(self, *exc) -> None:
        self.release()


class Stats:
    """缓存/补齐全过程的计数器，暴露给 /api/health。

    对应 pansou 的 recordCacheHit / recordCacheMiss / recordAsyncCompletion。
    """

    def __init__(self) -> None:
        self.cache_hit = 0
        self.cache_miss = 0
        self.async_completed = 0      # 后台补齐真正跑完的次数
        self.partial_returned = 0     # 超时先返回空/旧值的次数
        self.fallback_to_cache = 0    # 抓取失败回退旧缓存的次数

    def snapshot(self) -> dict[str, int]:
        return {
            "cache_hit": self.cache_hit,
            "cache_miss": self.cache_miss,
            "async_completed": self.async_completed,
            "partial_returned": self.partial_returned,
            "fallback_to_cache": self.fallback_to_cache,
        }


stats = Stats()
