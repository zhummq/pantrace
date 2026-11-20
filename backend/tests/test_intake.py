"""表格导入的离线测试。

这个模块的回归点都是"表格特有"的，文案提取那套测不到：

1. **提取码单独占一列**（值就是 `1234`，前面没有"提取码："）。
   `clean.password_for` 靠上下文关键词认码，对独立列完全失效——
   漏了这条，整表导进来全是无码资源。
2. **一行多链接共用标题**。表格里一行 = 一条资源 + 几个盘的备份，
   不是每条链接各配一个标题。
3. **整行是一段分享文案**（收集表最常见的填法）。没有任何单元格是干净标题，
   只能整行清洗一遍再取，否则是一片占位标题。
4. **详情页/站内页不进库**。解析不出分享 id 的链接转存必失败。

跑法：``.venv/Scripts/python.exe -m unittest tests.test_intake -v``
不打网络：纯字符串处理。
"""

from __future__ import annotations

import unittest

from app import intake


class TableVariationTest(unittest.TestCase):
    """不同作者建的表，形状不一样。这三条是"结构差异真的会让数据变少"的地方。"""

    Q = "https://pan.quark.cn/s/a1"
    B = "https://pan.baidu.com/s/1b2"
    X = "https://pan.xunlei.com/s/c3"

    def test_big_title_row_is_not_header(self):
        """首行是「2026年11月资源合集」这种大标题，不能整行吃掉。

        它含「资源」两个字，会被 `_header_kind` 认成标题列。原来的判据是
        「认得 ≥2 列 **或** 有标题列」，单个标题词就成立，于是这一行凭空消失。
        """
        rows = [
            ["2026年11月资源合集"],
            ["资源名称", "网盘链接", "提取码"],
            ["三体", self.Q, "1234"],
        ]
        result = intake.parse_rows(rows)
        self.assertEqual(result.rows, 3)      # 大标题行也要算进来
        self.assertEqual(result.resources, 1)

    def test_later_batch_does_not_treat_data_as_header(self):
        """分批导入时，第二批的首行是数据行，不许当表头。

        `docs_watch` 每 5000 行切一批，原来每批都独立自动检测表头。
        某行标题恰好写成「资源链接大全」，它会被认成表头然后整行丢掉——
        每多一批就可能多吃一行，38 万行的表能吃几十条。
        """
        rows = [["名称", "链接"]] + [["片名", self.Q]] * 3 + [["资源链接大全", self.B]]
        batches = list(intake.iter_row_batches(rows, size=4))
        self.assertEqual([head for head, _ in batches], [True, False])

        total = 0
        for head, chunk in batches:
            total += intake.parse_rows(
                chunk, has_header=None if head else False
            ).resources
        self.assertEqual(total, 4)

    def test_code_column_tolerates_no_password_markers(self):
        """码列里大量「无」不该把整列拖到阈值以下。

        真实收集表里"没有提取码"常常写「无」，原来这些值算进分母，
        7 行里 3 个码 + 4 个无 → 0.43，整列码全部丢掉。链接和标题都在，
        只有码没了——这种残缺最难被发现。
        """
        rows = [
            [self.Q, "a", "1111"],
            [self.B, "b", "2222"],
            [self.X, "c", "3333"],
            [self.Q, "d", "无"],
            [self.B, "e", "无"],
            [self.X, "f", "无"],
            [self.Q, "g", "无"],
        ]
        result = intake.parse_rows(rows, has_header=False)
        self.assertEqual([i["sharePwd"] for i in result.items], ["1111", "2222", "3333", "", "", "", ""])


class ExplicitColumnTest(unittest.TestCase):
    """人工指定列号。自动识别猜错时，这是唯一的确定性出口。"""

    Q = "https://pan.quark.cn/s/a1"
    B = "https://pan.baidu.com/s/1b2"

    def test_odd_shape_table(self):
        """表头用「影片」「下载」这类词，自动识别认不出标题列和链接列。

        真实表里就有这么写的。认不出的后果不是报错，是 `_row_title` 退回
        "取行内最长的像标题的格子"——备注写得比标题长，标题就成了备注。
        指定列号才能拿到真标题。
        """
        rows = [
            ["序号", "分类", "影片", "备注", "下载", "码"],
            ["1", "影视", "三体", "4K HDR 杜比视界内封中字超高清合集", self.Q, "ab12"],
            ["2", "剧集", "狂飙", "全 39 集 4K 内封字幕版 附花絮", self.B, "cd34"],
        ]
        auto = intake.parse_rows(rows)
        self.assertEqual(
            [i["title"] for i in auto.items],
            ["4K HDR 杜比视界内封中字超高清合集", "全 39 集 4K 内封字幕版 附花絮"],
        )

        fixed = intake.parse_rows(rows, title_col=3, link_col=5, code_col=6)
        self.assertEqual([i["title"] for i in fixed.items], ["三体", "狂飙"])
        self.assertEqual([i["sharePwd"] for i in fixed.items], ["ab12", "cd34"])
        self.assertEqual([i["shareUrl"] for i in fixed.items], [self.Q, self.B])

    def test_link_column_excludes_other_columns(self):
        """指定了链接列，别的列里的链接不该被一起收进来。

        "备用链接"列是常见结构。不指定列时整行扫描会把备用链接也入库，
        同一行产出两条资源、标题还一样，看起来像重复数据。
        """
        rows = [
            ["资源名称", "主链接", "备用链接"],
            ["三体全集", self.Q, self.B],
        ]
        auto = intake.parse_rows(rows)
        self.assertEqual(auto.resources, 2)

        fixed = intake.parse_rows(rows, title_col=1, link_col=2)
        self.assertEqual(fixed.resources, 1)
        self.assertEqual(fixed.items[0]["shareUrl"], self.Q)

    def test_wrong_column_is_reported_not_silent(self):
        """列号写错（或作者插了一列）时，必须报出来。

        静默的下场比报错糟得多：链接列指到了备注列，整表 0 条入库，
        而报表只写"读到 5000 行、新增 0"，看不出是配置错了。
        """
        rows = [["名称", "备注", "链接"], ["三体", "高清", self.Q]]
        result = intake.parse_rows(rows, title_col=1, link_col=2)
        self.assertEqual(result.resources, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("第 2 列", result.skipped[0][1])

    def test_columns_are_one_based(self):
        """列号跟人在表格里数的一致，第一列是 1 不是 0。"""
        rows = [["三体全集", self.Q], ["狂飙", self.B]]
        result = intake.parse_rows(rows, has_header=False, title_col=1, link_col=2)
        self.assertEqual([i["title"] for i in result.items], ["三体全集", "狂飙"])

    def test_zero_means_auto(self):
        rows = [["名称", "链接"], ["三体", self.Q]]
        auto = intake.parse_rows(rows)
        explicit_zero = intake.parse_rows(rows, title_col=0, link_col=0, code_col=0)
        self.assertEqual([i["title"] for i in auto.items],
                         [i["title"] for i in explicit_zero.items])


class CheckLinkColumnTest(unittest.TestCase):
    """登记时校验人工指定的链接列。指错了要在登记这一刻就拦下来。"""

    Q = "https://pan.quark.cn/s/a1"

    def test_good_column_passes(self):
        rows = [["三体", self.Q], ["狂飙", "https://pan.baidu.com/s/1b2"]]
        self.assertEqual(intake.check_link_column(rows, 2), "")

    def test_wrong_column_warns(self):
        rows = [["三体", "高清 4K", self.Q], ["狂飙", "1080P", "https://pan.baidu.com/s/1b2"]]
        warn = intake.check_link_column(rows, 2)
        self.assertIn("只有 0/2", warn)

    def test_empty_column_is_failure_not_unknown(self):
        """这一列全空 = 指到了不存在的列，不是"没数据所以判断不了"。

        原来 `if total and ...` 把总数为 0 的情形直接放过了，
        结果 `--set-cols 2 --link-col 3` 在只有两列的表上被当成"没问题"，
        配置写进库，定时任务每天导入 0 条。
        """
        rows = [["三体", self.Q], ["狂飙", "https://pan.baidu.com/s/1b2"]]
        warn = intake.check_link_column(rows, 3)
        self.assertNotEqual(warn, "")
        self.assertIn("全是空", warn)

    def test_zero_means_not_configured(self):
        self.assertEqual(intake.check_link_column([["x"]], 0), "")


class ProbeColumnsTest(unittest.TestCase):
    def test_reports_kinds_and_samples(self):
        """探测结果要带样本值，人才能核对列号对不对。"""
        rows = [
            ["序号", "资源名称", "下载地址", "提取码"],
            ["1", "三体全集", "https://pan.quark.cn/s/a1", "ab12"],
            ["2", "狂飙", "https://pan.baidu.com/s/1b2", "cd34"],
        ]
        report = intake.probe_columns(rows)
        kinds = {item["col"]: item["kind"] for item in report}
        self.assertEqual(kinds[1], "other")     # 序号
        self.assertEqual(kinds[2], "title")
        self.assertEqual(kinds[3], "link")
        self.assertEqual(kinds[4], "code")
        link = next(i for i in report if i["col"] == 3)
        self.assertIn("pan.quark.cn", link["samples"][0])
        self.assertEqual(link["filled"], 2)

    def test_no_header_still_probes(self):
        """没有表头也要能探测，不然新表第一次登记就抓瞎。"""
        rows = [
            ["三体全集", "https://pan.quark.cn/s/a1"],
            ["狂飙", "https://pan.baidu.com/s/1b2"],
        ]
        report = intake.probe_columns(rows)
        kinds = {item["col"]: item["kind"] for item in report}
        self.assertEqual(kinds[2], "link")


class HeaderTest(unittest.TestCase):
    def test_header_detected(self):
        rows = [["片名", "夸克链接", "提取码"], ["三体", "https://pan.quark.cn/s/a1", "8k2m"]]
        result = intake.parse_rows(rows)
        self.assertEqual(result.rows, 1)          # 表头那行不算数据
        self.assertEqual(result.resources, 1)

    def test_no_header_when_only_links(self):
        """一列全是链接时不能把第一行当表头——那会把一条资源吃掉。"""
        rows = [["https://pan.quark.cn/s/a1"], ["https://pan.quark.cn/s/a2"]]
        result = intake.parse_rows(rows)
        self.assertEqual(result.rows, 2)
        self.assertEqual(result.resources, 2)

    def test_code_column_header_wins(self):
        rows = [["标题", "地址", "密码"], ["繁花", "https://pan.quark.cn/s/b1", "z9x1"]]
        result = intake.parse_rows(rows)
        self.assertEqual(result.items[0]["sharePwd"], "z9x1")


class CodeColumnTest(unittest.TestCase):
    def test_code_column_without_header(self):
        """没有表头也要认出"整列都是 4 位码"的那一列。"""
        rows = [
            ["https://pan.quark.cn/s/a1", "1111"],
            ["https://pan.quark.cn/s/a2", "2222"],
            ["https://pan.quark.cn/s/a3", "3333"],
        ]
        result = intake.parse_rows(rows, has_header=False)
        self.assertEqual([i["sharePwd"] for i in result.items], ["1111", "2222", "3333"])

    def test_inline_code_wins_over_column(self):
        """链接旁边写了码，以它为准；独立列只是兜底。"""
        rows = [
            ["标题", "链接", "提取码"],
            ["三体", "https://pan.quark.cn/s/a1 提取码 8k2m", "zzzz"],
        ]
        result = intake.parse_rows(rows)
        self.assertEqual(result.items[0]["sharePwd"], "8k2m")


class MultiLinkRowTest(unittest.TestCase):
    def test_two_links_share_title_each_own_code(self):
        rows = [
            ["资源", "夸克", "百度"],
            ["流浪地球2 https://pan.quark.cn/s/q1 提取码 1111 "
             "https://pan.baidu.com/s/1q2 提取码 2222", "", ""],
        ]
        result = intake.parse_rows(rows)
        self.assertEqual(result.resources, 2)
        self.assertEqual({i["title"] for i in result.items}, {"流浪地球2"})
        by_platform = {i["platform"]: i["sharePwd"] for i in result.items}
        self.assertEqual(by_platform, {"quark": "1111", "baidu": "2222"})

    def test_one_code_for_two_links(self):
        """只有一个码时，归给就近的那条，另一条留空——不许瞎配。"""
        rows = [
            ["标题", "链接"],
            ["三体", "https://pan.quark.cn/s/q1 提取码 1111 https://pan.baidu.com/s/1q2"],
        ]
        result = intake.parse_rows(rows)
        codes = {i["platform"]: i["sharePwd"] for i in result.items}
        self.assertEqual(codes["quark"], "1111")
        self.assertEqual(codes["baidu"], "")


class TitleTest(unittest.TestCase):
    def test_whole_row_is_share_text(self):
        """整行一段文案：标题必须从文案里洗出来，不能给占位。"""
        rows = [["三体 全集 https://pan.quark.cn/s/abc123 提取码: 1234"]]
        result = intake.parse_rows(rows, has_header=False)
        self.assertEqual(result.items[0]["title"], "三体 全集")
        self.assertEqual(result.items[0]["sharePwd"], "1234")

    def test_marketing_suffix_removed(self):
        rows = [["繁花 4K【更多资源关注公众号xx】 https://pan.quark.cn/s/b1"]]
        result = intake.parse_rows(rows, has_header=False)
        self.assertEqual(result.items[0]["title"], "繁花 4K")

    def test_untitled_counted_and_placeholder_used(self):
        """只有链接没有标题：入库但计数，让人知道要去补。"""
        rows = [["https://pan.quark.cn/s/onlylink"]]
        result = intake.parse_rows(rows, has_header=False)
        self.assertEqual(result.untitled, 1)
        self.assertIn("分享", result.items[0]["title"])


class GateTest(unittest.TestCase):
    def test_detail_page_rejected(self):
        """详情页链接连提取都提不出来，直接算"没有网盘链接"。"""
        rows = [["某资源 https://pan.quark.cn/list/share/abc"]]
        result = intake.parse_rows(rows, has_header=False)
        self.assertEqual(result.resources, 0)
        self.assertTrue(result.skipped)

    def test_123_and_tianyi_are_kept(self):
        """123 / 天翼没有转存适配器，但必须入库。

        口径 2026-11 改过。原来挡在这里的理由是"没有 adapter，存进去也点不开"，
        但 `/go` 本来就有回退原链接这条路（带提取码一起回退），用户照样能拿到
        文件；挡在入口的代价是库里这几类永远是 0，搜索永远缺一大块，而且
        123 恰恰是短剧/合集类资源发布最集中的地方。
        """
        rows = [
            ["标题", "链接"],
            ["三体", "https://www.123pan.com/s/abcd-efgh"],
            ["繁花", "https://cloud.189.cn/t/AbCdEfGh（访问码：3f2a）"],
        ]
        result = intake.parse_rows(rows)
        self.assertEqual(result.resources, 2)
        by_title = {it["title"]: it for it in result.items}
        self.assertEqual(by_title["三体"]["platform"], "123")
        self.assertEqual(by_title["繁花"]["platform"], "tianyi")
        # 码要留下，链接尾巴要干净——两个都得对，否则回退过去是 404
        self.assertEqual(by_title["繁花"]["sharePwd"], "3f2a")
        self.assertEqual(by_title["繁花"]["shareUrl"], "https://cloud.189.cn/t/AbCdEfGh")

    def test_row_without_link_skipped(self):
        rows = [["标题", "链接"], ["三体", ""], ["繁花", "https://pan.quark.cn/s/b1"]]
        result = intake.parse_rows(rows)
        self.assertEqual(result.resources, 1)
        self.assertEqual(result.skipped[0][0], 2)   # 行号指回表格里的实际行

    def test_blank_rows_ignored(self):
        rows = [["标题", "链接"], ["", ""], ["三体", "https://pan.quark.cn/s/a1"]]
        result = intake.parse_rows(rows)
        self.assertEqual(result.rows, 1)

    def test_duplicate_link_in_same_row_collapses(self):
        rows = [["标题", "链接"], ["三体", "https://pan.quark.cn/s/a1 https://pan.quark.cn/s/a1"]]
        result = intake.parse_rows(rows)
        self.assertEqual(result.resources, 1)


class SourceTest(unittest.TestCase):
    def test_source_name_and_kind(self):
        rows = [["标题", "链接"], ["三体", "https://pan.quark.cn/s/a1"]]
        item = intake.parse_rows(rows, source_name="doc:测试表").items[0]
        self.assertEqual(item["sourceName"], "doc:测试表")
        self.assertEqual(item["sourceKind"], "doc")

    def test_parse_text_multiline(self):
        text = "三体 https://pan.quark.cn/s/a1 提取码 1111\n繁花 https://pan.quark.cn/s/b1"
        result = intake.parse_text(text)
        self.assertEqual(result.resources, 2)
        self.assertEqual(result.items[1]["sharePwd"], "")


if __name__ == "__main__":
    unittest.main()
