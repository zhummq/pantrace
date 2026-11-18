"""同键并发去重（`app/search/singleflight.py`）的离线测试。

这层只有一个职责：**同一缓存键同时未命中时，只放一个请求到上游**。
但它一旦写错，代价直接是钱 —— haisou 一次搜索是「平台数 × 2 页」个计费请求，
并发 N 个就是 2N 份额度。所以四件事都要钉住：

1. 同键并发只有一个 leader，其余搭车拿到同一个结果；
2. 不同键互不影响（别把锁写成全局串行，那会把并发搜索重新串起来）；
3. leader 失败要广播 —— follower 各自重试等于没加锁，失败还会放大成 N 倍计费；
4. follower 等到超时要能自己降级跑，否则 leader 卡住就带着所有人一起挂。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_singleflight -v``
"""

from __future__ import annotations

import asyncio
import unittest

from app.search.singleflight import SingleFlight


class SingleFlightTest(unittest.TestCase):
    def test_same_key_runs_factory_once(self):
        """5 个并发同键请求，factory 只执行一次，其余 4 个搭车。"""
        calls: list[int] = []

        async def scenario():
            sf = SingleFlight()

            async def factory():
                calls.append(1)
                await asyncio.sleep(0.05)
                return {"n": 1}

            return await asyncio.gather(*(sf.run("k", factory) for _ in range(5)))

        results = asyncio.run(scenario())

        self.assertEqual(len(calls), 1, "同键并发必须只打一次上游")
        self.assertEqual([shared for _, shared in results], [False, True, True, True, True])
        # 搭车的人拿到的必须是同一份数据，不能是空壳
        self.assertTrue(all(body["n"] == 1 for body, _ in results))

    def test_different_keys_run_separately(self):
        """不同键各跑各的 —— 锁是按 key 的，不是全局串行。"""
        calls: list[str] = []

        async def scenario():
            sf = SingleFlight()

            async def factory(tag: str):
                calls.append(tag)
                await asyncio.sleep(0.02)
                return tag

            return await asyncio.gather(
                sf.run("a", lambda: factory("a")),
                sf.run("b", lambda: factory("b")),
            )

        results = asyncio.run(scenario())

        self.assertEqual(sorted(calls), ["a", "b"])
        self.assertTrue(all(not shared for _, shared in results))
        self.assertEqual(sorted(body for body, _ in results), ["a", "b"])

    def test_failure_is_broadcast(self):
        """leader 抛异常时 follower 也必须失败，不能偷偷各自重试。"""

        async def scenario():
            sf = SingleFlight()

            async def factory():
                await asyncio.sleep(0.02)
                raise ValueError("upstream boom")

            return await asyncio.gather(
                *(sf.run("k", factory) for _ in range(3)), return_exceptions=True
            )

        results = asyncio.run(scenario())

        self.assertTrue(
            all(isinstance(item, ValueError) for item in results),
            f"三个请求都该收到同一个异常，实际：{results}",
        )

    def test_follower_timeout_falls_back_to_own_call(self):
        """follower 等超时后自己跑一遍，不跟着 leader 一起卡死。"""
        calls: list[int] = []

        async def scenario():
            sf = SingleFlight(wait_timeout=0.05)

            async def factory():
                calls.append(1)
                await asyncio.sleep(0.15)
                return len(calls)

            leader = asyncio.ensure_future(sf.run("k", factory))
            # 让 leader 先登记进 _inflight，否则下面那次会被当成 leader
            await asyncio.sleep(0.02)
            got = await sf.run("k", factory)
            await leader
            return got

        got, shared = asyncio.run(scenario())

        self.assertEqual(len(calls), 2, "leader 一次 + 超时降级一次")
        self.assertFalse(shared, "降级执行不是搭车，shared 必须是 False")
        self.assertEqual(got, 2)

    def test_inflight_is_cleared_after_finish(self):
        """结束后必须摘表，否则会随 key 数量无界增长。"""

        async def scenario():
            sf = SingleFlight()

            async def factory():
                return 1

            await sf.run("k", factory)
            return sf.inflight, sf.is_inflight("k")

        inflight, still_there = asyncio.run(scenario())

        self.assertEqual(inflight, 0)
        self.assertFalse(still_there)

    def test_key_reusable_after_finish(self):
        """跑完的键能再次执行 —— 不能搭上一个早已完成的 Task 拿到旧结果。"""
        calls: list[int] = []

        async def scenario():
            sf = SingleFlight()

            async def factory():
                calls.append(1)
                return len(calls)

            first, _ = await sf.run("k", factory)
            second, shared = await sf.run("k", factory)
            return first, second, shared

        first, second, shared = asyncio.run(scenario())

        self.assertEqual((first, second), (1, 2), "第二次必须是新结果，不是上次的缓存")
        self.assertEqual(len(calls), 2)
        self.assertFalse(shared)


if __name__ == "__main__":
    unittest.main()
