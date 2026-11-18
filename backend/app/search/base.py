"""插件基类：4 秒响应窗口 + 后台补齐 + 缓存回退。

**这个方法逐行对照 `pansou/plugin/plugin.go` 的 `AsyncSearch`（642-925 行）与
`refreshCacheInBackground`（1172-1237 行）翻译。** 逻辑一行没改，只在三处做了
语言层面的等价替换：

| Go | Python | 说明 |
|---|---|---|
| `go func(){...}()` + `doneChan` | `asyncio.create_task` + `shield` | 超时不取消底层任务，语义等价 |
| `select` 三分支 | `wait_for(shield(task))` + `except TimeoutError` | 结果/超时两条路径，失败在 task 内部转成 outcome |
| `chan` 传值 | task 返回值 | 不再需要错误通道：失败信息放进 `PluginOutcome.failure` |

`asyncio.shield` 是这里唯一不能省的东西：**不加它，`wait_for` 超时会取消底层任务，
"后台补齐"就成了一句空话。**
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

import httpx

from .cache import SearchCache, merge_results
from .observe import classify_failure, liveness
from .pool import WorkerPool, stats
from .types import PluginOutcome, SearchResult

log = logging.getLogger("pantrace.search.base")

# 与 pansou 同值：defaultAsyncResponseTimeout / defaultPluginTimeout / defaultCacheTTL
DEFAULT_RESPONSE_TIMEOUT = 4.0     # 前台窗口：到点就走，用户不等
DEFAULT_PLUGIN_TIMEOUT = 30.0      # 后台客户端：走了还继续抓
DEFAULT_CACHE_TTL = 3600.0

# 这些字段只控制行为、不影响结果内容，不能算进缓存键的摘要里。
# 算进去会导致"刷新"这个动作自己劈出一个新缓存槽，绕过旧值。
_NO_DIGEST_KEYS = frozenset({"refresh", "ctx", "main_cache_key", "caller"})


def ext_digest(ext: dict | None) -> str:
    """对影响结果的参数做摘要。对应 pansou 的 `util.ExtDigest`。

    缓存键缺了它，`{"pages_per_type": 3}` 的请求会命中只抓 1 页的缓存。
    """
    if not ext:
        return "0"
    payload = {k: v for k, v in ext.items() if k not in _NO_DIGEST_KEYS}
    if not payload:
        return "0"
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def build_clients(
    timeout_fast: float = DEFAULT_RESPONSE_TIMEOUT,
    timeout_long: float = DEFAULT_PLUGIN_TIMEOUT,
    proxy: str | None = None,
) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    """双超时客户端。对应 pansou 的 `GetClient()` / `backgroundHTTPClient()`。

    `trust_env=False` 是硬要求：本机环境里有 `HTTP_PROXY`，httpx 默认会把它挂上，
    于是"直连"这个配置在本机实际走了代理，结论和线上完全相反。
    项目主上游早就为此设了 `use_env_proxy=False`。
    """
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=16)
    common = dict(trust_env=False, proxy=proxy, limits=limits, follow_redirects=True)
    return (
        httpx.AsyncClient(timeout=timeout_fast, **common),
        httpx.AsyncClient(timeout=timeout_long, **common),
    )


class BasePlugin:
    """所有源的基类。子类只需实现 `fetch()`。

    实例是全局共享单例 —— **请求态一律走参数，禁止写实例字段**。
    pansou 为此专门把 ctx 和主缓存键挪进 ext：`SetCurrentKeyword` 这类写法在并发下
    会让 A 关键词的结果写进 B 的缓存槽。
    """

    name: str = ""
    level: int = 3                      # 1~4，进排序分（level1 +1000 / level2 +500 / level3 0 / level4 -200）
    response_timeout: float = DEFAULT_RESPONSE_TIMEOUT
    cache_ttl: float = DEFAULT_CACHE_TTL
    channel: str = ""                   # TG 频道用；普通插件留空

    # 通道类型，对应 pansou 的 `sourceType`（src 参数）。引擎按它分流：
    #   "plugin" —— 走接口/网页抓取（haisou 之类）
    #   "tg"     —— 走 Telegram 频道
    # pansou 里这是两条独立的并行分支；我们统一成插件，靠这个属性还原分流能力。
    # **必须显式声明**，不能靠插件名猜：名字是用户可改的配置项，类型不是。
    source_kind: str = "plugin"

    # 由 registry.load_all() 统一注入，所有插件共享同一份
    cache: SearchCache | None = None
    pool: WorkerPool | None = None
    client_fast: httpx.AsyncClient | None = None
    client_long: httpx.AsyncClient | None = None

    # ---------- 子类实现 ----------

    async def fetch(self, client: httpx.AsyncClient, keyword: str, ext: dict) -> list[SearchResult]:
        raise NotImplementedError

    # ---------- 主流程（AsyncSearch 的翻译） ----------

    def cache_key(self, keyword: str, ext: dict | None = None) -> str:
        """`name:keyword:ext_digest`。少 name 各源串台，少 ext 拿错结果。"""
        return f"{self.name}:{keyword}:{ext_digest(ext)}"

    async def search(self, keyword: str, ext: dict | None = None) -> PluginOutcome:
        ext = dict(ext or {})
        started = time.time()
        cache_key = self.cache_key(keyword, ext)
        force_refresh = bool(ext.get("refresh"))

        # ---- 1. 查缓存（未强制刷新时） ----
        if not force_refresh:
            cached = self.cache.get(cache_key) if self.cache else None
            if cached is not None:
                age = started - cached.timestamp

                # 完全有效：未过期 且 完整
                if age < self.cache_ttl and cached.complete:
                    stats.cache_hit += 1
                    # 用到 80% 寿命就后台刷，用户仍拿旧值（stale-while-revalidate）
                    if age > self.cache_ttl * 4 / 5:
                        self._spawn_refresh(keyword, ext, cache_key, cached)
                    return PluginOutcome(source=self.name, results=cached.results, is_final=True)

                # 过期但有结果：先给旧的，同时后台刷。
                # 注意 pansou 的实测结论：真正让结果变多的是"缓存不死、后台合并往里加"，
                # 不是过期重搜（重搜结果是 -32 / -72 条）。所以别为了让缓存"新鲜"去缩 TTL。
                if cached.results:
                    stats.cache_hit += 1
                    if age >= self.cache_ttl:
                        self._spawn_refresh(keyword, ext, cache_key, cached)
                    return PluginOutcome(source=self.name, results=cached.results, is_final=True)

        stats.cache_miss += 1

        # ---- 2. 起后台任务，前台只等 response_timeout ----
        task = asyncio.create_task(self._worker(keyword, ext, cache_key, started))
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.response_timeout)
        except asyncio.TimeoutError:
            # 到点就走。task 没有被取消（shield），它会继续跑完并写缓存。
            return self._partial(cache_key, started)

    async def _worker(self, keyword: str, ext: dict, cache_key: str, started: float) -> PluginOutcome:
        """后台主体。对应 `AsyncSearch` 里的 goroutine。

        不往外抛异常：超时后这个 task 的返回值已经没人接了，抛出去只会变成
        "Task exception was never retrieved" 的噪音日志。
        """
        acquired = bool(self.pool and self.pool.try_acquire())
        # 拿不到槽就降级用短超时客户端跑，**不排队** —— 排队会把
        # "第二个用户等第一个"的老问题原样带回来。
        client = self.client_long if acquired else self.client_fast
        if client is None:
            raise RuntimeError(f"[{self.name}] 未注入 HTTP 客户端，请先调用 registry.load_all()")

        try:
            results = await self.fetch(client, keyword, ext)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = classify_failure(exc)
            # 失败回退旧缓存：与超时路径一致。否则这个源会因为一次失败就从结果里凭空消失，
            # `refresh=true` 时尤其明显（刷新会跳过缓存，失败就直接返回空）。
            cached = self.cache.get(cache_key) if self.cache else None
            if cached is not None and cached.results:
                stats.fallback_to_cache += 1
                liveness.observe_plugin(self.name, len(cached.results), exc)
                log.warning("[%s] 抓取失败，回退旧缓存 %d 条（%s）: %s", self.name, len(cached.results), failure.value, exc)
                return PluginOutcome(
                    source=self.name,
                    results=cached.results,
                    is_final=True,
                    failure=failure,
                    message=f"抓取失败，已回退旧缓存: {exc}",
                )
            liveness.observe_plugin(self.name, 0, exc)
            log.warning("[%s] 抓取失败且无缓存（%s）: %s", self.name, failure.value, exc)
            return PluginOutcome(source=self.name, is_final=True, failure=failure, message=str(exc))
        finally:
            if acquired and self.pool:
                self.pool.release()

        # ---- 写单源缓存：读 → 合并 → 写回（不是覆盖） ----
        merged = results
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None and cached.results:
                merged = merge_results(results, cached.results)
            self.cache.merge_put(cache_key, results, complete=True, now=started)

        liveness.observe_plugin(self.name, len(merged), None)
        stats.async_completed += 1
        return PluginOutcome(source=self.name, results=merged, is_final=True)

    def _partial(self, cache_key: str, started: float) -> PluginOutcome:
        """超时/取消的返回。对应 `AsyncSearch` 里的 `partial()`。"""
        stats.partial_returned += 1
        cached = self.cache.get(cache_key) if self.cache else None

        # 有旧结果就先给用户：用户拿到的是"上次的"，而不是空白
        if cached is not None and cached.results:
            return PluginOutcome(source=self.name, results=cached.results, is_final=False)

        # 没有旧结果：写一条空 + complete=False。
        # 这条记录的作用不是"缓存空结果"，而是告诉下次请求"上一轮没跑完"，
        # 所以它一定不能带 complete=True，否则空结果会被钉住一整个 TTL。
        if self.cache:
            self.cache.put(cache_key, [], complete=False, now=started)

        # 这里不记 liveness：这一轮还没结束，记成 zero_yield 是误判。
        # 对应 pansou "超时未完成"与"零产出"分开统计。
        return PluginOutcome(source=self.name, results=[], is_final=False)

    # ---------- SWR 后台刷新 ----------

    def _spawn_refresh(self, keyword: str, ext: dict, cache_key: str, old) -> None:
        asyncio.create_task(self._refresh(keyword, ext, cache_key, old))

    async def _refresh(self, keyword: str, ext: dict, cache_key: str, old) -> None:
        """后台刷新缓存。对应 `refreshCacheInBackground`。

        两条与 Go 一致的提前退出：
        - 拿不到工作槽**直接放弃**（这一步不降级，避免在满负载时再加请求）
        - 抓到空结果**不覆盖缓存**（空结果不代表旧结果失效）
        """
        acquired = bool(self.pool and self.pool.try_acquire())
        if self.pool and not acquired:
            return
        client = self.client_long if client_ok(self.client_long) else self.client_fast
        if client is None:
            return
        try:
            results = await self.fetch(client, keyword, ext)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("[%s] 后台刷新失败: %s", self.name, exc)
            return
        finally:
            if acquired and self.pool:
                self.pool.release()

        if not results:
            return

        merged = merge_results(results, old.results)
        if self.cache:
            self.cache.merge_put(cache_key, merged, complete=True)
        log.info("[%s] 后台刷新完成 %s：新 %d 条 → 合并 %d 条", self.name, cache_key, len(results), len(merged))


def client_ok(client: httpx.AsyncClient | None) -> bool:
    return client is not None and not client.is_closed
