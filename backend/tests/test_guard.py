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


class AppChannelTest(unittest.TestCase):
    """小程序通道（X-App-Token）：令牌代替票据，限流换成"按客户端"。

    这条通道存在的唯一原因：云函数出口 IP 不固定，票据的 IP 绑定在它上面
    是必现的 403（领票一个出口、搜索另一个）。所以这里盯死两件事：

      1. 没配令牌时绝不能变成"随便过"——空值当通过，等于接口裸奔；
      2. 免的是票，不是配额——令牌泄露时要能被总闸拦住。
    """

    def setUp(self):
        guard.init(
            "test-secret",
            300,
            rate_limit=3,
            window=60,
            app_token="ptk_test_token",
            app_rate_limit=2,
            app_global_limit=5,
        )

    def test_valid_token_passes(self):
        self.assertTrue(guard.app_token_ok("ptk_test_token"))

    def test_wrong_token_rejected(self):
        self.assertFalse(guard.app_token_ok("ptk_other"))
        self.assertFalse(guard.app_token_ok("ptk_test_tokenx"))

    def test_token_surrounding_space_tolerated(self):
        """头值带换行/空格是常见的手工配置事故，不该因此全站 403。"""
        self.assertTrue(guard.app_token_ok("  ptk_test_token\n"))
        self.assertTrue(guard.app_token_ok("ptk_test_token "))

    def test_unconfigured_channel_never_passes(self):
        """最要命的一条：空 APP_TOKEN 不能把空令牌放进来。"""
        guard.init("test-secret", 300, rate_limit=3, window=60, app_token="")

        self.assertFalse(guard.app_token_ok(""))
        self.assertFalse(guard.app_token_ok("   "))
        self.assertFalse(guard.app_token_ok("ptk_test_token"), "配置清了就该全部拒绝")

    def test_quota_is_per_client_not_global(self):
        """一个人刷爆自己的额度，不能连坐别人——这是按 IP 限流的替代品。"""
        guard.allow_client("hash-a")
        guard.allow_client("hash-a")

        ok, _ = guard.allow_client("hash-a")
        self.assertFalse(ok, "第 3 次超出单客户端 2 次/分钟")
        self.assertTrue(guard.allow_client("hash-b")[0], "另一个用户不该受影响")

    def test_global_cap_stops_hash_rotation(self):
        """令牌泄露后换着 client_hash 刷：单客户端配额绕得过去，总闸绕不过去。"""
        for i in range(5):
            self.assertTrue(guard.allow_global()[0], f"第 {i + 1} 次应在总闸内")

        ok, wait = guard.allow_global()
        self.assertFalse(ok)
        self.assertGreaterEqual(wait, 1)

    def test_app_quota_does_not_consume_ip_quota(self):
        """两套桶必须分开：否则小程序几亿用户共用云函数出口 IP，会互相挤爆。"""
        for _ in range(3):
            guard.allow("cloud-func-egress")

        self.assertFalse(guard.allow("cloud-func-egress")[0], "IP 桶已满")
        self.assertTrue(guard.allow_client("hash-a")[0], "客户端桶应当是独立的")


if __name__ == "__main__":
    unittest.main()
