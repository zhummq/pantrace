"""影子回填写入侧的回归测试：谁该记，谁不该记。

只盯一件事——**同一份上游结果被数了几遍**。这个错了不会报错、不会崩，
只会让热榜前几名的顺序慢慢失真，而且集中在热门词上，最难察觉。

三个用例对应三种请求形态：

1. leader（真打了上游）→ 记，且必须记在新的原始链接上；
2. follower（singleflight 搭车）→ **不记**，它既没打上游也没拿到不同结果；
3. L2 缓存命中（几分钟后又搜同一个词）→ 记，那是真实需求。

跑法：``.venv/Scripts/python.exe -m unittest tests.test_export_ingest -v``
不打网络：`CHECK_ON` 关掉，`_export_search` 之前的链路全不经过。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import BackgroundTasks

import app.main as m
from app import store


def _items(n: int = 5) -> list[dict]:
    return [
        {
            "shareUrl": f"https://pan.quark.cn/s/ingest{i}",
            "title": f"三体 第{i}版",
            "platform": "quark",
            "sharePwd": "1234",
            "fileCount": 10,
            "sizeBytes": 2048,
            "sourceName": "haisou",
            "sourceKind": "plugin",
        }
        for i in range(n)
    ]


class ExportIngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store.init(Path(self._tmp.name) / "library.db")
        self._library_on = m.LIBRARY_ON
        self._check_on = m.CHECK_ON
        m.LIBRARY_ON = True
        # 探活会走网络/SQLite 排队，这里只关心回填，整段关掉
        m.CHECK_ON = False

    def tearDown(self) -> None:
        m.LIBRARY_ON = self._library_on
        m.CHECK_ON = self._check_on
        self._tmp.cleanup()

    def _export(self, *, shared: bool = False, query: str = "三体") -> dict:
        """模拟一次出口，并把 BackgroundTasks 同步跑完（响应发出后的那一步）。"""
        body = {"query": query, "items": _items()}
        if shared:
            body["shared"] = True
        background = BackgroundTasks()
        out = m._export_search(body, background, page=1)
        for task in background.tasks:
            task.func(*task.args, **task.kwargs)
        return out

    def test_leader_ingests(self):
        out = self._export()
        self.assertEqual(len(out["items"]), 5)
        stats = store.stats()
        self.assertEqual(stats["total"], 5)
        self.assertEqual(stats["hits_7d"], 5)

    def test_follower_does_not_ingest(self):
        """搭车不许重复记账——这是本文件存在的唯一理由。"""
        self._export()                       # leader：5 条、5 次命中
        self._export(shared=True)            # 搭车
        self._export(shared=True)            # 搭车
        stats = store.stats()
        self.assertEqual(stats["total"], 5, "搭车不该新增资源")
        self.assertEqual(stats["hits_7d"], 5, "搭车不该重复计命中")
        self.assertEqual(stats["hits_7d_by_channel"], {"upstream": 5})

    def test_cache_hit_still_ingests(self):
        """缓存命中不是搭车：用户是真的又搜了一次，热度要认。"""
        self._export()
        self._export()                       # 第二次命中 L2 缓存，body 里没有 shared
        self.assertEqual(store.stats()["hits_7d"], 10)

    def test_library_off_writes_nothing(self):
        m.LIBRARY_ON = False
        self._export()
        self.assertEqual(store.stats()["total"], 0)

    def test_ingest_uses_raw_url_not_signed_token(self):
        """回填点若挪到 _public_body 之后，库里全是 /go/<token> 解不开的串。"""
        out = self._export()
        self.assertTrue(out["items"][0]["shareUrl"].startswith("/go/"))
        with store._session() as conn:
            urls = [r["url"] for r in conn.execute("SELECT url FROM resources")]
        self.assertEqual(len(urls), 5)
        for url in urls:
            self.assertTrue(url.startswith("https://pan.quark.cn/s/"), url)
            self.assertNotIn("/go/", url)


if __name__ == "__main__":
    unittest.main()
