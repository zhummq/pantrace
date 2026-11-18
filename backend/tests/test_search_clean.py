"""清洗层与合并排序层的行为验证。

这些用例的目的不是"跑通"，而是**证明机制真的在起作用**：
- 一条消息里两个链接各带各的提取码，取出的码必须各归各位；
- 排序确实按「时间 + 关键词 + 源等级」三项相加，而不是原样返回。
"""

from __future__ import annotations

import time
import unittest

from app.search import clean, merge
from app.search.types import Link, SearchResult


class LinkTypeTest(unittest.TestCase):
    def test_get_link_type(self):
        cases = {
            "https://pan.baidu.com/s/1abc": "baidu",
            "https://pan.quark.cn/s/abc": "quark",
            "https://www.alipan.com/s/abc": "aliyun",
            "https://drive.uc.cn/s/abc": "uc",
            "https://cloud.189.cn/t/abc": "tianyi",
            "https://pan.xunlei.com/s/abc": "xunlei",
            "https://123684.com/s/abc": "123",
            "https://115.com/s/abc": "115",
            "magnet:?xt=urn:btih:abc": "magnet",
            "ed2k://|file|a|1|ABC|/": "ed2k",
            "https://example.com/x": "others",
        }
        for url, expected in cases.items():
            self.assertEqual(clean.get_link_type(url), expected, url)

    def test_link_prefix_stripped(self):
        # 链接前面带"链接："前缀时也要认出来
        self.assertEqual(clean.get_link_type("链接：https://pan.baidu.com/s/1abc"), "baidu")


class CleanUrlTest(unittest.TestCase):
    def test_baidu_pwd_stripped_then_rebuilt(self):
        cand = clean.LinkCandidates()
        cand.add("baidu", "https://pan.baidu.com/s/1abcDEF?pwd=x7k2", "x7k2")
        links = cand.finalize()

        self.assertEqual(len(links), 1)
        # 密码单独存一份，URL 上再拼回去 —— 两处都要对
        self.assertEqual(links[0].password, "x7k2")
        self.assertEqual(links[0].url, "https://pan.baidu.com/s/1abcDEF?pwd=x7k2")

    def test_123_adds_protocol_when_missing(self):
        self.assertEqual(
            clean.clean_123_pan_url("123pan.com/s/abc123"),
            "https://123pan.com/s/abc123",
        )

    def test_115_password_cut_at_four(self):
        url = "https://115.com/s/abc123?password=ab12"
        self.assertEqual(clean.clean_115_pan_url(url), url)

    def test_uc_share_code_containing_123_survives(self):
        # 分享码里出现 "123" 不能被当成结束标记切掉
        url = "https://drive.uc.cn/s/abc123def"
        self.assertEqual(clean.clean_uc_pan_url(url), url)


class ExtractPasswordTest(unittest.TestCase):
    def test_url_param_wins(self):
        self.assertEqual(
            clean.extract_password("提取码：zzzz", "https://pan.baidu.com/s/1a?pwd=ab12"),
            "ab12",
        )

    def test_fullwidth_colon(self):
        self.assertEqual(clean.extract_password("提取码：ab12", ""), "ab12")

    def test_tianyi_access_code_in_url(self):
        url = "https://cloud.189.cn/t/abc（访问码：k3m9）"
        self.assertEqual(clean.extract_password("", url), "k3m9")

    def test_no_password_returns_empty(self):
        self.assertEqual(clean.extract_password("这里没有码", ""), "")

    def test_is_valid_password(self):
        self.assertTrue(clean.is_valid_password("ab12"))
        self.assertFalse(clean.is_valid_password("ab-12"))
        self.assertFalse(clean.is_valid_password("码12"))


class PasswordAnchoringTest(unittest.TestCase):
    """D6 的核心：多链接消息里提取码不能互相串位。"""

    TEXT = (
        "三体全集 4K\n"
        "链接：https://pan.baidu.com/s/1abcDEF 提取码：x7k2\n"
        "备用：https://pan.quark.cn/s/aaa111 提取码：q9m3\n"
        "再备用：https://pan.xunlei.com/s/ccc222 提取码：w5n8"
    )

    def test_each_link_gets_its_own_code(self):
        cand = clean.LinkCandidates()
        for url in (
            "https://pan.baidu.com/s/1abcDEF",
            "https://pan.quark.cn/s/aaa111",
            "https://pan.xunlei.com/s/ccc222",
        ):
            pw = clean.password_for(url, self.TEXT, self.TEXT)
            cand.add(clean.get_link_type(url), url, pw)

        got = {link.url: link.password for link in cand.finalize()}
        self.assertEqual(got["https://pan.baidu.com/s/1abcDEF?pwd=x7k2"], "x7k2")
        self.assertEqual(got["https://pan.quark.cn/s/aaa111"], "q9m3")
        self.assertEqual(got["https://pan.xunlei.com/s/ccc222"], "w5n8")

    def test_old_whole_text_extraction_would_cross_wires(self):
        """反例：不按锚定位时，第二条链接拿到的是垃圾，不是它自己的码。

        旧逻辑按 "提取码" 切分后取第一个冒号，正文里 "链接：" 的冒号比
        "https:" 更靠前，于是切出 "https://..."，再取前 4 位得到 "http"。
        这就是为什么必须加「以链接为锚、只往后看 60 字节」这一层。
        """
        self.assertEqual(
            clean.extract_password(self.TEXT, "https://pan.quark.cn/s/aaa111"),
            "http",
        )

    def test_link_without_code_is_kept(self):
        cand = clean.LinkCandidates()
        cand.add("quark", "https://pan.quark.cn/s/nocode", "")
        links = cand.finalize()

        self.assertEqual(len(links), 1, "无提取码的链接不许丢")
        self.assertEqual(links[0].password, "")
        self.assertEqual(links[0].url, "https://pan.quark.cn/s/nocode")

    def test_short_context_without_anchor_uses_whole_context(self):
        # 按钮标签很短、里面没写链接本身，整段就是它的上下文
        self.assertEqual(clean.extract_code_near("https://pan.quark.cn/s/x", "提取码：ab12"), "ab12")

    def test_long_context_without_anchor_gives_up(self):
        # 长正文里找不到这个链接 → 无法判断位置，返回空让调用方回退
        long_text = "无关内容" * 80
        self.assertEqual(clean.extract_code_near("https://pan.quark.cn/s/x", long_text), "")


class ExtractLinksTest(unittest.TestCase):
    def test_multi_platform_order(self):
        text = (
            "https://pan.baidu.com/s/1abcDEF?pwd=x7k2 "
            "https://pan.quark.cn/s/aaa111 "
            "https://pan.xunlei.com/s/ccc222"
        )
        links = clean.extract_net_disk_links(text)
        # 百度那条的 ?pwd= 会被剥掉：CleanBaiduPanURL 把 "?pwd=" 当结束标记先截断，
        # 紧接着那句 `if strings.Contains(url, "?pwd=")` 因此永远进不去。
        # 这是 pansou 原样行为，密码由 ExtractPassword 单独取回，别在这里"修"。
        self.assertEqual(
            links,
            [
                "https://pan.baidu.com/s/1abcDEF",
                "https://pan.quark.cn/s/aaa111",
                "https://pan.xunlei.com/s/ccc222",
            ],
        )

    def test_123_duplicate_collapsed(self):
        text = "https://123pan.com/s/abc123 https://123pan.com/s/abc123"
        self.assertEqual(clean.extract_net_disk_links(text), ["https://123pan.com/s/abc123"])


class MergeTest(unittest.TestCase):
    def test_same_key_keeps_more_complete(self):
        thin = SearchResult(unique_id="src-1", title="三体")
        rich = SearchResult(
            unique_id="src-1",
            title="三体全集 全四季 4K",
            content="片名：三体",
            links=[Link(type="quark", url="https://pan.quark.cn/s/a")],
        )

        merged = merge.merge_search_results([thin], [rich])
        self.assertEqual(len(merged), 1)
        self.assertIs(merged[0], rich, "同键应取信息更完整的那条")

    def test_sorted_by_datetime_desc(self):
        now = time.time()
        a = SearchResult(unique_id="a", title="旧", datetime=now - 86400)
        b = SearchResult(unique_id="b", title="新", datetime=now)
        merged = merge.merge_search_results([a], [b])
        self.assertEqual([r.unique_id for r in merged], ["b", "a"])

    def test_completeness_uses_byte_length_for_title(self):
        """10 个汉字的标题在 pansou 里是 +3（30 字节 / 10），不是 +1。"""
        title = "十个汉字标题啊哈呀嘿"          # 正好 10 个字 = 30 字节
        r = SearchResult(title=title)
        self.assertEqual(len(title), 10)
        self.assertEqual(r.completeness_score(), 3)


class ScoreTest(unittest.TestCase):
    def test_time_score_buckets(self):
        now = time.time()
        day = 86400
        self.assertEqual(merge.calculate_time_score(0), 0)
        self.assertEqual(merge.calculate_time_score(now - day * 0.5), 500)
        self.assertEqual(merge.calculate_time_score(now - day * 2), 400)
        self.assertEqual(merge.calculate_time_score(now - day * 5), 300)
        self.assertEqual(merge.calculate_time_score(now - day * 20), 200)
        self.assertEqual(merge.calculate_time_score(now - day * 60), 100)
        self.assertEqual(merge.calculate_time_score(now - day * 200), 50)
        self.assertEqual(merge.calculate_time_score(now - day * 800), 20)

    def test_keyword_priority(self):
        total = len(merge.PRIORITY_KEYWORDS)
        self.assertEqual(merge.get_keyword_priority("三体 合集"), total * 70)
        self.assertEqual(merge.get_keyword_priority("三体 COMPLETE"), 70)
        self.assertEqual(merge.get_keyword_priority("三体"), 0)

    def test_result_source(self):
        self.assertEqual(merge.get_result_source(SearchResult(channel="abc")), "tg:abc")
        self.assertEqual(merge.get_result_source(SearchResult(unique_id="src-9")), "plugin:src")
        self.assertEqual(merge.get_result_source(SearchResult()), "unknown")

    def test_plugin_level_score(self):
        from app.search.base import BasePlugin
        from app.search.registry import register

        class Lvl1Fake(BasePlugin):
            name = "lvl1fakesrc"
            level = 1

        register(Lvl1Fake)
        self.assertEqual(merge.get_plugin_level_score("plugin:lvl1fakesrc"), 1000)
        self.assertEqual(merge.get_plugin_level_score("plugin:never_registered"), 0)
        self.assertEqual(merge.get_plugin_level_score("tg:anychannel"), 0)

    def test_sort_puts_collection_ahead_of_fresher_item(self):
        now = time.time()
        fresh = SearchResult(
            unique_id="fresh", title="三体", datetime=now, channel="ch", content="x"
        )
        old_collection = SearchResult(
            unique_id="old", title="三体 合集 全四季", datetime=now - 86400 * 10,
            channel="ch", content="x",
        )
        ordered = merge.sort_results_by_time_and_keywords([fresh, old_collection])
        self.assertEqual(
            [r.unique_id for r in ordered], ["old", "fresh"],
            "关键词分 490 应压过 10 天的时间差",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
