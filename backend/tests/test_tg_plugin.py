"""TG 源插件（`plugins/tg_searchers.py`）的离线测试。

这层是 pansou `searchTG` 通道接进插件体系的适配层，所以要盯三件事：

1. **缓存键必须与 `search_tg` 内部一致** —— 不一致的话后台补齐写进 `tg:` 键的结果
   外层读不到，补齐等于白干，而且不报错、只表现为"补齐没生效"。
2. **门关必须落成 NETWORK 失败**，不能是空结果 —— 空结果会被 `Liveness` 记成
   `zero_yield`，把"根本没连上"误判成"这个源改版了"。
3. **频道列表来源优先级**：ext 里给了就用给的，否则读配置。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_tg_plugin -v``
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.search import registry
from app.search.observe import FailureClass, classify_failure
from app.search.plugins import tg_searchers
from app.search.tg import tg_cache_key


def run(coro):
    return asyncio.run(coro)


class FakeSettings:
    tg_channel_list = ["tgsearchers7"]


class CacheKeyAlignmentTest(unittest.TestCase):
    """缓存键对齐，这是本文件最重要的一条。"""

    def setUp(self):
        self.plugin = tg_searchers.TGSeachersPlugin()

    def test_matches_search_tg_key(self):
        """外层插件的键必须等于 `search_tg` 内部用的键。

        改成 `super().cache_key()`（带 ext_digest）就会挂：那时补齐写 `tg:` 键，
        外层读 `tg_searchers:` 键，永远读不到补齐结果。
        """
        self.assertEqual(
            self.plugin.cache_key("三体", {"channels": ["a", "b"]}),
            tg_cache_key("三体", ["a", "b"]),
        )

    def test_channels_do_not_leak_stray_whitespace(self):
        self.assertEqual(
            self.plugin.cache_key("三体", {"channels": [" a ", "@b"]}),
            tg_cache_key("三体", ["a", "b"]),
        )

    def test_default_channels_come_from_settings(self):
        with patch.object(tg_searchers, "get_settings", return_value=FakeSettings()):
            self.assertEqual(
                self.plugin.cache_key("三体", {}),
                tg_cache_key("三体", ["tgsearchers7"]),
            )


class ChannelsResolutionTest(unittest.TestCase):
    def setUp(self):
        self.plugin = tg_searchers.TGSeachersPlugin()

    def test_ext_list_wins(self):
        self.assertEqual(tg_searchers._channels(["x", "@y"]), ["x", "y"])

    def test_ext_string_is_split(self):
        self.assertEqual(tg_searchers._channels("x, @y ,"), ["x", "y"])

    def test_empty_falls_back_to_settings(self):
        with patch.object(tg_searchers, "get_settings", return_value=FakeSettings()):
            self.assertEqual(tg_searchers._channels(None), ["tgsearchers7"])
            self.assertEqual(tg_searchers._channels([]), ["tgsearchers7"])

    def test_no_channels_anywhere_raises(self):
        """没配频道 = 配置缺失，不是"这个源没内容"。"""
        with patch.object(tg_searchers, "get_settings", return_value=type("S", (), {"tg_channel_list": []})()):
            with self.assertRaises(ValueError):
                run(self.plugin.fetch(None, "三体", {}))


class GateClosedTest(unittest.TestCase):
    def setUp(self):
        self.plugin = tg_searchers.TGSeachersPlugin()

    def test_gate_closed_becomes_network_failure(self):
        """门关 → 抛 TGUnreachable，且失败分类是 NETWORK 而不是 OTHER/ZERO_YIELD。"""
        with patch.object(tg_searchers, "get_settings", return_value=FakeSettings()), \
             patch.object(tg_searchers.TG, "is_reachable", return_value=False), \
             patch.object(tg_searchers.TG, "reason", "连续 2 次探测失败: ConnectTimeout"):
            with self.assertRaises(tg_searchers.TGUnreachable) as ctx:
                run(self.plugin.fetch(None, "三体", {}))
        self.assertEqual(classify_failure(ctx.exception), FailureClass.NETWORK)

    def test_gate_open_with_no_results_is_not_a_failure(self):
        """门开着、频道里确实没这条资源 → 返回 []，不能抛错，否则会被记成源故障。"""
        async def fake_search_tg(*args, **kwargs):
            return []

        with patch.object(tg_searchers, "get_settings", return_value=FakeSettings()), \
             patch.object(tg_searchers.TG, "is_reachable", return_value=True), \
             patch.object(tg_searchers, "search_tg", fake_search_tg):
            self.assertEqual(run(self.plugin.fetch(None, "三体", {})), [])


class DelegationTest(unittest.TestCase):
    """这层不许自己实现判定，必须原样转交给 search_tg。"""

    def setUp(self):
        self.plugin = tg_searchers.TGSeachersPlugin()

    def test_forwards_keyword_channels_cache_and_client(self):
        seen = {}

        async def fake_search_tg(keyword, channels, **kwargs):
            seen.update(keyword=keyword, channels=channels, **kwargs)
            return []

        client = object()
        with patch.object(tg_searchers, "get_settings", return_value=FakeSettings()), \
             patch.object(tg_searchers.TG, "is_reachable", return_value=True), \
             patch.object(tg_searchers, "search_tg", fake_search_tg):
            run(self.plugin.fetch(client, "流浪地球", {"channels": ["a"]}))

        self.assertEqual(seen["keyword"], "流浪地球")
        self.assertEqual(seen["channels"], ["a"])
        self.assertIs(seen["client"], client)
        # 必须把插件缓存传进去，否则后台补齐无处可写
        self.assertIs(seen["cache"], self.plugin.cache)

    def test_force_refresh_is_forwarded(self):
        seen = {}

        async def fake_search_tg(keyword, channels, **kwargs):
            seen.update(kwargs)
            return []

        with patch.object(tg_searchers, "get_settings", return_value=FakeSettings()), \
             patch.object(tg_searchers.TG, "is_reachable", return_value=True), \
             patch.object(tg_searchers, "search_tg", fake_search_tg):
            run(self.plugin.fetch(None, "三体", {"refresh": True}))

        self.assertTrue(seen["force_refresh"])


class RegistrationTest(unittest.TestCase):
    def test_plugin_is_discoverable_by_name(self):
        registry._discover()
        self.assertIn("tg_searchers", registry.registered_names())
        self.assertIs(registry.get_registered_class("tg_searchers"), tg_searchers.TGSeachersPlugin)

    def test_level_is_primary_source(self):
        """频道里是转存后公开的分享，属一手来源，和 haisou 同级。"""
        self.assertEqual(tg_searchers.TGSeachersPlugin.level, 1)


if __name__ == "__main__":
    unittest.main()
