"""关键词标题过滤 / 来源标记 / 来源贡献分的离线测试。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_keyword_and_score -v``
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import source_score
from app.search import adapter, engine
from app.search.types import Link, PluginOutcome, SearchResult


def result(uid: str, title: str, *, channel: str = "", platform: str = "quark") -> SearchResult:
    return SearchResult(
        unique_id=uid,
        channel=channel,
        title=title,
        links=[Link(type=platform, url=f"https://pan.example/{uid}", password="")],
        extras={"platform": platform, "shareUrl": f"https://pan.example/{uid}"},
    )


def run(coro):
    return asyncio.run(coro)


class FakePlugin:
    def __init__(self, name: str, results: list[SearchResult], kind: str = "plugin"):
        self.name = name
        self.source_kind = kind
        self._results = results
        self.seen_ext: dict | None = None

    async def search(self, keyword: str, ext: dict) -> PluginOutcome:
        self.seen_ext = ext
        return PluginOutcome(source=self.name, results=self._results, is_final=True)


class TestKeywordFilter(unittest.TestCase):
    def test_single_token_case_insensitive(self):
        tokens = engine.keyword_tokens("S事 in 全集")
        self.assertTrue(engine.title_matches_keyword("《s事 IN》全集", tokens))

    def test_all_tokens_required(self):
        tokens = engine.keyword_tokens("三体 全集")
        self.assertTrue(engine.title_matches_keyword("三体电视剧 全集 4K", tokens))
        self.assertFalse(engine.title_matches_keyword("三体电视剧 第一部", tokens))

    def test_whitespace_and_punctuation_split(self):
        tokens = engine.keyword_tokens("三体，全集/4k")
        self.assertEqual(tokens, ["三体", "全集", "4k"])

    def test_empty_query_keeps_everything(self):
        kept, dropped = engine.filter_by_keyword([result("a", "任意标题")], [])
        self.assertEqual((len(kept), dropped), (1, 0))

    def test_filter_returns_kept_and_count(self):
        rows = [result("a", "三体 全集"), result("b", "无关资源"), result("c", "三体 剧集")]
        kept, dropped = engine.filter_by_keyword(rows, ["三体"])
        self.assertEqual([r.unique_id for r in kept], ["a", "c"])
        self.assertEqual(dropped, 1)

    def test_subsequence_allowed_when_order_preserved(self):
        """字符不连续但顺序对 → 放行。这是「走马上任」被过滤光后的修正目标。"""
        tokens = engine.keyword_tokens("走马上任")
        self.assertTrue(engine.title_matches_keyword("走马（2026）上任", tokens))
        self.assertTrue(engine.title_matches_keyword("走 马 上 任", tokens))
        self.assertTrue(engine.title_matches_keyword("走马上任", tokens))

    def test_order_reversed_is_rejected(self):
        """同样这些字，顺序反了或乱了 → 必须拒绝。"""
        tokens = engine.keyword_tokens("走马上任")
        self.assertFalse(engine.title_matches_keyword("上任走马", tokens))
        self.assertFalse(engine.title_matches_keyword("走任马上", tokens))
        self.assertFalse(engine.title_matches_keyword("马走任上", tokens))

    def test_missing_character_is_rejected(self):
        tokens = engine.keyword_tokens("走马上任")
        self.assertFalse(engine.title_matches_keyword("走马上", tokens))
        self.assertFalse(engine.title_matches_keyword("无关标题", tokens))

    def test_multiple_tokens_keep_global_order(self):
        """多个词的字符拼成一条序列，顺序同样必须一致。"""
        tokens = engine.keyword_tokens("三体 全集")
        self.assertTrue(engine.title_matches_keyword("三体电视剧 全集 4K", tokens))
        self.assertFalse(engine.title_matches_keyword("全集 三体", tokens))


class TestSourceMark(unittest.TestCase):
    def test_tg_item_carries_channel_source(self):
        item = adapter.to_item(result("tg-ch1-1", "三体", channel="ch1"))
        self.assertEqual(item["sourceKey"], "tg:ch1")
        self.assertEqual(item["sourceKind"], "tg")
        self.assertEqual(item["sourceName"], "ch1")

    def test_plugin_item_carries_plugin_source(self):
        item = adapter.to_item(result("haisou-1", "三体"))
        self.assertEqual(item["sourceKey"], "plugin:haisou")
        self.assertEqual(item["sourceKind"], "plugin")
        self.assertEqual(item["sourceName"], "haisou")


class EngineKeywordBase(unittest.TestCase):
    def search(self, plugins, query="三体", platforms=None, source="plugin"):
        with patch.object(engine.registry, "loaded", return_value=plugins):
            return run(engine.search(
                query, list(platforms or []), 1, 20, "title", source=source,
            ))


class TestEngineKeyword(EngineKeywordBase):
    """关键词过滤在各档内部生效；过滤空了就是空，不再跨档兜底。"""

    def test_title_filter_keeps_matching_drops_noise(self):
        hs = FakePlugin("haisou", [
            result("haisou-1", "三体 全集"),
            result("haisou-2", "完全无关的转发"),
        ])
        data = self.search([hs], source="plugin")

        self.assertEqual([i["title"] for i in data["items"]], ["三体 全集"])
        self.assertEqual(data["_meta"]["filtered"], 1)
        self.assertEqual(data["_meta"]["tier"], "plugin")

    def test_tg_title_matching_carries_channel_source(self):
        tg = FakePlugin("tg_searchers", [result("tg-x", "三体 全集", channel="ch1")], kind="tg")
        data = self.search([tg], source="tg")

        self.assertEqual(data["_meta"]["tier"], "tg")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["sourceKey"], "tg:ch1")

    def test_all_titles_filtered_returns_empty_with_diagnostic(self):
        """标题全不含关键词 = 正常零结果，不抛 504，也不去另一档捞。"""
        hs = FakePlugin("haisou", [
            result("haisou-1", "无关"),
            result("haisou-2", "也不相关"),
        ])
        data = self.search([hs], source="plugin")

        self.assertEqual(data["items"], [])
        self.assertEqual(data["_meta"]["filtered"], 2)
        self.assertEqual(data["_meta"]["tier"], "plugin")

    def test_filter_then_platform_filter(self):
        """标题匹配但平台不符 → 出口被平台过滤掉，不跨档兜底。"""
        tg = FakePlugin(
            "tg_searchers", [result("tg-x", "三体 百度", platform="baidu")], kind="tg",
        )
        data = self.search([tg], platforms=["quark"], source="tg")

        self.assertEqual(data["items"], [])
        self.assertEqual(data["_meta"]["tier"], "tg")


class TestSourceScore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "score.db")
        source_score.init(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rank_counts_only_recorded_clicks(self):
        source_score.record("tg:ch1", ip="1.2.3.4")
        source_score.record("tg:ch1", ip="1.2.3.4")
        source_score.record("plugin:haisou")
        rank = source_score.rank()

        self.assertEqual(rank["total"], 3)
        rows = {r["key"]: r for r in rank["rows"]}
        self.assertEqual(rows["tg:ch1"]["score"], 2)
        self.assertEqual(rows["plugin:haisou"]["score"], 1)
        self.assertGreater(rank["since"], 0)

    def test_bad_key_and_bot_are_ignored(self):
        self.assertFalse(source_score.record("tg:"))
        self.assertFalse(source_score.record("random:key"))
        self.assertFalse(source_score.record("tg:ch1", bot=True))
        self.assertEqual(source_score.rank()["total"], 0)

    def test_reinit_keeps_original_since(self):
        first = source_score.rank()["since"]
        source_score.init(self.db)
        self.assertEqual(source_score.rank()["since"], first)

    def test_legacy_clicks_are_not_counted(self):
        """启用前写进 events 的历史点击，永远进不了这张新表。"""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO source_scores(ts, day, source_key, source_kind, source_name) "
            "VALUES (?,?,?,?,?)",
            (1.0, "2020-01-01", "tg:old", "tg", "old"),
        )
        conn.commit()
        conn.close()
        self.assertEqual(source_score.rank()["total"], 0)


if __name__ == "__main__":
    unittest.main()
