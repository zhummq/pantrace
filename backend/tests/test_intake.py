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

    def test_extracted_but_not_transferable(self):
        """能提取但不在转存白名单的盘（123/天翼）也挡住。

        这不是 bug，是口径：库里的链接最终要走 `/go` 转存，没有适配器的平台
        存进去也点不开。挡在这里，比让用户点了报错好。
        """
        rows = [["标题", "链接"], ["三体", "https://www.123pan.com/s/abcd-efgh"]]
        result = intake.parse_rows(rows)
        self.assertEqual(result.resources, 0)
        self.assertTrue(any("识别不出" in reason for _, reason in result.skipped))

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
