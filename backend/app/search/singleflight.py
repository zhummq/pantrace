"""同键并发去重（singleflight）。

对应 pansou `service/main_cache_lock.go`：同一缓存键同时未命中时，只放一个请求
打到上游，其余等它的结果。

**为什么必须有**：本站按请求次数给上游计费。用户连点两次搜索、首屏两个组件几乎
同时发请求、或者有人拿脚本并发刷同一个词——这些在缓存未命中时都会各自打一遍
上游。haisou 一次搜索是「平台数 × 2 页」个计费请求，N 个并发就是 2N 份额度，
而且上游对连续请求敏感（`code 1003`），并发越多越容易撞限流。

三条设计约束：

1. **异常也要广播**。leader 失败（504）时 follower 必须一起失败，否则它们各自
   重试，等于把这把锁原样还回去——最坏情况是失败放大成 N 倍计费。
2. **follower 不能无限等**。leader 卡住（上游挂起、后台任务迟迟不返回）时
   follower 到点自己降级跑一遍。宁可多花一份额度，也不能让用户陪着一起挂。
3. **结束立刻摘表**。不摘会随 key 数量无界增长，等于一个没有 TTL 的泄漏。

不负责缓存：结果要不要写缓存、写哪个键，由调用方的 factory 决定。这里只管
"同键只跑一次"。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# follower 等 leader 的上限。engine 一轮最坏是 TG 窗口(8s) + 接口窗口(10s)，
# 再留出组装与写库余量，30 秒足够覆盖；超过说明 leader 已经不正常了。
DEFAULT_WAIT_TIMEOUT = 30.0


class SingleFlight:
    """进程内（单事件循环）的同键去重。

    多 worker 部署时每个进程各自一张表，实际效果 = 每个进程各放一个请求。
    要跨进程精确到一次，得上 Redis 锁；本站单进程够用，接受这个误差。
    """

    def __init__(self, wait_timeout: float = DEFAULT_WAIT_TIMEOUT) -> None:
        self._inflight: dict[str, asyncio.Task] = {}
        self.wait_timeout = wait_timeout

    @property
    def inflight(self) -> int:
        """当前还在跑的键数。供 /api/health 观察是否有人把相同请求打成并发。"""
        return len(self._inflight)

    def is_inflight(self, key: str) -> bool:
        return key in self._inflight

    async def run(
        self, key: str, factory: Callable[[], Awaitable[T]]
    ) -> tuple[T, bool]:
        """执行或搭车。

        返回 `(结果, 是否搭了别人的车)`。第二个值给调用方记埋点用：
        搭车的请求没有单独打上游，`cached=True` 比 `upstream_ms=0` 更能说明原因。
        """
        task = self._inflight.get(key)

        if task is not None:
            try:
                # shield：wait_for 超时时只取消"等待"，不取消 leader 本身。
                # 不 shield 的话，第一个 follower 超时会把 leader 一起砍掉，
                # 后面所有 follower 连带收到 CancelledError。
                return await asyncio.wait_for(asyncio.shield(task), self.wait_timeout), True
            except asyncio.TimeoutError:
                logger.warning(
                    "[singleflight] 等待同键请求超过 %.1fs，降级为单独执行：%s",
                    self.wait_timeout,
                    key,
                )
                return await factory(), False

        task = asyncio.ensure_future(factory())
        self._inflight[key] = task
        try:
            return await task, False
        finally:
            # leader 结束（正常/异常/取消）都要摘，否则后续同键请求会搭上一个
            # 早已完成的 Task，拿到旧结果。
            self._inflight.pop(key, None)
