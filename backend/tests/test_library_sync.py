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
        # ignore_cleanup_errors：同步线程可能刚好在写库，Windows 下临时目录删不掉
        # 会抛 WinError 32，把一个通过的用例报成 ERROR，误导性极大
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        store.init(Path(self._tmp.name) / "library.db")
        # 作业状态是模块级的，测试之间必须清干净，否则上一个用例的 done 会串过来
        with library_sync._lock:
            library_sync._jobs.clear()
            library_sync._running.clear()

    def tearDown(self) -> None:
        # 后台作业收尾（写进度、关连接）要一点时间，早删一毫秒就 WinError 32
        for _ in range(40):
            if not library_sync._running:
                break
            time.sleep(0.05)
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

    def test_ncols_covers_far_column(self):
        """指定的列在第 5 列，就得读 5 列。

        之前固定读 3 列：第 5 列永远读回来是空串，整张表一条也入不了库，
        而表现不是报错，是"全部跳过"——最难查的那种。
        """
        src = dict(self.source(), title_col=1, link_col=5, code_col=0)
        self.assertEqual(library_sync.read_ncols(src), 5)

    def test_ncols_keeps_scan_floor(self):
        """没指定或指定在前几列时，仍读满 COLUMN_SCAN——自动识别要看样本。"""
        src = dict(self.source(), title_col=1, link_col=2)
        self.assertEqual(library_sync.read_ncols(src), library_sync.docs_qq.COLUMN_SCAN)


class StartRowTest(SyncTestBase):
    """用户指了"第 3 行开始才是资源"，就从第 3 行读、且不许再猜表头。"""

    Q = "https://pan.quark.cn/s/a1"

    def _fake_read(self, seen: list):
        class _Meta:
            rev = "r1"
            title = "测试表"

            def tab(self, _tid):
                class _Tab:
                    title = "测试表"
                    max_row = 4
                return _Tab()

        def inner(url, **kw):
            seen.append(kw)
            # 起始行之前的广告行根本不会被读回来——这正是这个配置的意义
            return [
                ["踢毽子视频教学", "https://pan.quark.cn/s/c1"],
                ["植物大战僵尸", "https://pan.quark.cn/s/c2"],
            ], _Meta()

        return inner

    def test_reads_from_start_row(self):
        got = store.add_doc_source(pad_id="DWs", tab_id="t1", url="https://docs.qq.com/sheet/DWs", start_row=3)
        src = next(s for s in store.list_doc_sources() if s["id"] == got["id"])
        seen: list = []
        with patch.object(library_sync.docs_qq, "read_tab", self._fake_read(seen)):
            report = library_sync.sync_one(src, dry_run=True)
        self.assertEqual(seen[0]["start_row"], 2)      # 1 起转 0 起
        self.assertEqual(report.added, 2)              # 第一行没有被当表头吃掉

    def test_first_line_offset_keeps_report_rows_absolute(self):
        """增量只读后半段时，报告里的行号要对得上原表，不能从 1 重新数。"""
        rows = [["三体", "https://pan.quark.cn/s/d1"], ["狂飙", "没有链接"]]
        got = library_sync.ingest_rows(rows, "test", assume_header=False, first_line=101)
        self.assertEqual(got["skipped"][0][0], 102)

    def test_changing_config_resets_progress(self):
        """改了列号/起始行，进度归零——否则旧配置下入库的错数据永远留着。"""
        got = store.add_doc_source(pad_id="DWc", tab_id="t", url="u", title_col=1, link_col=2)
        store.save_doc_progress(got["id"], last_row=380000)
        store.set_doc_columns(got["id"], title_col=1, link_col=3, start_row=2, reset_progress=True)
        after = next(s for s in store.list_doc_sources() if s["id"] == got["id"])
        self.assertEqual(after["last_row"], 0)
        self.assertEqual(after["start_row"], 2)

    def test_saving_same_config_keeps_progress(self):
        got = store.add_doc_source(pad_id="DWk", tab_id="t", url="u", title_col=1, link_col=2)
        store.save_doc_progress(got["id"], last_row=900)
        store.set_doc_columns(got["id"], title_col=1, link_col=2, reset_progress=False)
        after = next(s for s in store.list_doc_sources() if s["id"] == got["id"])
        self.assertEqual(after["last_row"], 900)

    def test_readd_with_new_config_resets_progress(self):
        """同一张表重导、但起始行变了：进度要归零。

        不归零的话，已入库的那批还是按旧配置解析的，人看到"已登记过"会
        以为修好了，实际错数据一直躺着。
        """
        first = store.add_doc_source(pad_id="DWr", tab_id="t", url="u", title_col=1, link_col=2)
        store.save_doc_progress(first["id"], last_row=5000)
        again = store.add_doc_source(
            pad_id="DWr", tab_id="t", url="u", title_col=1, link_col=2, start_row=3
        )
        self.assertFalse(again["created"])
        self.assertTrue(again["reset"])
        self.assertEqual(again["last_row"], 0)

    def test_readd_identical_config_keeps_progress(self):
        """单纯重复点了一次导入（配置没变）不许把 38 万行重拉一遍。"""
        first = store.add_doc_source(pad_id="DWq", tab_id="t", url="u", title_col=1, link_col=2)
        store.save_doc_progress(first["id"], last_row=5000)
        again = store.add_doc_source(
            pad_id="DWq", tab_id="t", url="u", title_col=1, link_col=2
        )
        self.assertFalse(again["reset"])
        self.assertEqual(again["last_row"], 5000)


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

class DailyScheduleTest(SyncTestBase):
    """每日调度：算时间、判断要不要补跑、串行跑一遍。

    这三件事错了都不会报错——只是"某天没跑"或"跑了两遍"，而日志里看不出来。
    所以时间计算单独测，用固定时间戳构造，不依赖当前钟点。
    """

    def _fake_read(self):
        class _Meta:
            rev = "r1"
            title = "测试表"

            def tab(self, _tid):
                class _Tab:
                    title = "测试表"
                    max_row = 2
                return _Tab()

        def inner(url, **kw):
            return [["名称", "链接"], ["三体", "https://pan.quark.cn/s/daily"]], _Meta()

        return inner

    def test_next_run_before_hour_is_today(self):
        base = time.mktime((2026, 11, 20, 10, 0, 0, 0, 0, -1))
        got = library_sync.next_run_at(23, base)
        self.assertEqual(time.localtime(got)[:5], (2026, 11, 20, 23, 0))

    def test_next_run_after_hour_is_tomorrow(self):
        base = time.mktime((2026, 11, 20, 10, 0, 0, 0, 0, -1))
        got = library_sync.next_run_at(4, base)
        self.assertEqual(time.localtime(got)[:5], (2026, 11, 21, 4, 0))

    def test_next_run_at_exact_hour_goes_tomorrow(self):
        """正好卡在 4:00 的这一刻也算"过了"，否则会当场再跑一遍。"""
        base = time.mktime((2026, 11, 20, 4, 0, 0, 0, 0, -1))
        got = library_sync.next_run_at(4, base)
        self.assertEqual(time.localtime(got)[:5], (2026, 11, 21, 4, 0))

    def test_no_source_no_catch_up(self):
        self.assertFalse(library_sync._needs_catch_up(time.time()))

    def test_never_run_needs_catch_up(self):
        """刚登记的表 last_run_at=0，应该被补跑捞起来。"""
        self.source()
        self.assertTrue(library_sync._needs_catch_up(time.time()))

    def test_recent_run_no_catch_up(self):
        src = self.source()
        store.save_doc_progress(src["id"])
        self.assertFalse(library_sync._needs_catch_up(time.time() + 3600))

    def test_stale_run_needs_catch_up(self):
        src = self.source()
        store.save_doc_progress(src["id"])
        self.assertTrue(library_sync._needs_catch_up(time.time() + 25 * 3600))

    def test_run_all_handles_every_source(self):
        self.source()
        store.add_doc_source(
            pad_id="DWtwo", tab_id="t1", title="第二张", url="https://docs.qq.com/sheet/DWtwo"
        )
        with patch.object(library_sync.docs_qq, "read_tab", self._fake_read()):
            got = library_sync.run_all(poll=0.01, wait_max=10)
        self.assertEqual(got["total"], 2)
        self.assertEqual(got["ok"], 2)
        self.assertEqual(got["failed"], 0)

    def test_run_all_keeps_going_after_failure(self):
        """一份表炸了不能拖停后面那份——这是"整轮停住"最隐蔽的形态。"""
        self.source()
        store.add_doc_source(
            pad_id="DWtwo", tab_id="t1", title="第二张", url="https://docs.qq.com/sheet/DWtwo"
        )
        boom = {"left": 1}

        def flaky(url, **kw):
            if boom["left"]:
                boom["left"] -= 1
                raise RuntimeError("boom")
            return self._fake_read()(url, **kw)

        with patch.object(library_sync.docs_qq, "read_tab", flaky):
            got = library_sync.run_all(poll=0.01, wait_max=10)
        self.assertEqual(got["total"], 2)
        self.assertEqual(got["failed"], 1)
        self.assertEqual(got["ok"], 1)

    def test_daily_info_shape(self):
        info = library_sync.daily_info()
        self.assertIn("next_at", info)
        self.assertIn("last", info)

class DailyThreadTest(SyncTestBase):
    """守护线程本身：它会真的跑起来吗，跑完还记得结果吗。

    这是最容易出错也最难发现的一环——线程里抛个异常直接退出，服务照常响应，
    只是从此再也不自动同步，日志里一句 "Thread died" 也没有。
    """

    def _fake_read(self):
        class _Meta:
            rev = "r1"
            title = "测试表"

            def tab(self, _tid):
                class _Tab:
                    title = "测试表"
                    max_row = 2
                return _Tab()

        return lambda *a, **k: ([["名称", "链接"], ["三体", "https://pan.quark.cn/s/thr"]], _Meta())

    def setUp(self) -> None:
        super().setUp()
        # 别的用例可能已经起过调度（模块级状态），不复位的话 start_daily 会静默返回
        library_sync._daily_started = False
        library_sync._daily_last = {}
        library_sync._daily_next = 0.0

    def tearDown(self) -> None:
        # 模块级状态：不清掉，后面任何用例调 start_daily 都会静默返回
        library_sync._daily_started = False
        library_sync._daily_last = {}
        library_sync._daily_next = 0.0
        super().tearDown()

    def test_catch_up_runs_and_records(self):
        self.source()
        with patch.object(library_sync, "CATCH_UP_GRACE", 0), \
             patch.object(library_sync.docs_qq, "read_tab", self._fake_read()):
            library_sync.start_daily(4)
            for _ in range(80):
                if library_sync._daily_last:
                    break
                time.sleep(0.05)

        last = library_sync._daily_last
        self.assertEqual(last.get("reason"), "补跑")
        self.assertEqual(last.get("ok"), 1)
        self.assertEqual(last.get("error"), "")
        # 补跑之后必须把下一次整点排上，否则面板会一直显示"未设置"
        self.assertGreater(library_sync._daily_next, time.time())

    def test_start_twice_keeps_one_scheduler(self):
        with patch.object(library_sync, "CATCH_UP_GRACE", 0), \
             patch.object(library_sync.docs_qq, "read_tab", self._fake_read()):
            library_sync.start_daily(4)
            before = library_sync._daily_next
            library_sync.start_daily(5)      # 第二次应该被忽略
        self.assertEqual(library_sync._daily_hour, 4)
        self.assertGreater(before, 0)
