"""同步作业的离线测试。

盯三件事，都是"错了不会报错、只会少数据"的那类：

1. **分批时表头只猜一次**。每批各自猜，某批首行标题像表头就整行吃掉。
2. **进度不能顶到表外**。线上真出现过 last_row=5013 而表只有 525 行，
   之后每次增量都从 5013 起读，等于永远读空。
3. **作业状态必须收敛**。异常吞掉不记，前端会一直转圈，用户以为在跑。

不打网络：`docs_qq.read_tab` 全部打桩。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import library_sync, store


class SyncTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store.init(Path(self._tmp.name) / "library.db")
        # 作业状态是模块级的，测试之间必须清干净，否则上一个用例的 done 会串过来
        with library_sync._lock:
            library_sync._jobs.clear()
            library_sync._running.clear()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def source(self, **kw) -> dict:
        got = store.add_doc_source(pad_id="DWtest", tab_id="t1", title="测试表", url="https://docs.qq.com/sheet/DWtest")
        return next(s for s in store.list_doc_sources() if s["id"] == got["id"])


class IngestRowsTest(SyncTestBase):
    Q = "https://pan.quark.cn/s/a1"

    def test_header_guessed_only_in_first_batch(self):
        """第二批首行叫《资源链接大全》，不许被当表头吃掉。

        这是真实踩过的坑：38 万行切 76 批，每批独立猜表头，最多能吃 76 行。
        散在批次里对账根本看不出来。

        每条给不同的分享 id —— 同一条 URL 会按 dedup_key 合并成一条，
        那样测出来永远是 1，反而看不出批次有没有吃行。
        """
        rows = [["名称", "链接"]]
        rows += [[f"片名{i}", f"https://pan.quark.cn/s/a{i}"] for i in range(1, 5001)]
        rows.append(["资源链接大全", "https://pan.quark.cn/s/last"])
        got = library_sync.ingest_rows(rows, "test")
        self.assertEqual(got["added"], 5001)

    def test_skip_reasons_aggregated(self):
        """两种"没进去"要分得清：整行没链接 vs 有链接但解析不出分享 id。

        混成一个原因，排查时会误判——前者是表结构问题，后者是平台规则问题。
        """
        rows = [
            ["名称", "链接"],
            ["三体", "没有链接的备注"],
            ["狂飙", self.Q],
        ]
        got = library_sync.ingest_rows(rows, "test")
        self.assertEqual(got["added"], 1)
        self.assertEqual(got["skipped"][0][0], 2)   # 行号要对得上原表
        self.assertIn("没有网盘链接", got["skipped"][0][1])

    def test_link_column_only_scans_that_column(self):
        """指定了链接列，"备用链接"列不该被一起收进来。"""
        rows = [
            ["名称", "主链接", "备用链接"],
            ["三体", self.Q, "https://pan.baidu.com/s/1b2"],
        ]
        got = library_sync.ingest_rows(rows, "test", cols={"title_col": 1, "link_col": 2})
        self.assertEqual(got["added"], 1)


class ColsTest(SyncTestBase):
    def test_cols_of_defaults_to_zero(self):
        src = self.source()
        self.assertEqual(library_sync.cols_of(src), {"title_col": 0, "link_col": 0, "code_col": 0})

    def test_cols_of_reads_config(self):
        got = store.add_doc_source(pad_id="DWx", tab_id="t", title_col=3, link_col=5, code_col=6)
        src = next(s for s in store.list_doc_sources() if s["id"] == got["id"])
        self.assertEqual(library_sync.cols_of(src), {"title_col": 3, "link_col": 5, "code_col": 6})


class SkipSummaryTest(unittest.TestCase):
    def test_groups_by_reason(self):
        rows = [
            (2, "第 2 列没有网盘链接"),
            (3, "第 2 列没有网盘链接"),
            (4, "没有网盘链接"),
            (5, "识别不出分享 id：https://example.com/x"),
        ]
        got = library_sync.skip_summary(rows)
        self.assertEqual(got[0], {"reason": "第 2 列没有网盘链接", "count": 2})

    def test_empty(self):
        self.assertEqual(library_sync.skip_summary([]), [])


class SyncOneTest(SyncTestBase):
    """`sync_one` 的边界：进度必须夹在表长以内。"""

    Q = "https://pan.quark.cn/s/a1"

    def _fake_read(self, max_row: int, rows: list[list[str]]):
        class _Meta:
            rev = "r1"
            title = "测试表"

            def tab(self, _tid):
                class _Tab:
                    title = "测试表"
                    max_row = _Meta_maxrow
                return _Tab()

        _Meta_maxrow = max_row

        def inner(url, **kw):
            return rows, _Meta()

        return inner

    def test_progress_clamped_to_table_length(self):
        """表只有 3 行，订阅上记着 5013 —— 进度要夹回 3，不能越过表尾。

        越过之后每次增量都从 5013 起读，永远读空，而日志只写"读到 0 行"。
        """
        src = self.source()
        store.save_doc_progress(src["id"], last_row=5013)
        src = next(s for s in store.list_doc_sources() if s["id"] == src["id"])
        rows = [
            ["名称", "链接"],
            ["三体", "https://pan.quark.cn/s/b1"],
            ["狂飙", "https://pan.quark.cn/s/b2"],
        ]
        with patch.object(library_sync.docs_qq, "read_tab", self._fake_read(3, rows)):
            report = library_sync.sync_one(src)
        self.assertEqual(report.last_row, 3)
        self.assertEqual(report.added, 2)

    def test_dry_run_does_not_write_progress(self):
        src = self.source()
        rows = [["名称", "链接"], ["三体", self.Q]]
        with patch.object(library_sync.docs_qq, "read_tab", self._fake_read(2, rows)):
            library_sync.sync_one(src, dry_run=True)
        after = next(s for s in store.list_doc_sources() if s["id"] == src["id"])
        self.assertEqual(after["last_row"], 0)
        self.assertEqual(store.stats()["total"], 0)


class JobTest(SyncTestBase):
    """后台作业的状态机。前端全靠它决定"还在转"还是"出错了"。"""

    Q = "https://pan.quark.cn/s/a1"

    def _fake_sync(self, src, **kw):
        time.sleep(0.05)
        return library_sync.SyncReport(
            source_id=int(src["id"]), tab="测试表", rows=10, max_row=100,
            last_row=100, added=8, touched=2, skipped=[(2, "没有网盘链接")],
        )

    def _wait(self, sid, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = library_sync.status(sid).get("state")
            if state in ("done", "error"):
                return state
            time.sleep(0.02)
        return library_sync.status(sid).get("state")

    def test_job_completes_with_counts(self):
        src = self.source()
        with patch.object(library_sync, "sync_one", self._fake_sync):
            started = library_sync.start(src["id"])
            self.assertEqual(started["state"], "running")
            self.assertEqual(self._wait(src["id"]), "done")
        job = library_sync.status(src["id"])
        self.assertEqual((job["added"], job["touched"], job["skipped"]), (8, 2, 1))
        self.assertFalse(library_sync.is_running(src["id"]))

    def test_duplicate_start_returns_same_job(self):
        """连点两下不该起两份。两份同时写库会让"新增"看起来翻倍。"""
        src = self.source()
        with patch.object(library_sync, "sync_one", self._fake_sync):
            first = library_sync.start(src["id"])
            second = library_sync.start(src["id"])
            self.assertEqual(first["started_at"], second["started_at"])
            self.assertEqual(self._wait(src["id"]), "done")

    def test_failure_recorded_not_swallowed(self):
        """抓取抛异常时必须落进状态，还要写进 doc_sources.last_error。"""
        src = self.source()

        def boom(_src, **_kw):
            raise RuntimeError("腾讯文档改版了")

        with patch.object(library_sync, "sync_one", boom):
            library_sync.start(src["id"])
            self.assertEqual(self._wait(src["id"]), "error")
        job = library_sync.status(src["id"])
        self.assertIn("腾讯文档改版了", job["error"])
        after = next(s for s in store.list_doc_sources() if s["id"] == src["id"])
        self.assertIn("腾讯文档改版了", after["last_error"])
        self.assertFalse(library_sync.is_running(src["id"]))

    def test_unknown_source_fails_cleanly(self):
        library_sync.start(9999)
        self.assertEqual(self._wait(9999), "error")
        self.assertIn("没有 id=9999", library_sync.status(9999)["error"])


class ProbeTabTest(unittest.TestCase):
    """`probe` 的 tab 解析：链接里带了 `?tab=` 就只读那一张。

    不带的话会把七张表全列出来，用户粘的是某一张表的链接，却让他重新挑一遍。
    """

    def test_url_tab_is_used_when_not_passed(self):
        from app import docs_qq

        seen = {}

        def fake_meta(url):
            class _M:
                title = "文档"
                tabs = [
                    docs_qq.Tab("t1", "表一", 10, 2),
                    docs_qq.Tab("t2", "表二", 20, 2),
                ]
            return _M()

        def fake_read(url, tab_id="", **kw):
            seen["tab_id"] = tab_id
            return [["名称", "链接"], ["三体", "https://pan.quark.cn/s/a1"]], None

        with patch.object(library_sync.docs_qq, "fetch_meta", fake_meta), \
             patch.object(library_sync.docs_qq, "read_tab", fake_read):
            got = library_sync.probe("https://docs.qq.com/sheet/DWabc?tab=t2")
        self.assertEqual([t["tab_id"] for t in got["tabs"]], ["t2"])
        self.assertEqual(seen["tab_id"], "t2")

    def test_without_tab_lists_all(self):
        from app import docs_qq

        def fake_meta(url):
            class _M:
                title = "文档"
                tabs = [
                    docs_qq.Tab("t1", "表一", 10, 2),
                    docs_qq.Tab("t2", "表二", 20, 2),
                ]
            return _M()

        with patch.object(library_sync.docs_qq, "fetch_meta", fake_meta), \
             patch.object(library_sync.docs_qq, "read_tab", lambda *a, **k: ([], None)):
            got = library_sync.probe("https://docs.qq.com/sheet/DWabc")
        self.assertEqual(len(got["tabs"]), 2)
