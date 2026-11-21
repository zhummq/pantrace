"""QQ 群消息解析的离线测试。

群里发的分享和表格导入长得不一样，回归点也不同。每条都对应一个
"写错了很难归因"的坑：

1. **标题独占一行**。群里最常见就是「片名一行、链接一行」，不跨行找标题
   的话整批都是占位标题。
2. **标题不能越界**。不分段的话上一段的标题会漏给下一段的链接，落库后是
   "点开《三体》是《活着》"——搜得到、点开错，最难查的一类脏数据。
3. **元信息行不许顶掉片名**。「片名 / 导演 / 年份」三行连着写时，取错一行
   整条资源就查不到了。
4. **提取码单独一行**。这个形态占群里分享的八成，漏了等于资源全无码。
5. **码不跨链接**。`q1 提取码1111 百度` 这种，配错码比不给码更糟——
   用户拿去转存会得到"提取码错误"。
6. **平台名不是标题**。多盘备份时每行前面写着「夸克 / 百度」，被当成标题
   就会出现三条叫「夸克」「百度」的垃圾记录。

跑法：``.venv/Scripts/python.exe -m unittest tests.test_qq_intake -v``
不打网络：纯字符串处理。
"""

from __future__ import annotations

import unittest

from app import qq_intake

Q = "https://pan.quark.cn/s/a1b2c3"
B = "https://pan.baidu.com/s/1d2e3f"


class TitleAcrossLinesTest(unittest.TestCase):
    """标题和链接不在同一行时，标题归谁。"""

    def test_title_line_before_link(self):
        """片名独占一行，链接在下一行——群里最标准的写法。"""
        got = qq_intake.parse_message(f"【4K修复】漫长的季节 2023\n链接：{Q}\n提取码：k9m2")
        self.assertEqual(len(got.items), 1)
        self.assertEqual(got.items[0]["title"], "【4K修复】漫长的季节 2023")
        self.assertEqual(got.items[0]["sharePwd"], "k9m2")

    def test_meta_lines_do_not_steal_title(self):
        """「导演：辛爽」比片名短，不能顶掉它。

        两条标题行连着出现时，判据是"谁更长"。按"后出现的赢"来写，
        这里的标题会变成「导演：辛爽」——一样能入库、一样能搜到链接，
        只是搜不到片名，很难被发现。
        """
        text = f"漫长的季节\n导演：辛爽\n年份：2023\n{Q}"
        got = qq_intake.parse_message(text)
        self.assertEqual(got.items[0]["title"], "漫长的季节")

    def test_blank_line_resets_title(self):
        """空行分段后，上一段的标题不许漏给下一段。

        不分段的表现：三体 的标题配给了 活着 的链接。库里去重键是
        `platform:pwd_id`，标题错了它照样是"有效数据"，查不出来。
        """
        text = f"三体 全集\n{Q}\n\n活着\n{B}"
        got = qq_intake.parse_message(text)
        self.assertEqual([i["title"] for i in got.items], ["三体 全集", "活着"])

    def test_title_switches_after_use(self):
        """同一个段落里连着两条资源，第二条的标题要能接手。

        段内标题是复用的（一条标题挂多个盘），所以"用过一次"才是换人的信号。
        这条不成立的话，第二条资源的标题会一直停在上一条。
        """
        text = f"三体 全集\n{Q}\n活着\n{B}"
        got = qq_intake.parse_message(text)
        self.assertEqual([i["title"] for i in got.items], ["三体 全集", "活着"])


class TitleNoiseTest(unittest.TestCase):
    """什么不算标题。"""

    def test_platform_name_is_not_title(self):
        """「夸克 https://…」「百度 https://…」里的平台名是盘标，不是资源名。

        多盘备份的标准写法。判错的话会得到三条分别叫「夸克」「百度」的记录，
        而且总标题一次都用不上。
        """
        text = f"三体 全集\n夸克 {Q}\n百度 {B} 提取码 8888"
        got = qq_intake.parse_message(text)
        self.assertEqual([i["title"] for i in got.items], ["三体 全集", "三体 全集"])

    def test_code_line_is_not_title(self):
        """「提取码：abcd」单独一行，剥完只剩四个字母，不是标题。"""
        text = f"三体 全集\n{Q}\n提取码：ab12"
        got = qq_intake.parse_message(text)
        self.assertEqual(got.items[0]["title"], "三体 全集")
        self.assertEqual(got.items[0]["sharePwd"], "ab12")

    def test_inline_title_wins(self):
        """链接行自带标题时用它，不用上一行的。"""
        text = f"合集\n三体 第一季 {Q}\n活着 {B}"
        got = qq_intake.parse_message(text)
        self.assertEqual([i["title"] for i in got.items], ["三体 第一季", "活着"])

    def test_ad_tail_stripped(self):
        """【更多资源关注公众号】这类尾巴不进标题。"""
        text = f"三体 全集 {Q} 【更多资源关注公众号：xx影视】"
        got = qq_intake.parse_message(text)
        self.assertEqual(got.items[0]["title"], "三体 全集")


class CodeAssignmentTest(unittest.TestCase):
    """提取码归属。配错比不配更糟——用户拿去转存会看到"提取码错误"。"""

    def test_code_on_its_own_line(self):
        """码单独一行，也要配给上面那条链接。"""
        got = qq_intake.parse_message(f"{Q}\n提取码：7x2k")
        self.assertEqual(got.items[0]["sharePwd"], "7x2k")

    def test_code_does_not_cross_links(self):
        """一码一链，靠得近的拿。

        `Q 提取码1111 B` 里 1111 紧贴 Q，不能同时配给 B。
        """
        got = qq_intake.parse_message(f"{Q} 提取码 1111\n{B}")
        by_url = {i["shareUrl"]: i["sharePwd"] for i in got.items}
        self.assertEqual(by_url[Q], "1111")
        self.assertEqual(by_url[B], "")

    def test_two_codes_two_links(self):
        """两条链接各带一个码，不能串。"""
        text = f"第一集 {Q} 提取码 aa11\n第二集 {B} 提取码 bb22"
        got = qq_intake.parse_message(text)
        by_url = {i["shareUrl"]: i["sharePwd"] for i in got.items}
        self.assertEqual(by_url[Q], "aa11")
        self.assertEqual(by_url[B], "bb22")

    def test_similar_ids_not_merged(self):
        """`/s/xyz` 和 `/s/xyz2` 是两条资源，不能因为前缀像就被判成重复。

        `clean.extract_net_disk_links` 的夸克段用「互相包含」判重（为了合并
        同一条链接的不同尾巴），跨行拿它当入库清单会把第二条连同提取码丢掉。
        """
        text = "第一集 https://pan.quark.cn/s/xyz\n第二集 https://pan.quark.cn/s/xyz2\n提取码：ab12"
        got = qq_intake.parse_message(text)
        self.assertEqual(len(got.items), 2)
        self.assertEqual({i["shareUrl"] for i in got.items},
                         {"https://pan.quark.cn/s/xyz", "https://pan.quark.cn/s/xyz2"})


class FilteringTest(unittest.TestCase):
    """不该入库的东西。"""

    def test_no_link(self):
        got = qq_intake.parse_message("今天天气不错啊")
        self.assertEqual(got.items, [])
        self.assertIn("没有网盘链接", got.skipped)

    def test_empty(self):
        self.assertEqual(qq_intake.parse_message("   ").items, [])

    def test_detail_page_skipped(self):
        """认不出分享 id 的链接（详情页、站内页）不入库。"""
        got = qq_intake.parse_message("三体 https://pan.quark.cn/list/share")
        self.assertEqual(got.items, [])

    def test_cq_code_stripped(self):
        """CQ 码不能污染标题。"""
        got = qq_intake.parse_message(f"[CQ:at,qq=123] 三体 全集\n{Q}")
        self.assertEqual(got.items[0]["title"], "三体 全集")

    def test_source_level_defaults_to_one(self):
        """群主/管理员手动发的，来源等级 1（和自建 TG 频道同级）。"""
        got = qq_intake.parse_message(f"三体 {Q}", source_name="资源群")
        self.assertEqual(got.items[0]["sourceLevel"], 1)
        self.assertEqual(got.items[0]["sourceKind"], "qq")
        self.assertEqual(got.items[0]["sourceName"], "资源群")


class BatchTest(unittest.TestCase):
    """批量入口：丢弃原因要能对上消息 id，否则不知道哪条没进来。"""

    def test_dropped_keeps_id(self):
        items, dropped = qq_intake.parse_batch([
            {"id": "m1", "text": f"三体 {Q}", "groupName": "资源群"},
            {"id": "m2", "text": "闲聊一句"},
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual([d["id"] for d in dropped], ["m2"])

    def test_source_falls_back_to_group_id(self):
        """没群名时用「QQ群+群号」，管理端至少能看出是哪个群。"""
        items, _ = qq_intake.parse_batch([{"id": "m", "text": f"三体 {Q}", "groupId": "123456"}])
        self.assertEqual(items[0]["sourceName"], "QQ群123456")


if __name__ == "__main__":
    unittest.main()
