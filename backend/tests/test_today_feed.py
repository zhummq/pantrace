"""今日更新（来源字段 + 新增列表）的离线测试。

盯的是"分组会不会骗人"这一类问题——数字错了没人会发现，页面上看着都正常：

1. **来源只在第一次入库时定**。一条资源被群里发过、又被搜索爬到，第二次
   入库不能把 origin 改成 upstream，否则今天的"QQ 群 12 条"明天就变成
   "上游回填 12 条"，运营只会以为群里没人发东西。
2. **老库补列**。38 万行的库不能重建，只能 ALTER。补完必须默认 upstream：
   那时候确实只有上游回填一个入口，这是事实，不是兜底。
3. **"今天"按日历日切**。用 `now - N*86400` 的话，同一天不同时刻看到的
   区间不一样，"今日新增"会随时间缩水。
4. **排序是入库时间倒序**，不掺相关性/热度。今日更新页上被算法排过序，
   用户会以为那是"推荐"而不是"刚进来的"。

跑法：``.venv/Scripts/python.exe -m unittest tests.test_today_feed -v``
不打网络：全部本地临时库。
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app import store

Q = "https://pan.quark.cn/s/aaaa1111"
B = "https://pan.baidu.com/s/bbbb2222"
A = "https://www.alipan.com/s/cccc3333"


def _row(url: str, title: str, *, kind: str = "qq", level: int = 1, name: str = "资源群") -> dict:
    # 注意键名是 shareUrl：store.ingest 读的是这个（和上游结果字段对齐），
    # 写成 url 会被当成"没链接"整条丢掉，而且不报错。
    return {
        "shareUrl": url,
        "title": title,
        "sourceName": name,
        "sourceKind": kind,
        "sourceLevel": level,
    }


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "library.db"
        store.init(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _origin(self, key: str) -> str:
        # 必须显式 close：`with sqlite3.connect(...)` 只管事务，连接照样开着，
        # Windows 上会一直锁着 .db 文件，tearDown 删临时目录直接 PermissionError。
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT origin FROM resources WHERE dedup_key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else ""


class OriginTest(_Base):
    def test_origin_recorded_per_channel(self):
        store.ingest([_row(Q, "三体 全集", kind="qq")])
        store.ingest([_row(B, "活着", kind="tg")])
        store.ingest([_row(A, "流浪地球", kind="doc")])
        self.assertEqual(self._origin("quark:aaaa1111"), "qq")
        self.assertEqual(self._origin("baidu:bbbb2222"), "tg")
        self.assertEqual(self._origin("ali:cccc3333"), "doc")

    def test_unknown_kind_falls_back_to_upstream(self):
        """adapter 对不认识的上游给的是 unknown，那确实是从搜索链路来的。"""
        store.ingest([_row(Q, "三体 全集", kind="unknown")])
        self.assertEqual(self._origin("quark:aaaa1111"), "upstream")

    def test_origin_does_not_change_on_reingest(self):
        """群里发过的资源又被搜索爬到，来源不能被改写成 upstream。"""
        store.ingest([_row(Q, "三体 全集", kind="qq")])
        store.ingest([_row(Q, "三体 全集（重发）", kind="plugin", level=2)])
        self.assertEqual(self._origin("quark:aaaa1111"), "qq")

    def test_batch_keeps_front_origin(self):
        """同一批里同一资源来自两个入口，留更靠前（人工筛过）的那个。"""
        store.ingest([
            _row(Q, "三体 全集", kind="plugin", level=2),
            _row(Q, "三体 全集", kind="qq", level=1),
        ])
        self.assertEqual(self._origin("quark:aaaa1111"), "qq")


class MigrationTest(unittest.TestCase):
    def test_old_db_gets_origin_column(self):
        """老库（没有 origin 列）启动时补列，历史行默认 upstream。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE resources (
                    id            INTEGER PRIMARY KEY,
                    dedup_key     TEXT NOT NULL UNIQUE,
                    platform      TEXT NOT NULL,
                    pwd_id        TEXT NOT NULL,
                    title         TEXT NOT NULL,
                    aliases       TEXT NOT NULL DEFAULT '',
                    url           TEXT NOT NULL,
                    pwd           TEXT NOT NULL DEFAULT '',
                    file_count    INTEGER NOT NULL DEFAULT 0,
                    size_bytes    INTEGER NOT NULL DEFAULT 0,
                    source_name   TEXT NOT NULL DEFAULT '',
                    source_level  INTEGER NOT NULL DEFAULT 3,
                    check_state   TEXT NOT NULL DEFAULT 'unknown',
                    check_at      INTEGER NOT NULL DEFAULT 0,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at  INTEGER NOT NULL,
                    hit_count     INTEGER NOT NULL DEFAULT 0,
                    click_count   INTEGER NOT NULL DEFAULT 0,
                    revive_count  INTEGER NOT NULL DEFAULT 0,
                    archived_at   INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute(
                "INSERT INTO resources (dedup_key, platform, pwd_id, title, url,"
                " first_seen_at, last_seen_at) VALUES ('quark:old00001','quark',"
                "'old00001','老数据','https://pan.quark.cn/s/old00001',1,1)"
            )
            conn.commit()
            conn.close()

            store.init(path)
            # 同样要显式 close：Windows 上留着连接，临时目录删不掉
            conn = sqlite3.connect(path)
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(resources)")]
                self.assertIn("origin", cols)
                self.assertEqual(
                    conn.execute(
                        "SELECT origin FROM resources WHERE dedup_key='quark:old00001'"
                    ).fetchone()[0],
                    "upstream",
                )
            finally:
                conn.close()


class RecentTest(_Base):
    def test_newest_first(self):
        now = int(time.time())
        store.ingest([_row(Q, "早的")], now=now - 3600)
        store.ingest([_row(B, "晚的")], now=now - 60)
        got = store.recent(since=now - 86400, now=now)
        self.assertEqual([i["title"] for i in got["items"]], ["晚的", "早的"])
        self.assertEqual(got["total"], 2)

    def test_since_window_excludes_old(self):
        now = int(time.time())
        store.ingest([_row(Q, "昨天的")], now=now - 3 * 86400)
        store.ingest([_row(B, "今天的")], now=now - 60)
        got = store.recent(since=store_day_start(now), now=now)
        self.assertEqual([i["title"] for i in got["items"]], ["今天的"])

    def test_origin_filter(self):
        now = int(time.time())
        store.ingest([_row(Q, "群里的", kind="qq")], now=now - 60)
        store.ingest([_row(B, "爬来的", kind="plugin")], now=now - 50)
        got = store.recent(since=now - 86400, origin="qq", now=now)
        self.assertEqual([i["title"] for i in got["items"]], ["群里的"])

    def test_item_carries_origin_fields(self):
        store.ingest([_row(Q, "三体", kind="qq")])
        item = store.recent(since=int(time.time()) - 3600)["items"][0]
        self.assertEqual(item["origin"], "qq")
        self.assertEqual(item["originName"], "QQ 群")

    def test_archived_hidden(self):
        now = int(time.time())
        store.ingest([_row(Q, "归档的")], now=now - 60)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE resources SET archived_at = ?", (now,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(store.recent(since=now - 86400, now=now)["total"], 0)

    def test_pagination(self):
        now = int(time.time())
        for i in range(5):
            store.ingest([_row(f"https://pan.quark.cn/s/page{i:04d}", f"第{i}条")],
                         now=now - 100 + i)
        first = store.recent(since=now - 86400, limit=2, offset=0, now=now)
        second = store.recent(since=now - 86400, limit=2, offset=2, now=now)
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(first["total"], 5)
        self.assertNotEqual(first["items"][0]["title"], second["items"][0]["title"])


class SummaryTest(_Base):
    def test_groups_in_fixed_order(self):
        now = int(time.time())
        store.ingest([_row(Q, "群里的", kind="qq")], now=now - 60)
        store.ingest([_row(B, "爬来的", kind="plugin", level=2)], now=now - 50)
        got = store.day_summary(store_day_start(now), now=now)
        self.assertEqual(got["total"], 2)
        # QQ 群在前，上游回填在后——不按数量排，来源顺序本身是稳定信息
        self.assertEqual([x["key"] for x in got["byOrigin"]], ["qq", "plugin"])
        self.assertEqual(got["byOrigin"][0]["name"], "QQ 群")

    def test_empty_day(self):
        now = int(time.time())
        got = store.day_summary(store_day_start(now), now=now)
        self.assertEqual(got["total"], 0)
        self.assertEqual(got["byOrigin"], [])


def store_day_start(now: float) -> int:
    y, m, d = time.localtime(now)[:3]
    return int(time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)))


if __name__ == "__main__":
    unittest.main()
