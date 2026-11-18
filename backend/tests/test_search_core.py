"""搜索核心层的离线测试。

只验证**机制**，不碰网络。这里每条用例都对应 pansou 注释里记过的一个事故，
改代码时先看这些用例过不过。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_search_core -v``
"""

from __future__ import annotations

import asyncio
import time
import unittest

from app.search.base import BasePlugin
from app.search.cache import SearchCache, merge_results
from app.search.observe import HttpStatusError, Liveness, classify_failure, liveness
from app.search.pool import WorkerPool, stats
from app.search.types import FailureClass, Link, SearchResult


def make_result(uid: str, title: str = "", n_links: int = 1) -> SearchResult:
    return SearchResult(
        unique_id=uid,
        title=title or uid,
        links=[Link(type="quark", url=f"https://pan.quark.cn/s/{uid}{i}") for i in range(n_links)],
    )


class Fake(BasePlugin):
    """可控的假插件：delay + 回什么/抛什么。"""

    name = "fake"
    delay = 0.01
    payload: list[SearchResult] = []
    error: BaseException | None = None

    async def fetch(self, client, keyword, ext):
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return list(self.payload)


def build(cls, *, cache: SearchCache, pool: WorkerPool, timeout: float = 0.2) -> BasePlugin:
    inst = cls()
    inst.cache = cache
    inst.pool = pool
    inst.response_timeout = timeout
    # 不注入真实客户端：fetch 是假的，客户端不会被用到
    inst.client_fast = object()          # type: ignore[assignment]
    inst.client_long = object()          # type: ignore[assignment]
    return inst


class TestWindowAndBackfill(unittest.TestCase):
    """4 秒窗口 + 后台补齐：慢源不能拖住快的，也不能被取消。"""

    def setUp(self) -> None:
        self.cache = SearchCache()        # 纯内存
        self.pool = WorkerPool()

    def test_fast_returns_final(self):
        class Fast(Fake):
            name = "fast"

        p = build(Fast, cache=self.cache, pool=self.pool)
        p.delay = 0.01
        p.payload = [make_result("a")]

        out = asyncio.run(p.search("三体"))
        self.assertTrue(out.is_final)
        self.assertEqual(len(out.results), 1)
        self.assertIsNone(out.failure)

    def test_slow_times_out_but_keeps_running(self):
        """超时返回空且 is_final=False，但底层任务**没被取消**，稍后缓存会被写上。"""
        class Slow(Fake):
            name = "slow"

        p = build(Slow, cache=self.cache, pool=self.pool, timeout=0.1)
        p.delay = 0.5
        p.payload = [make_result("b1"), make_result("b2")]

        async def scenario():
            out = await p.search("遮天")
            # 超时那一刻：空结果、未完成
            first = (len(out.results), out.is_final, out.failure)
            # 等后台跑完（shield 保证它还在）
            await asyncio.sleep(0.7)
            return first, self.cache.get(p.cache_key("遮天"))

        first, cached = asyncio.run(scenario())
        self.assertEqual(first[0], 0, "超时应当返回空结果")
        self.assertFalse(first[1], "超时结果必须标记 is_final=False")
        self.assertIsNone(first[2], "超时是 pending，不算失败")

        self.assertIsNotNone(cached, "后台补齐必须写进缓存 —— shield 没生效时这里会是 None")
        assert cached is not None
        self.assertTrue(cached.complete, "补齐完成后必须标记 complete")
        self.assertEqual(len(cached.results), 2)

    def test_fast_not_blocked_by_slow(self):
        """一快一慢并发：快的不能被慢的拖到超时。"""
        class Fast(Fake):
            name = "fast2"

        class Slow(Fake):
            name = "slow2"

        fast = build(Fast, cache=self.cache, pool=self.pool, timeout=0.2)
        fast.delay = 0.02
        fast.payload = [make_result("f")]
        slow = build(Slow, cache=self.cache, pool=self.pool, timeout=0.2)
        slow.delay = 2.0
        slow.payload = [make_result("s")]

        async def scenario():
            t0 = time.perf_counter()
            fo, so = await asyncio.gather(fast.search("x"), slow.search("x"))
            return time.perf_counter() - t0, fo, so

        elapsed, fo, so = asyncio.run(scenario())
        self.assertLess(elapsed, 0.5, f"快的源被拖慢了：{elapsed:.2f}s")
        self.assertTrue(fo.is_final)
        self.assertEqual(len(fo.results), 1)
        self.assertFalse(so.is_final, "慢源应当超时返回")


class TestCacheSemantics(unittest.TestCase):
    """缓存的 complete 语义 + 合并写。"""

    def setUp(self) -> None:
        self.cache = SearchCache()
        self.pool = WorkerPool()

    def test_second_call_hits_cache(self):
        class P(Fake):
            name = "cached"

        p = build(P, cache=self.cache, pool=self.pool, timeout=1.0)
        p.delay = 0.01
        p.payload = [make_result("c1")]
        calls = {"n": 0}

        orig = p.fetch

        async def counting(client, keyword, ext):
            calls["n"] += 1
            return await orig(client, keyword, ext)

        p.fetch = counting  # type: ignore[method-assign]

        asyncio.run(p.search("三体"))
        asyncio.run(p.search("三体"))
        self.assertEqual(calls["n"], 1, "第二次同词必须命中缓存，不重复抓")

    def test_timeout_writes_incomplete_marker(self):
        """超时且无旧缓存时，要落一条 complete=False，让下次知道该重抓。

        关键在于读的时机：必须在那次超时刚返回、后台还没补完时读。
        写错成"睡一觉再读"的话，读到的是后台补完的 complete=True，这条用例就变成了假测试。
        """
        class Slow3(Fake):
            name = "incomplete"

        p = build(Slow3, cache=self.cache, pool=self.pool, timeout=0.05)
        p.delay = 1.0
        p.payload = [make_result("late")]

        async def scenario():
            out = await p.search("这个词")
            return out, self.cache.get(p.cache_key("这个词"))

        out, entry = asyncio.run(scenario())
        self.assertFalse(out.is_final)
        self.assertEqual(len(out.results), 0)
        self.assertIsNotNone(entry, "超时必须留下一条记录，否则下次还会当成从未搜过")
        assert entry is not None
        self.assertFalse(entry.complete, "超时留下的记录必须是 complete=False，否则空结果会被钉住一整个 TTL")
        self.assertEqual(len(entry.results), 0)

    def test_merge_put_does_not_drop_concurrent_results(self):
        """合并写：后写的补齐不能吞掉之前写进去的结果（那次的 74% 事故）。"""
        key = "p:kw:0"
        self.cache.put(key, [make_result("x1"), make_result("x2")], complete=True)
        # 模拟后台补齐：只拿到 x3，但如果直接覆盖，x1/x2 就没了
        self.cache.merge_put(key, [make_result("x3")], complete=True)
        entry = self.cache.get(key)
        assert entry is not None
        ids = {r.unique_id for r in entry.results}
        self.assertEqual(ids, {"x1", "x2", "x3"}, "覆盖写会丢掉并发写入的结果")

    def test_merge_takes_more_complete(self):
        """同 key 撞上取完整度高的，不是保留先到的。"""
        thin = SearchResult(unique_id="same", title="标题")
        fat = SearchResult(
            unique_id="same",
            title="标题长一些",
            content="有正文",
            links=[Link(type="quark", url="u1")],
            tags=["t"],
        )
        merged = merge_results([thin], [fat])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].content, "有正文")


class TestFailureHandling(unittest.TestCase):
    """失败分类 + 回退缓存。"""

    def setUp(self) -> None:
        self.cache = SearchCache()
        self.pool = WorkerPool()

    def test_status_classification(self):
        self.assertIs(classify_failure(status=429), FailureClass.RATE_LIMITED)
        self.assertIs(classify_failure(status=403), FailureClass.FORBIDDEN)
        self.assertIs(classify_failure(status=404), FailureClass.NOT_FOUND)
        self.assertIs(classify_failure(status=504), FailureClass.SERVER_ERROR)
        self.assertIs(classify_failure(asyncio.TimeoutError()), FailureClass.TIMEOUT)
        self.assertIsNone(classify_failure())

    def test_404_not_retryable(self):
        """接口已删/永久封禁不该重试，重试只是给死源加负载。"""
        self.assertFalse(FailureClass.NOT_FOUND.retryable)
        self.assertFalse(FailureClass.FORBIDDEN.retryable)
        self.assertTrue(FailureClass.TIMEOUT.retryable)
        self.assertTrue(FailureClass.SERVER_ERROR.retryable)

    def test_failure_falls_back_to_cache(self):
        """抓取失败时回退旧缓存，源不会因为一次失败就从结果里消失。"""
        class Fail(Fake):
            name = "failfb"

        p = build(Fail, cache=self.cache, pool=self.pool, timeout=1.0)
        p.payload = [make_result("old")]
        asyncio.run(p.search("词"))

        # 第二次让它报错
        p.error = HttpStatusError(503, p.name)
        out = asyncio.run(p.search("词", {"refresh": True}))  # refresh 跳过缓存
        self.assertIs(out.failure, FailureClass.SERVER_ERROR)
        self.assertEqual(len(out.results), 1, "失败时必须回退旧缓存")
        self.assertEqual(out.results[0].unique_id, "old")


class TestLiveness(unittest.TestCase):
    """存活分级。zero_yield 是独立状态 —— 不报错不等于健康。"""

    def test_zero_yield_vs_failing(self):
        reg = type(liveness)()          # 独立的实例，不污染全局
        for _ in range(6):
            reg.observe_plugin("quiet", 0, None)
        for _ in range(6):
            reg.observe_plugin("broken", 0, RuntimeError("boom"))

        snap = reg.snapshot()["plugins"]
        self.assertEqual(snap["quiet"]["status"], Liveness.ZERO_YIELD.value)
        self.assertEqual(snap["broken"]["status"], Liveness.FAILING.value)

    def test_insufficient_data_not_judged(self):
        reg = type(liveness)()
        reg.observe_plugin("new", 0, None)
        snap = reg.snapshot()["plugins"]
        self.assertNotIn("new", snap, "样本不足时不该出现在告警名单里")

    def test_recovery_clears_bad_status(self):
        """滑动窗口：早期失败过，恢复后要能被冲刷掉，不是永久标坏。"""
        reg = type(liveness)()
        for _ in range(6):
            reg.observe_plugin("x", 0, RuntimeError("old"))
        self.assertIn("x", reg.snapshot()["plugins"])
        for _ in range(20):
            reg.observe_plugin("x", 3, None)
        self.assertNotIn("x", reg.snapshot()["plugins"], "恢复正常后应从告警名单消失")


class TestWorkerPool(unittest.TestCase):
    """工作池：拿不到槽必须返回 False，不能排队。"""

    def test_reject_when_full(self):
        pool = WorkerPool(max_workers=2, max_tasks=10)
        self.assertTrue(pool.try_acquire())
        self.assertTrue(pool.try_acquire())
        self.assertFalse(pool.try_acquire(), "满了就该拒绝，由调用方降级")
        self.assertEqual(pool.rejected, 1)
        pool.release()
        self.assertTrue(pool.try_acquire())

    def test_max_tasks_cap(self):
        pool = WorkerPool(max_workers=100, max_tasks=2)
        self.assertTrue(pool.try_acquire())
        self.assertTrue(pool.try_acquire())
        self.assertFalse(pool.try_acquire())


class TestBatchOutcome(unittest.TestCase):
    """批次判定：这三条是最容易"顺手优化掉"、然后出事的地方。"""

    def test_all_failed_no_cache(self):
        from app.search.outcome import BatchOutcome

        o = BatchOutcome()
        o.observe("a", 0, HttpStatusError(503))
        o.observe("b", 0, HttpStatusError(429))
        o.finalize(["a", "b"])
        ok, reason = o.cache_decision(require_yield=True)
        self.assertFalse(ok, "全部失败不该写缓存")
        self.assertIn("失败", reason)

    def test_zero_yield_no_cache(self):
        """全部成功但零产出 —— 源改版后的典型形态，写进缓存就钉死一小时。"""
        from app.search.outcome import BatchOutcome

        o = BatchOutcome()
        o.observe("a", 0, None)
        o.observe("b", 0, None)
        o.finalize(["a", "b"])

        self.assertTrue(o.complete(), "没有超时没有失败 —— 单看 complete() 会认为是完整的")
        ok, reason = o.cache_decision(require_yield=True)
        self.assertFalse(ok, "零产出必须拒绝写入，即使 complete() 说它完整")
        self.assertIn("零产出", reason)

    def test_normal_writes(self):
        from app.search.outcome import BatchOutcome

        o = BatchOutcome()
        o.observe("a", 3, None)
        o.observe("b", 0, None)
        o.finalize(["a", "b"])
        ok, _ = o.cache_decision(require_yield=True)
        self.assertTrue(ok, "有产出的正常批次要写缓存")

    def test_backfill_ratio_gate(self):
        """超时超过 1/3 不补齐：上游整体变慢时补齐会把请求量再放大一倍。"""
        from app.search.outcome import BatchOutcome

        # 1/3 以内 → 补
        o = BatchOutcome()
        o.observe("a", 2, None)
        o.observe("b", 2, None)
        o.observe("c", 0, None, timed_out=True)
        o.finalize(["a", "b", "c"])
        self.assertTrue(o.should_backfill(True))

        # 超过 1/3 → 不补
        o2 = BatchOutcome()
        o2.observe("a", 2, None)
        o2.observe("b", 0, None, timed_out=True)
        o2.observe("c", 0, None, timed_out=True)
        o2.finalize(["a", "b", "c"])
        self.assertFalse(o2.should_backfill(True), "过半数超时时不该再追加请求")

    def test_backfill_requires_enabled_and_success(self):
        from app.search.outcome import BatchOutcome

        o = BatchOutcome()
        o.observe("a", 1, None)
        o.observe("b", 0, None, timed_out=True)
        o.finalize(["a", "b"])
        self.assertFalse(o.should_backfill(False), "开关关掉就不能补")


class TestRegistry(unittest.TestCase):
    """注册机制：默认全关是安全底线，不能改。"""

    def test_parse_enabled_empty_means_all_off(self):
        from app.search.registry import parse_enabled

        self.assertEqual(parse_enabled(""), [], "空配置 = 一个都不启用，这是安全的默认值")
        self.assertEqual(parse_enabled(None), [])
        self.assertEqual(parse_enabled("a, b , "), ["a", "b"])
        self.assertEqual(parse_enabled(["a", "", " b "]), ["a", "b"])

    def test_load_all_with_empty_whitelist_loads_nothing(self):
        from app.search import registry

        self.assertEqual(registry.load_all(""), [], "没显式批准就不该有任何源上线")

    def test_register_rejects_duplicate_name(self):
        from app.search.registry import register

        class A(BasePlugin):
            name = "dup_check"

        class B(BasePlugin):
            name = "dup_check"

        register(A)
        with self.assertRaises(ValueError):
            register(B)

    def test_template_importable_and_registers(self):
        """模板文件必须能被正常 import —— 否则新人复制过去先用不了。"""
        from app.search.plugins import _template  # noqa: F401
        from app.search.registry import registered_names

        self.assertIn("template", registered_names())


if __name__ == "__main__":
    unittest.main(verbosity=2)
