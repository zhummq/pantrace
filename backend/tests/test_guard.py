"""限流配额的"加权"消费（`app/guard.py` 的 `cost`）。

普通搜索占 1 份配额，`refresh=1` 占 3 份 —— 因为它绕过缓存、直接回源，
每一次都真实消耗上游额度。不加权的话，刷新就成了"免费加速器"：
30 次/分钟全拿去刷新，等于 60 个计费请求。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_guard -v``
"""

from __future__ import annotations

import unittest

from app import guard


class GuardCostTest(unittest.TestCase):
    def setUp(self):
        # 每个用例重置计数：guard 是模块级单例，不清会互相污染
        guard.init("test-secret", 300, rate_limit=3, window=60)

    def test_default_cost_is_one(self):
        for i in range(3):
            ok, wait = guard.allow("ip")
            self.assertTrue(ok, f"第 {i + 1} 次应该放行")
            self.assertEqual(wait, 0)

        ok, wait = guard.allow("ip")
        self.assertFalse(ok, "超出 3 次必须拒绝")
        self.assertGreaterEqual(wait, 1)

    def test_refresh_eats_three_quota(self):
        ok, _ = guard.allow("ip", cost=3)
        self.assertTrue(ok, "额度刚好用完时应放行这一次")

        ok, _ = guard.allow("ip")
        self.assertFalse(ok, "一次刷新 = 3 份，普通搜索不该再有位置")

    def test_cost_larger_than_limit_does_not_crash(self):
        """cost > limit 是配置写错的情形，要拒绝但绝不能抛 IndexError。"""
        guard.init("test-secret", 300, rate_limit=2, window=60)

        ok, wait = guard.allow("ip", cost=3)

        self.assertFalse(ok)
        self.assertGreaterEqual(wait, 1)

    def test_cost_is_per_ip(self):
        """加权只能影响发起刷新的那个 IP，不能把别人一起限掉。"""
        guard.allow("a", cost=3)

        self.assertTrue(guard.allow("b")[0])


if __name__ == "__main__":
    unittest.main()
