"""资源库的离线测试。

盯四个回归点，每个都对应一个"写错了很难归因"的坑：

1. **跨空格召回**：`三体 全集` 和 `三体全集` 必须互相搜得到。切 gram 时先按
   空格分段就做不到，表现是"某些词搜不到"，不像 bug 像玄学。
2. **去重键**：同一分享的五六种 URL 写法必须归一成一条。用字符串比对会变成多条。
3. **详情页不入库**：解析不出分享 ID 的链接进来是废数据，`pan.py` 转存必失败。
4. **排序**：可用性压过热度，`bad` 不进候选。

跑法：``.venv/Scripts/python.exe -m unittest tests.test_store -v``
不打网络：全部本地临时库。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import store


def _row(url: str, title: str, **kw) -> dict:
    base = {
        "url": url,
        "title": title,
        "platform": "quark",
        "pwd": "",
        "file_count": 0,
        "size_bytes": 0,
        "source_name": "test",
        "source_level": 1,
    }
    base.update(kw)
    return base


class StoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "library.db"
        store.init(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestGrams(unittest.TestCase):
    def test_two_char_word_recall(self):
        """trigram 方案在两字词上直接归零，这里必须给出单字 + 双字。"""
        g = store.grams("三体").split()
        self.assertIn("三", g)
        self.assertIn("体", g)
        self.assertIn("三体", g)

    def test_cross_space_gram(self):
        """核心回归：空格不能把双字切断，否则「三体全集」搜不到「三体 全集」。"""
        g = store.grams("三体 全集").split()
        self.assertIn("体全", g)

    def test_punctuation_stripped(self):
        self.assertEqual(store.grams("《三体》"), store.grams("三体"))

    def test_empty(self):
        self.assertEqual(store.grams("   "), "")
        self.assertEqual(store.match_expr(""), "")


class TestUpsert(StoreTestBase):
    def test_same_share_many_urls_one_row(self):
        """带 pwd、尾斜杠、大小写域名、锚点、分享文案，全部归一到一条。"""
        variants = [
            "https://pan.quark.cn/s/abc123",
            "https://pan.quark.cn/s/abc123?pwd=1234",
            "HTTPS://PAN.QUARK.CN/s/abc123/",
            "pan.quark.cn/s/abc123#/list/share",
        ]
        for i, url in enumerate(variants):
            store.upsert_many([_row(url, f"三体 第{i}版")])

        s = store.stats()
        self.assertEqual(s["total"], 1)

    def test_detail_page_rejected(self):
        """详情页/不支持的站一律不入库——它们转存必失败。"""
        result = store.upsert_many([
            _row("https://www.example.com/post/123", "某个资源介绍页"),
            _row("https://pan.quark.cn/s/okid", "正常资源"),
        ])
        self.assertEqual(result["added"], 1)
        self.assertEqual(store.stats()["total"], 1)

    def test_title_kept_alias_added(self):
        """重复入库不覆盖原标题（防越改越标题党），别的叫法进 aliases。"""
        store.upsert_many([_row("https://pan.quark.cn/s/abc123", "三体 全集")])
        store.upsert_many([_row("https://pan.quark.cn/s/abc123", "三体1-3季+小说 完整版")])

        with store._session() as conn:
            row = conn.execute("SELECT title, aliases FROM resources").fetchone()
        self.assertEqual(row["title"], "三体 全集")
        self.assertIn("三体1-3季+小说 完整版", row["aliases"])

    def test_fields_fill_from_later_visit(self):
        """后面的回填能补上提取码/文件数，但不能把已有的覆盖成空。"""
        store.upsert_many([_row("https://pan.quark.cn/s/abc123", "三体", pwd="")])
        store.upsert_many([_row("https://pan.quark.cn/s/abc123", "三体", pwd="1234", file_count=9)])
        store.upsert_many([_row("https://pan.quark.cn/s/abc123", "三体", pwd="", file_count=0)])

        with store._session() as conn:
            row = conn.execute("SELECT pwd, file_count FROM resources").fetchone()
        self.assertEqual(row["pwd"], "1234")
        self.assertEqual(row["file_count"], 9)


class TestSearch(StoreTestBase):
    def test_basic_recall(self):
        store.upsert_many([
            _row("https://pan.quark.cn/s/a1", "三体 全集"),
            _row("https://pan.quark.cn/s/a2", "庆余年 第二季"),
        ])
        hit = store.search("三体")
        self.assertEqual(hit["total"], 1)
        self.assertEqual(hit["items"][0]["title"], "三体 全集")

    def test_cross_space_query(self):
        """标题带空格，查询不带空格（以及反过来），都必须命中。"""
        store.upsert_many([_row("https://pan.quark.cn/s/a1", "三体 全集 4K")])
        self.assertEqual(store.search("三体全集")["total"], 1)
        self.assertEqual(store.search("三体 全集")["total"], 1)

    def test_alias_searchable(self):
        """别名也要能被搜到——命中率靠这个，不靠标题写得多准。"""
        store.upsert_many([_row("https://pan.quark.cn/s/a1", "三体 全集")])
        store.upsert_many([_row("https://pan.quark.cn/s/a1", "三体1-3季+小说")])
        self.assertEqual(store.search("三体1-3季")["total"], 1)

    def test_bad_state_hidden(self):
        """死链不出现在结果里，是隐藏不是扣分。"""
        store.upsert_many([
            _row("https://pan.quark.cn/s/a1", "三体 全集"),
            _row("https://pan.quark.cn/s/a2", "三体 小说"),
        ])
        with store._session() as conn:
            rid = conn.execute(
                "SELECT id FROM resources WHERE pwd_id = 'a2'"
            ).fetchone()["id"]
        store.mark_check(rid, store.STATE_BAD)

        items = store.search("三体")["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["shareUrl"], "https://pan.quark.cn/s/a1")

    def test_ok_beats_unknown(self):
        """可用性 300 分压过一切：检活通过的排前面。"""
        store.upsert_many([
            _row("https://pan.quark.cn/s/a1", "三体 全集"),
            _row("https://pan.quark.cn/s/a2", "三体 全集 高清"),
        ])
        with store._session() as conn:
            rid = conn.execute(
                "SELECT id FROM resources WHERE pwd_id = 'a2'"
            ).fetchone()["id"]
        store.mark_check(rid, store.STATE_OK)

        items = store.search("三体")["items"]
        self.assertEqual(items[0]["shareUrl"], "https://pan.quark.cn/s/a2")

    def test_platform_filter(self):
        store.upsert_many([
            _row("https://pan.quark.cn/s/a1", "三体 全集", platform="quark"),
            _row("https://pan.baidu.com/s/1b2", "三体 全集", platform="baidu"),
        ])
        self.assertEqual(store.search("三体")["total"], 2)
        self.assertEqual(store.search("三体", platforms=["baidu"])["total"], 1)


class TestHits(StoreTestBase):
    def test_counted_and_aggregated(self):
        """按 (resource, query, day) 聚合，不是每出一条写一行流水。"""
        result = store.upsert_many([_row("https://pan.quark.cn/s/a1", "三体 全集")])
        rid = result["ids"]["quark:a1"]

        store.record_hits([rid], "三体")
        store.record_hits([rid], "三体")
        store.record_hits([rid], "三体 ")

        with store._session() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM resource_hits").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT count FROM resource_hits").fetchone()["count"], 3
            )
            self.assertEqual(
                conn.execute("SELECT hit_count FROM resources").fetchone()["hit_count"], 3
            )


class TestIngest(StoreTestBase):
    def test_swallows_errors(self):
        """影子回填是纯旁路，坏数据不许把搜索链路带崩。"""
        result = store.ingest([{"shareUrl": None, "title": None}], "三体")
        self.assertEqual(result["added"], 0)

    def test_from_search_items(self):
        """直接吃 /api/search 的 items 形状。"""
        items = [{
            "shareUrl": "https://pan.quark.cn/s/a1",
            "title": "三体 全集",
            "platform": "quark",
            "sharePwd": "1234",
            "fileCount": 12,
            "sizeBytes": 1024,
            "sourceName": "tgsearchers",
            "sourceKind": "tg",
        }]
        result = store.ingest(items, "三体")
        self.assertEqual(result["added"], 1)
        self.assertEqual(store.stats()["hits_7d"], 1)


class TestMarkCheck(StoreTestBase):
    def test_revive_counted(self):
        """bad → ok 算一次复活。这个信号比任何评分都准。"""
        result = store.upsert_many([_row("https://pan.quark.cn/s/a1", "三体")])
        rid = result["ids"]["quark:a1"]
        store.mark_check(rid, store.STATE_BAD)
        store.mark_check(rid, store.STATE_OK)

        with store._session() as conn:
            row = conn.execute("SELECT check_state, revive_count FROM resources").fetchone()
        self.assertEqual(row["check_state"], store.STATE_OK)
        self.assertEqual(row["revive_count"], 1)


class TestHitsChannel(StoreTestBase):
    """命中来源必须分开记。

    混在一起就再也分不清「这条资源是上游给的」还是「库自己找得出来」——
    前者说明库还没派上用场，后者说明库在兜上游给不了的东西。P1 上线后
    这个区别就是判断本地检索到底有没有出力的唯一依据。
    """

    def _one(self) -> int:
        result = store.upsert_many([_row("https://pan.quark.cn/s/a1", "三体 全集")])
        return result["ids"]["quark:a1"]

    def test_same_day_same_query_kept_apart(self):
        rid = self._one()
        store.record_hits([rid], "三体", channel=store.CHANNEL_UPSTREAM)
        store.record_hits([rid], "三体", channel=store.CHANNEL_UPSTREAM)
        store.record_hits([rid], "三体", channel=store.CHANNEL_LOCAL)

        with store._session() as conn:
            rows = {
                r["channel"]: r["count"]
                for r in conn.execute(
                    "SELECT channel, count FROM resource_hits WHERE resource_id=?", (rid,)
                )
            }
        self.assertEqual(rows, {"upstream": 2, "local": 1})

    def test_stats_splits_by_channel(self):
        rid = self._one()
        store.record_hits([rid], "三体", channel=store.CHANNEL_UPSTREAM)
        store.record_hits([rid], "三体", channel=store.CHANNEL_LOCAL)
        store.record_hits([rid], "三体", channel=store.CHANNEL_LOCAL)

        s = store.stats()
        self.assertEqual(s["hits_7d"], 3)  # 总和，向后兼容
        self.assertEqual(s["hits_7d_by_channel"], {"upstream": 1, "local": 2})

    def test_unknown_channel_falls_back_to_upstream(self):
        """白名单归一：脏值不许自己开出一条新口径。"""
        rid = self._one()
        store.record_hits([rid], "三体", channel="从浏览器插件来的")
        store.record_hits([rid], "三体", channel="")

        with store._session() as conn:
            rows = conn.execute("SELECT DISTINCT channel FROM resource_hits").fetchall()
        self.assertEqual([r["channel"] for r in rows], ["upstream"])

    def test_ingest_passes_channel_through(self):
        items = [{
            "shareUrl": "https://pan.quark.cn/s/a1",
            "title": "三体 全集",
            "platform": "quark",
            "sharePwd": "1234",
            "fileCount": 12,
            "sizeBytes": 1024,
            "sourceName": "library",
            "sourceKind": "library",
        }]
        store.ingest(items, "三体", channel=store.CHANNEL_LOCAL)
        self.assertEqual(store.stats()["hits_7d_by_channel"], {"local": 1})


class TestDropStats(StoreTestBase):
    """入库丢弃必须留数字。

    以前 `_prepare` 里解析失败就是一句 `continue`：某个平台整类进不了库，
    看总量完全看不出来（123 / 天翼就这么被埋了很久）。这个计数存在的意义
    就是让"整类被静默丢掉"当场可见，而不是等对着库做 group by 才发现。
    """

    def setUp(self) -> None:
        super().setUp()
        store._drop_counts.clear()
        store._drop_samples.clear()

    def test_counts_and_samples(self) -> None:
        store.upsert_many([
            _row("https://www.lanzoue.com/iAbCdEf", "蓝奏资源", platform=""),
            _row("https://pan.quark.cn/s/abc", ""),
        ])
        dropped = store.stats()["dropped"]
        self.assertEqual(dropped["counts"], {"unsupported": 1, "no_title": 1})
        self.assertIn("lanzoue.com", dropped["samples"]["unsupported"][0])

    def test_115_is_no_longer_dropped(self) -> None:
        result = store.upsert_many([
            _row("https://115.com/s/swabcdef?password=1234", "115 资源", platform=""),
        ])
        self.assertEqual(result["added"], 1)
        self.assertEqual(store.stats()["by_platform"], {"115": 1})
        self.assertEqual(store.stats()["dropped"]["counts"], {})

    def test_platform_comes_from_url_not_from_caller(self) -> None:
        """调用方把 115 链接标成 quark，也不许按 quark 存。

        标错就分裂 dedup_key：同一条资源两条记录，热度各算一半。
        """
        store.upsert_many([
            _row("https://115.com/s/swabcdef", "115 资源", platform="quark"),
        ])
        self.assertEqual(store.stats()["by_platform"], {"115": 1})


class TestMigration(unittest.TestCase):
    """老库升级：`resource_hits` 没有 channel 列时，init 必须把它迁过来。

    线上已经有这种库了（113 条资源、若干命中）。迁移写错的后果是
    "启动成功但历史热度全丢"——不会报错，只是数字归零。
    """

    OLD_SCHEMA = """
        CREATE TABLE resource_hits (
            resource_id INTEGER NOT NULL,
            query       TEXT    NOT NULL,
            day         INTEGER NOT NULL,
            count       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (resource_id, query, day)
        );
        CREATE INDEX idx_hits_query ON resource_hits(query, day DESC);
        INSERT INTO resource_hits(resource_id, query, day, count)
            VALUES (1, '三体', 20261119, 7), (2, '三体', 20261119, 3);
    """

    def _make_old_db(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(self.OLD_SCHEMA)
        finally:
            conn.close()

    def test_old_hits_migrated_to_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            self._make_old_db(db)

            store.init(db)

            with store._session() as conn:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(resource_hits)")]
                self.assertIn("channel", cols)
                rows = conn.execute(
                    "SELECT resource_id, channel, count FROM resource_hits ORDER BY resource_id"
                ).fetchall()
                # 索引必须跟着重建：历史查询靠它，而且迁移后写入量的增长全靠它
                idx = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_hits_query'"
                ).fetchone()

            self.assertEqual(
                [(r["resource_id"], r["channel"], r["count"]) for r in rows],
                [(1, "upstream", 7), (2, "upstream", 3)],
            )
            self.assertIsNotNone(idx, "迁移后索引丢了，查询会退化成全表扫")

    def test_migration_is_idempotent(self):
        """服务重启会再跑一次 init。第二次不许报错，更不许把数据搬第二遍。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            self._make_old_db(db)

            store.init(db)
            store.init(db)

            with store._session() as conn:
                total = conn.execute("SELECT COUNT(*) FROM resource_hits").fetchone()[0]
            self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()
