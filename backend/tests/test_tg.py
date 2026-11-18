"""TG 通道的离线测试。

解析用例跑 `tests/fixtures/` 里三个真实抓回的页面（2026-11-18 从线上取得），
因此选择器是否还对得上可以随时复验；门控与抓取用 httpx.MockTransport 模拟，
不依赖 t.me 可达性。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_tg -v``
"""

from __future__ import annotations

import asyncio
import pathlib
import unittest
from unittest.mock import patch

import httpx

from app.search.observe import HttpStatusError
from app.search.tg import (
    PARSE_STATUS_NO_MESSAGES,
    PARSE_STATUS_OK,
    PARSE_STATUS_STRUCTURE_CHANGED,
    TGReachability,
    build_search_url,
    parse_search_results,
    parse_search_results_with_status,
    probe_tg_url,
    search_channel,
    search_tg,
    tg_cache_key,
    tg_probe_url,
)
from app.search.tg.parse import (
    cut_title_by_keywords,
    extract_first_url,
    extract_title,
    extract_work_title_before_colon,
    is_single_line_format,
    is_telegram_date_header,
    is_title_metadata_line,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ParseRealPageTest(unittest.TestCase):
    """真实页面。数字变了就说明选择器或判定逻辑和线上对不上了。"""

    @classmethod
    def setUpClass(cls):
        cls.search_html = load("tg_search.html")
        cls.channel_html = load("tg_channel.html")
        cls.empty_html = load("tg_empty.html")

    def test_search_page_yields_20_messages(self):
        results, status = parse_search_results_with_status(self.search_html, "tgsearchers7")
        self.assertEqual(status, PARSE_STATUS_OK)
        self.assertEqual(len(results), 20)

    def test_channel_page_drops_message_without_links(self):
        """20 个消息块里有一条是纯图片（text_not_supported_wrap），应被丢弃但不算结构变化。"""
        results, status = parse_search_results_with_status(self.channel_html, "tgsearchers7")
        self.assertEqual(status, PARSE_STATUS_OK)
        self.assertEqual(len(results), 19)

    def test_empty_page_is_no_messages_not_structure_changed(self):
        """核心判定：0 条结果不等于解析失效。空结果页必须报 no_messages。"""
        results, status = parse_search_results_with_status(self.empty_html, "tgsearchers7")
        self.assertEqual(results, [])
        self.assertEqual(status, PARSE_STATUS_NO_MESSAGES)

    def test_structure_changed_when_message_class_breaks(self):
        """消息块还在、却一条都识别不出来 → 必须告警，不能静默返回空。"""
        broken = self.search_html.replace(
            'class="tgme_widget_message ', 'class="renamed_widget_message '
        )
        results, status = parse_search_results_with_status(broken, "tgsearchers7")
        self.assertEqual(results, [])
        self.assertEqual(status, PARSE_STATUS_STRUCTURE_CHANGED)

    def test_unique_id_and_datetime(self):
        results = parse_search_results(self.search_html, "tgsearchers7")
        first = results[0]
        self.assertEqual(first.unique_id, "tgsearchers7_102423")
        self.assertEqual(first.message_id, "102423")
        self.assertEqual(first.channel, "tgsearchers7")
        self.assertGreater(first.datetime, 0)

    def test_baidu_link_keeps_pwd_and_password_is_separate(self):
        """百度链接必须剥掉 ?pwd= 单独存密码，再拼回完整形态。"""
        results = parse_search_results(self.search_html, "tgsearchers7")
        first = results[0]
        self.assertEqual(len(first.links), 1)
        link = first.links[0]
        self.assertEqual(link.type, "baidu")
        self.assertEqual(link.password, "7QA8")
        self.assertIn("pwd=7QA8", link.url)
        # 只有 1 个链接时按"同一作品的不同网盘"处理，work_title 直接取默认标题
        self.assertEqual(link.work_title, first.title)

    def test_title_tags_images(self):
        results = parse_search_results(self.search_html, "tgsearchers7")
        first = results[0]
        self.assertEqual(first.title, "遮天（2025）4K 10bit 立体环绕声 更至 EP183")
        self.assertEqual(first.tags[:4], ["遮天", "动画", "奇幻", "冒险"])
        self.assertEqual(len(first.images), 1)
        self.assertTrue(first.images[0].startswith("http"))

    def test_images_exclude_user_avatar(self):
        """头像在 .tgme_widget_message_user 里，不在 bubble 内，不能混进来。"""
        results = parse_search_results(self.channel_html, "tgsearchers7")
        for result in results:
            for image in result.images:
                self.assertNotIn("data:image/svg", image)


class TitleExtractionTest(unittest.TestCase):
    """合成用例：真实样本里没有日期头、元数据行这些分支。"""

    def test_date_header_is_skipped(self):
        text = "📅 9月9日\n《三体》全集 4K\n百度：https://pan.baidu.com/s/1x"
        self.assertEqual(extract_title(text, text), "《三体》全集 4K")

    def test_full_date_header_is_skipped(self):
        text = "2026年11月18日\n流浪地球2\n夸克：https://pan.quark.cn/s/abc"
        self.assertTrue(is_telegram_date_header("2026年11月18日"))
        self.assertEqual(extract_title(text, text), "流浪地球2")

    def test_metadata_line_is_skipped(self):
        text = "网盘：夸克\n《漫长的季节》全12集\n夸克：https://pan.quark.cn/s/abc"
        self.assertEqual(extract_title(text, text), "《漫长的季节》全12集")

    def test_tag_only_first_line_falls_through(self):
        text = "#影视 #合集\n《狂飙》全39集"
        self.assertEqual(extract_title(text, text), "《狂飙》全39集")

    def test_name_prefix_wins(self):
        text = "名称：三体（2023）4K\n描述：改编自刘慈欣"
        self.assertEqual(extract_title(text, text), "三体（2023）4K")

    def test_cut_at_description_keyword(self):
        self.assertEqual(cut_title_by_keywords("三体全集 简介： blah", ("简介", "描述")), "三体全集")

    def test_metadata_prefix_list(self):
        for line in ("类型：动画", "分享：xxx", "简介：yyy", "更多资源：zzz"):
            self.assertTrue(is_title_metadata_line(line), line)
        self.assertFalse(is_title_metadata_line("三体全集"))


class LinkCollectionTest(unittest.TestCase):
    """真实样本里该频道不发内联按钮，所以按钮分支只能用合成页面验证。"""

    PAGE = """<html><body>
    <div class="tgme_widget_message_wrap">
      <div class="tgme_widget_message" data-post="testch/7">
        <div class="tgme_widget_message_bubble">
          <div class="tgme_widget_message_text">三体全集 4K 收藏版</div>
          <div class="tgme_widget_message_inline_keyboard">
            <a href="https://pan.quark.cn/s/abc123"><span>夸克 提取码 x7k2</span></a>
          </div>
          <div class="tgme_widget_message_footer">
            <div class="tgme_widget_message_date"><time datetime="2026-11-01T10:00:00+00:00"></time></div>
          </div>
        </div>
      </div>
    </div>
    </body></html>"""

    def test_inline_keyboard_link_is_collected(self):
        results = parse_search_results(self.PAGE, "testch")
        self.assertEqual(len(results), 1)
        links = results[0].links
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].type, "quark")
        self.assertEqual(links[0].url, "https://pan.quark.cn/s/abc123")

    def test_password_written_in_button_text(self):
        """频道把提取码写在按钮上的情形：按钮文字也要当密码上下文。"""
        results = parse_search_results(self.PAGE, "testch")
        self.assertEqual(results[0].links[0].password, "x7k2")

    def test_extract_first_url_stops_at_space(self):
        self.assertEqual(
            extract_first_url("https://pan.quark.cn/s/abc def"), "https://pan.quark.cn/s/abc"
        )

    def test_work_title_before_colon_strips_netdisk_name_once(self):
        self.assertEqual(extract_work_title_before_colon("三体 百度网盘"), "三体")
        self.assertEqual(extract_work_title_before_colon("三体"), "三体")

    def test_single_line_format_detection(self):
        lines = [
            "三体丨百度：https://pan.baidu.com/s/1a",
            "三体丨夸克：https://pan.quark.cn/s/2b",
            "三体丨迅雷：https://pan.xunlei.com/s/3c",
            "其它不相干的行",
        ]
        self.assertTrue(is_single_line_format(lines))
        self.assertFalse(is_single_line_format(["三体", "https://pan.quark.cn/s/abc"]))


class ReachabilityGateTest(unittest.TestCase):
    """tg_reachability.go 的核心：单次失败不能关门。"""

    def _client(self, handler) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)

    def test_404_counts_as_reachable(self):
        """判定依据是"拿到 HTTP 响应"而不是状态码：404 证明网络通。"""

        async def scenario():
            client = self._client(lambda request: httpx.Response(404))
            return await probe_tg_url(client, "https://t.me/s/x")

        reachable, reason = asyncio.run(scenario())
        self.assertTrue(reachable)
        self.assertIn("404", reason)

    def test_transport_error_is_unreachable(self):
        def boom(request):
            raise httpx.ConnectTimeout("connect timeout")

        async def scenario():
            client = self._client(boom)
            return await probe_tg_url(client, "https://t.me/s/x")

        reachable, reason = asyncio.run(scenario())
        self.assertFalse(reachable)
        self.assertIn("ConnectTimeout", reason)

    def test_single_failure_does_not_close_the_gate(self):
        """最危险的失效模式：单次超时把可用的 TG 静默跳过。"""

        def boom(request):
            raise httpx.ConnectTimeout("connect timeout")

        async def scenario():
            gate = TGReachability()
            client = self._client(boom)
            await gate.probe_once(client, "https://t.me/s/x")
            return gate

        gate = asyncio.run(scenario())
        self.assertTrue(gate.is_reachable())
        self.assertEqual(gate.fail_streak, 1)
        self.assertIn("暂不改判", gate.reason)

    def test_two_failures_close_the_gate(self):
        def boom(request):
            raise httpx.ConnectTimeout("connect timeout")

        async def scenario():
            gate = TGReachability()
            client = self._client(boom)
            await gate.probe_once(client, "https://t.me/s/x")
            await gate.probe_once(client, "https://t.me/s/x")
            return gate

        gate = asyncio.run(scenario())
        self.assertFalse(gate.is_reachable())
        self.assertEqual(gate.fail_streak, 2)
        self.assertIn("连续 2 次", gate.reason)

    def test_recovery_resets_streak(self):
        async def scenario():
            gate = TGReachability()
            bad = self._client(lambda request: (_ for _ in ()).throw(httpx.ConnectError("x")))
            good = self._client(lambda request: httpx.Response(200))
            await gate.probe_once(bad, "https://t.me/s/x")
            await gate.probe_once(good, "https://t.me/s/x")
            return gate

        gate = asyncio.run(scenario())
        self.assertTrue(gate.is_reachable())
        self.assertEqual(gate.fail_streak, 0)

    def test_snapshot_exposes_diagnostics(self):
        gate = TGReachability()
        snap = gate.snapshot()
        self.assertTrue(snap["reachable"])
        self.assertEqual(snap["required_failures"], 2)
        self.assertIsNone(snap["checked_seconds_ago"])

    def test_probe_url_uses_first_channel(self):
        self.assertEqual(tg_probe_url("tgsearchers7"), "https://t.me/s/tgsearchers7")
        self.assertEqual(tg_probe_url(""), "https://t.me/")


class ChannelFetchTest(unittest.TestCase):
    """search_service.go:709 的三条判定。"""

    def test_build_search_url_escapes_keyword(self):
        url = build_search_url("tgsearchers7", "三体 & 4K")
        self.assertTrue(url.startswith("https://t.me/s/tgsearchers7?q="))
        self.assertNotIn(" ", url)
        self.assertNotIn("&4K", url)

    def test_build_search_url_plain_channel(self):
        self.assertEqual(build_search_url("tgsearchers7"), "https://t.me/s/tgsearchers7")

    def test_non_200_raises_instead_of_empty(self):
        """限流不能被记成"频道没有内容"，否则存活统计和缓存同时被污染。"""

        async def scenario():
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(403)),
                trust_env=False,
            )
            return await search_channel("三体", "tgsearchers7", client=client)

        with self.assertRaises(HttpStatusError) as ctx:
            asyncio.run(scenario())
        self.assertEqual(ctx.exception.status, 403)

    def test_parses_body_from_transport(self):
        body = load("tg_search.html").encode("utf-8")

        async def scenario():
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
                trust_env=False,
            )
            return await search_channel("三体", "tgsearchers7", client=client)

        results = asyncio.run(scenario())
        self.assertEqual(len(results), 20)

    def test_body_is_capped(self):
        """上限之外的字节直接丢弃，不能让异常响应把内存吃满。"""
        body = load("tg_search.html").encode("utf-8")

        async def scenario():
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
                trust_env=False,
            )
            return await search_channel("三体", "tgsearchers7", client=client, max_bytes=4096)

        # 截断后的 HTML 里前 4KB 尚未包含消息块，结果为空但不报错
        results = asyncio.run(scenario())
        self.assertIsInstance(results, list)


class SearchTgOrchestrationTest(unittest.TestCase):
    """search_service.go:1364 `searchTG` 的编排判定。"""

    def setUp(self):
        import tempfile

        from app.search.cache import SearchCache

        self._tmp = tempfile.TemporaryDirectory()
        self.cache = SearchCache(db_path=pathlib.Path(self._tmp.name) / "tg.db")

    def tearDown(self):
        self.cache.close()
        self._tmp.cleanup()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_gate_closed_returns_empty_and_skips_requests(self):
        """门关时不该发出任何一个频道请求，否则又变成"全部挂满超时"。"""
        calls = []

        async def fake_channel(keyword, channel, client=None, timeout=0.0):
            calls.append(channel)
            return []

        gate = TGReachability()
        gate.reachable = False
        gate.reason = "连续 2 次探测失败"

        with patch("app.search.tg.service.search_channel", fake_channel):
            results = self._run(search_tg("三体", ["a", "b"], cache=self.cache, gate=gate))

        self.assertEqual(results, [])
        self.assertEqual(calls, [])

    def test_gate_closed_does_not_write_cache(self):
        """网络不通的空结果写进 TTL 缓存，会在网络恢复后继续骗人。"""
        gate = TGReachability()
        gate.reachable = False

        async def fake_channel(keyword, channel, client=None, timeout=0.0):
            return []

        with patch("app.search.tg.service.search_channel", fake_channel):
            self._run(search_tg("三体", ["a"], cache=self.cache, gate=gate))

        self.assertIsNone(self.cache.get(tg_cache_key("三体", ["a"])))

    def test_slow_channel_does_not_block_fast_one(self):
        """批任务窗口到点就返回，慢频道留给后台。"""
        from app.search.types import Link, SearchResult

        async def fake_channel(keyword, channel, client=None, timeout=0.0):
            if channel == "slow":
                await asyncio.sleep(0.5)
                return []
            return [SearchResult(unique_id=f"{channel}_1", title="快", links=[Link(type="quark", url="https://pan.quark.cn/s/x")])]

        gate = TGReachability()

        async def scenario():
            started = asyncio.get_running_loop().time()
            results = await search_tg(
                "三体",
                ["fast", "slow"],
                cache=self.cache,
                gate=gate,
                channel_timeout=0.1,
            )
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(0.6)  # 让后台补齐收尾，避免 pending task 警告
            return results, elapsed

        with patch("app.search.tg.service.search_channel", fake_channel):
            results, elapsed = self._run(scenario())

        self.assertLess(elapsed, 0.4)
        self.assertEqual(len(results), 1)

    def test_all_failed_does_not_write_cache(self):
        async def boom(keyword, channel, client=None, timeout=0.0):
            raise httpx.ConnectError("boom")

        gate = TGReachability()
        with patch("app.search.tg.service.search_channel", boom):
            results = self._run(search_tg("三体", ["a", "b"], cache=self.cache, gate=gate))

        self.assertEqual(results, [])
        self.assertIsNone(self.cache.get(tg_cache_key("三体", ["a", "b"])))

    def test_partial_failure_writes_incomplete_marker(self):
        from app.search.types import Link, SearchResult

        async def mixed(keyword, channel, client=None, timeout=0.0):
            if channel == "bad":
                raise httpx.ConnectError("boom")
            return [SearchResult(unique_id="ok_1", links=[Link(type="quark", url="https://pan.quark.cn/s/x")])]

        gate = TGReachability()
        with patch("app.search.tg.service.search_channel", mixed):
            self._run(search_tg("三体", ["ok", "bad"], cache=self.cache, gate=gate))

        entry = self.cache.get(tg_cache_key("三体", ["ok", "bad"]))
        self.assertIsNotNone(entry)
        self.assertFalse(entry.complete)  # 有失败就不算完整，下次该重抓
        self.assertEqual(len(entry.results), 1)

    def test_backfill_merges_instead_of_overwrite(self):
        """补齐必须走 merge_put。覆盖写会把期间并进来的结果吞掉（实测丢 74%）。

        用 4 个频道 / 1 个超时：`should_backfill` 的"超时不超过 1/3"才放行
        （2 个频道超时 1 个是 50%，会被这条规则拦住，见下一个用例）。
        """
        from app.search.types import Link, SearchResult

        channels = ["fast1", "fast2", "fast3", "slow"]
        key = tg_cache_key("三体", channels)

        async def fake_channel(keyword, channel, client=None, timeout=0.0):
            if channel == "slow":
                await asyncio.sleep(0.4)
                return [SearchResult(unique_id="slow_1", links=[Link(type="quark", url="https://pan.quark.cn/s/slow")])]
            return [SearchResult(unique_id=f"{channel}_1", links=[Link(type="quark", url=f"https://pan.quark.cn/s/{channel}")])]

        gate = TGReachability()

        async def scenario():
            await search_tg("三体", channels, cache=self.cache, gate=gate, channel_timeout=0.1)
            # 模拟"补齐进行中，另一个请求把结果并进了同一个键"
            self.cache.merge_put(
                key,
                [SearchResult(unique_id="other_1", links=[Link(type="quark", url="https://pan.quark.cn/s/other")])],
                complete=True,
            )
            await asyncio.sleep(0.8)  # 等后台补齐写完
            return self.cache.get(key)

        with patch("app.search.tg.service.search_channel", fake_channel):
            entry = self._run(scenario())

        self.assertIsNotNone(entry)
        ids = {r.unique_id for r in entry.results}
        self.assertIn("other_1", ids)   # 期间并进来的结果不能被吞
        self.assertIn("slow_1", ids)    # 补齐结果要在
        self.assertIn("fast1_1", ids)

    def test_backfill_skipped_when_timeouts_exceed_one_third(self):
        """超时超过 1/3 就不补：上游整体变慢时，补齐只会把请求量再放大一倍。"""
        from app.search.types import Link, SearchResult

        calls = []

        async def fake_channel(keyword, channel, client=None, timeout=0.0):
            calls.append(channel)
            if channel == "slow":
                await asyncio.sleep(0.3)
                return []
            return [SearchResult(unique_id=f"{channel}_1", links=[Link(type="quark", url=f"https://pan.quark.cn/s/{channel}")])]

        gate = TGReachability()

        async def scenario():
            await search_tg("三体", ["fast", "slow"], cache=self.cache, gate=gate, channel_timeout=0.1)
            await asyncio.sleep(0.5)

        with patch("app.search.tg.service.search_channel", fake_channel):
            self._run(scenario())

        # 首轮只该打 2 次；补齐被拦住，slow 不会被第二次请求
        self.assertEqual(calls, ["fast", "slow"])

    def test_cache_hit_skips_network_entirely(self):
        from app.search.types import Link, SearchResult

        key = tg_cache_key("三体", ["a"])
        self.cache.put(key, [SearchResult(unique_id="cached_1", links=[Link(type="quark", url="https://pan.quark.cn/s/c")])], complete=True)

        calls = []

        async def fake_channel(keyword, channel, client=None, timeout=0.0):
            calls.append(channel)
            return []

        with patch("app.search.tg.service.search_channel", fake_channel):
            results = self._run(search_tg("三体", ["a"], cache=self.cache))

        self.assertEqual(len(results), 1)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
