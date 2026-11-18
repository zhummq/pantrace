"""adapter + engine 的离线测试。

这两块是新搜索链路的出口：插件产出 SearchResult，adapter 转成前端要的 items，
engine 负责多源合并、本地分页、失败判定。测的是"结果对但形状错"的那类问题——
字段少一个、分页差一页、全挂却返回空列表，线上都表现为"能用但没数据"。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_search_engine -v``
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.search import adapter, engine
from app.search.types import FailureClass, Link, PluginOutcome, SearchResult


def result(
    uid: str,
    url: str = "https://pan.quark.cn/s/C1",
    title: str = "三体 全集",
    platform: str = "quark",
    pwd: str = "abcd",
    extras: dict | None = None,
) -> SearchResult:
    if extras is None:
        extras = {
            "hsid": f"h-{uid}",
            "platform": platform,
            "platformName": "夸克网盘",
            "shareCode": "C1",
            "shareUrl": url,
            "sharePwd": pwd,
            "fileCount": 3,
            "sizeBytes": 1024**3,
        }
    return SearchResult(
        unique_id=uid,
        title=title,
        content="文件数量: 3 | 网盘类型: 夸克网盘 | 大小: 1.00 GB",
        links=[Link(type="quark", url=url, password=pwd)],
        datetime=1_700_000_000,
        extras=extras,
    )


def run(coro):
    return asyncio.run(coro)


# ---------------- adapter ----------------


class TestAdapter(unittest.TestCase):
    def test_maps_extras_to_item(self):
        item = adapter.to_item(result("haisou-1"))

        self.assertEqual(item["hsid"], "h-haisou-1")
        self.assertEqual(item["platform"], "quark")
        self.assertEqual(item["platformName"], "夸克网盘")
        self.assertEqual(item["title"], "三体 全集")
        self.assertEqual(item["shareUrl"], "https://pan.quark.cn/s/C1")
        self.assertEqual(item["sharePwd"], "abcd")
        self.assertEqual(item["fileCount"], 3)
        self.assertEqual(item["sizeBytes"], 1024**3)
        self.assertEqual(item["shareCode"], "C1")

    def test_tg_result_without_extras_falls_back_to_link(self):
        """TG/网页源没有 extras，平台要从 Link.type 反推（aliyun → ali）。"""
        raw = SearchResult(
            unique_id="tg-ch1-1",
            channel="ch1",
            title="资源",
            links=[Link(type="aliyun", url="https://www.alipan.com/s/X", password="")],
        )
        item = adapter.to_item(raw)

        self.assertEqual(item["platform"], "ali")
        self.assertEqual(item["platformName"], "阿里云盘")
        self.assertEqual(item["shareUrl"], "https://www.alipan.com/s/X")
        self.assertEqual(item["hsid"], "")

    def test_result_without_link_is_dropped(self):
        """点不开的条目不该占位置，也不该在分页里算一条。"""
        raw = SearchResult(unique_id="haisou-x", title="没有链接")
        self.assertIsNone(adapter.to_item(raw))

    def test_to_items_dedups_by_url(self):
        rows = [
            result("haisou-1", url="https://pan.quark.cn/s/SAME"),
            result("haisou-2", url="https://pan.quark.cn/s/SAME"),
            result("haisou-3", url="https://pan.quark.cn/s/OTHER"),
        ]
        items = adapter.to_items(rows)
        self.assertEqual([i["shareUrl"] for i in items], [
            "https://pan.quark.cn/s/SAME",
            "https://pan.quark.cn/s/OTHER",
        ])


class TestPaginate(unittest.TestCase):
    def setUp(self):
        self.items = [{"n": i} for i in range(45)]

    def test_first_page(self):
        page, pg = adapter.paginate(self.items, 1, 20)
        self.assertEqual(len(page), 20)
        self.assertEqual(page[0]["n"], 0)
        self.assertEqual(pg, {"page": 1, "pageSize": 20, "total": 45, "totalPages": 3})

    def test_last_partial_page(self):
        page, pg = adapter.paginate(self.items, 3, 20)
        self.assertEqual(len(page), 5)
        self.assertEqual(page[0]["n"], 40)
        self.assertEqual(pg["totalPages"], 3)

    def test_page_past_end_is_empty_not_crash(self):
        page, pg = adapter.paginate(self.items, 9, 20)
        self.assertEqual(page, [])
        self.assertEqual(pg["total"], 45)

    def test_empty_result(self):
        page, pg = adapter.paginate([], 1, 20)
        self.assertEqual(page, [])
        self.assertEqual(pg["total"], 0)
        self.assertEqual(pg["totalPages"], 0)

    def test_bad_page_values_are_clamped(self):
        """page=0 从第 1 页算；page_size=0 回退默认 20（不是切出 0 条）。"""
        page, pg = adapter.paginate(self.items, 0, 0)
        self.assertEqual(pg["page"], 1)
        self.assertEqual(pg["pageSize"], 20)
        self.assertEqual(len(page), 20)


# ---------------- engine ----------------


class FakePlugin:
    def __init__(self, name: str, results: list[SearchResult] | None = None,
                 failure: FailureClass | None = None, pending: bool = False,
                 kind: str = "plugin"):
        self.name = name
        # 引擎按这个字段分层：tg 是基础通道，其余是兜底。
        self.source_kind = kind
        self._results = results or []
        self._failure = failure
        self._pending = pending
        self.seen_ext: dict | None = None

    async def search(self, keyword: str, ext: dict) -> PluginOutcome:
        self.seen_ext = ext
        return PluginOutcome(
            source=self.name,
            results=self._results,
            is_final=not self._pending,
            failure=self._failure,
            message="boom" if self._failure else "",
        )


class EngineBase(unittest.TestCase):
    def search(self, plugins, platforms=None, page=1, page_size=20, source="auto",
               force_refresh=False):
        with patch.object(engine.registry, "loaded", return_value=plugins):
            return run(engine.search(
                "三体", list(platforms or []), page, page_size, "title",
                source=source, force_refresh=force_refresh,
            ))


class TestEngine(EngineBase):
    def test_merges_and_paginates(self):
        a = FakePlugin("a", [result(f"a-{i}", url=f"https://pan.quark.cn/s/A{i}") for i in range(3)])
        b = FakePlugin("b", [result(f"b-{i}", url=f"https://pan.quark.cn/s/B{i}") for i in range(3)])

        data = self.search([a, b])

        self.assertEqual(data["pagination"]["total"], 6)
        self.assertEqual(len(data["items"]), 6)
        self.assertEqual(data["_meta"]["sources"], {"a": {"count": 3, "failure": None},
                                                    "b": {"count": 3, "failure": None}})

    def test_dedups_same_url_across_sources(self):
        same = "https://pan.quark.cn/s/SAME"
        a = FakePlugin("a", [result("a-1", url=same)])
        b = FakePlugin("b", [result("b-1", url=same)])

        data = self.search([a, b])

        self.assertEqual(data["pagination"]["total"], 1)
        self.assertEqual(len(data["items"]), 1)

    def test_one_source_failing_still_returns_others(self):
        ok = FakePlugin("ok", [result("ok-1", url="https://pan.quark.cn/s/OK")])
        bad = FakePlugin("bad", failure=FailureClass.SERVER_ERROR)

        data = self.search([ok, bad])

        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["_meta"]["sources"]["bad"]["failure"], "server_error")

    def test_all_failed_raises_instead_of_empty(self):
        """全挂返回空列表 = 让用户以为网上没这个资源。必须抛错走 504。"""
        bad = FakePlugin("bad", failure=FailureClass.TIMEOUT)
        with self.assertRaises(engine.SearchEngineError):
            self.search([bad])

    def test_no_sources_raises(self):
        with self.assertRaises(engine.SearchEngineError):
            self.search([])

    def test_zero_results_without_failure_is_not_an_error(self):
        """所有源都成功但都没有内容 = 正常零结果，不能报 504。"""
        empty = FakePlugin("empty", [])
        data = self.search([empty])
        self.assertEqual(data["items"], [])
        self.assertEqual(data["pagination"]["total"], 0)

    def test_platform_filter_drops_other_platforms(self):
        """用户选了百度网盘，就不能看到夸克链接。

        haisou 是"只查选中的平台"，本来不会串；但 TG 频道里的分享横跨所有网盘，
        请求里的 platforms 对它无效 —— 出口必须再筛一遍，否则筛选形同虚设。
        """
        mixed = FakePlugin("tg", [
            result("q-1", url="https://pan.quark.cn/s/Q1", platform="quark"),
            result("b-1", url="https://pan.baidu.com/s/B1", platform="baidu"),
            result("a-1", url="https://www.aliyundrive.com/s/A1", platform="ali"),
        ])

        data = self.search([mixed], platforms=["baidu"])

        self.assertEqual([i["platform"] for i in data["items"]], ["baidu"])
        self.assertEqual(data["pagination"]["total"], 1)

    def test_platform_filter_runs_before_pagination(self):
        """先筛再切页：否则第 1 页可能被别的平台占满，选中的平台一条都看不到。"""
        mixed = FakePlugin("tg", [
            result(f"q-{i}", url=f"https://pan.quark.cn/s/Q{i}", platform="quark")
            for i in range(10)
        ] + [result("b-1", url="https://pan.baidu.com/s/B1", platform="baidu")])

        data = self.search([mixed], platforms=["baidu"])

        self.assertEqual(data["pagination"]["total"], 1)
        self.assertEqual(len(data["items"]), 1)

    def test_no_platform_filter_keeps_everything(self):
        """没选平台 = 不限平台，不是"只要 platform 为空的"。"""
        mixed = FakePlugin("tg", [
            result("q-1", url="https://pan.quark.cn/s/Q1", platform="quark"),
            result("b-1", url="https://pan.baidu.com/s/B1", platform="baidu"),
        ])

        data = self.search([mixed], platforms=[])

        self.assertEqual(data["pagination"]["total"], 2)

    def test_ext_carries_scope_and_pages(self):
        plugin = FakePlugin("a", [result("a-1")])
        self.search([plugin])

        ext = plugin.seen_ext
        # 参数名照抄 pansou（ext["pages_per_type"]），插件直接读这个键。
        self.assertEqual(ext["pages_per_type"], engine.PAGES_PER_PLATFORM)
        self.assertEqual(ext["scope"], "title")
        self.assertGreaterEqual(ext["page_size"], 1)

    def test_page_slices_the_merged_set(self):
        rows = [result(f"a-{i}", url=f"https://pan.quark.cn/s/A{i}") for i in range(30)]
        plugin = FakePlugin("a", rows)

        with patch.object(engine.registry, "loaded", return_value=[plugin]):
            first = run(engine.search("三体", [], 1, 10, "title"))
            second = run(engine.search("三体", [], 2, 10, "title"))

        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(len(second["items"]), 10)
        # 两页不能有交集
        urls = {i["shareUrl"] for i in first["items"]}
        self.assertFalse(urls & {i["shareUrl"] for i in second["items"]})
        self.assertEqual(first["pagination"]["totalPages"], 3)


class TestTierFallback(EngineBase):
    """TG 是基础通道，计费接口是兜底。这组测的是"该不该花那份钱"。

    和 pansou 的差异也在这：它 all 是两条分支并行跑，我们串行降级，
    所以"TG 命中时 haisou 一次请求都没发"是一条必须成立的断言。
    """

    def tg(self, **kw):
        return FakePlugin("tg_searchers", kind="tg", **kw)

    def api(self, **kw):
        return FakePlugin("haisou", kind="plugin", **kw)

    def test_tg_hit_skips_paid_source(self):
        tg = self.tg(results=[result("tg-1")])
        api = self.api(results=[result("haisou-1")])

        data = self.search([tg, api])

        self.assertEqual(data["_meta"]["tier"], "tg")
        self.assertIsNone(api.seen_ext)          # 一次请求都没发
        self.assertEqual([i["hsid"] for i in data["items"]], ["h-tg-1"])

    def test_tg_empty_falls_back(self):
        tg = self.tg(results=[])
        api = self.api(results=[result("haisou-1")])

        data = self.search([tg, api])

        self.assertEqual(data["_meta"]["tier"], "plugin")
        self.assertIsNotNone(api.seen_ext)
        self.assertEqual(len(data["items"]), 1)

    def test_tg_failure_falls_back(self):
        tg = self.tg(failure=FailureClass.NETWORK)
        api = self.api(results=[result("haisou-1")])

        data = self.search([tg, api])

        self.assertEqual(data["_meta"]["tier"], "plugin")
        self.assertEqual(len(data["items"]), 1)

    def test_tg_pending_without_results_falls_back(self):
        """窗口到点没抓完、手上又没旧缓存：不能接着等第二个窗口。"""
        tg = self.tg(results=[], pending=True)
        api = self.api(results=[result("haisou-1")])

        data = self.search([tg, api])

        self.assertEqual(data["_meta"]["tier"], "plugin")
        self.assertEqual(len(data["items"]), 1)

    def test_tg_results_without_wanted_platform_falls_back(self):
        """TG 有结果但全是夸克、用户要百度：仍要兜底，否则用户拿到空。"""
        tg = self.tg(results=[result("tg-1", platform="quark")])
        api = self.api(results=[result("haisou-1", platform="baidu")])

        data = self.search([tg, api], platforms=["baidu"])

        self.assertEqual(data["_meta"]["tier"], "plugin")
        self.assertEqual({i["platform"] for i in data["items"]}, {"baidu"})

    def test_src_tg_never_touches_paid_source(self):
        """src=tg 是零成本通道：就算 TG 一条都没有，也不许调接口。"""
        tg = self.tg(results=[])
        api = self.api(results=[result("haisou-1")])

        data = self.search([tg, api], source="tg")

        self.assertIsNone(api.seen_ext)
        self.assertEqual(data["items"], [])
        self.assertEqual(data["_meta"]["tier"], "")

    def test_src_plugin_skips_tg(self):
        tg = self.tg(results=[result("tg-1")])
        api = self.api(results=[result("haisou-1")])

        data = self.search([tg, api], source="plugin")

        self.assertIsNone(tg.seen_ext)
        self.assertEqual(data["_meta"]["tier"], "plugin")

    def test_src_tg_without_tg_source_is_config_error(self):
        with self.assertRaises(engine.SourceNotEnabledError):
            self.search([self.api(results=[result("haisou-1")])], source="tg")

    def test_only_paid_source_means_no_tier_switch(self):
        """只启用 haisou（线上当前配置）：auto 直接走它，不报错、不多等一轮。"""
        api = self.api(results=[result("haisou-1")])

        data = self.search([api])

        self.assertEqual(data["_meta"]["tier"], "plugin")
        self.assertEqual(len(data["items"]), 1)

    def test_both_tiers_failing_raises(self):
        tg = self.tg(failure=FailureClass.NETWORK)
        api = self.api(failure=FailureClass.SERVER_ERROR)

        with self.assertRaises(engine.SearchEngineError):
            self.search([tg, api])

    def test_tg_failure_is_recorded_even_when_fallback_succeeds(self):
        """兜底成功也要留下 TG 的失败痕迹，否则查不出"主力通道已经挂了"。"""
        tg = self.tg(failure=FailureClass.NETWORK)
        api = self.api(results=[result("haisou-1")])

        data = self.search([tg, api])

        self.assertEqual(len(data["items"]), 1)
        rounds = engine.last_round()
        self.assertEqual(rounds["tg_searchers"]["failure"], "network")
        self.assertEqual(rounds["tg_searchers"]["tier"], "tg")
        self.assertEqual(rounds["haisou"]["tier"], "plugin")


class TestForceRefresh(EngineBase):
    """`refresh=1` 要落到插件的 ext 上，不能只停在 API 层。

    链路是 main（Query 参数）→ engine（force_refresh）→ `_ext()`（refresh 键）
    → BasePlugin（`bool(ext.get("refresh"))`）。断在任何一环，参数都变成"看起来
    支持、实际还在读缓存"——这种参数最坑，用户以为刷新了，其实拿的还是旧数据。
    """

    def test_flag_lands_in_ext(self):
        p = FakePlugin("a", [result("a-1", url="https://pan.quark.cn/s/A")])

        self.search([p], force_refresh=True)

        self.assertIs(p.seen_ext.get("refresh"), True)

    def test_absent_by_default(self):
        """默认不能带 refresh —— 带上就等于每次搜索都绕过缓存、每次都在烧额度。"""
        p = FakePlugin("a", [result("a-1", url="https://pan.quark.cn/s/A")])

        self.search([p])

        self.assertNotIn("refresh", p.seen_ext or {})

    def test_refresh_does_not_change_ext_digest(self):
        """`refresh` 必须被排除在缓存键摘要之外。

        进摘要 → 刷新结果写进另一个插件缓存键 → 普通查询永远读不到它，
        表现为"刷新过之后还是老数据"，而且不报错。
        """
        from app.search.base import ext_digest

        plain = {"platforms": ["quark"], "scope": "title", "page_size": 20}
        refreshing = dict(plain, refresh=True)

        self.assertEqual(ext_digest(plain), ext_digest(refreshing))


if __name__ == "__main__":
    unittest.main(verbosity=2)
