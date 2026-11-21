"""AI 结构化结果的合并与校验。

这里的每一条都不是"模型写得准不准"，而是**模型写错时会不会污染库**。
大模型在采集链路里只有一个可靠职责：把标题写成人话。链接和提取码必须
回到原文里核对，因为这两样写错的后果用户看不见：

1. 链接被"补全"或改写一个字符 → 落库就是死链，用户点开只看到"分享已失效"，
   没人会回头怀疑采集时写坏了。
2. 模型凭印象编一条原文没有的链接 → 库里多出一条永远搜得到、永远打不开的
   资源，还得人工清理。
3. 原文没写提取码，模型按常见规律猜一个 → 用户转存报"提取码错误"，
   比不给码更耽误事。
4. 反过来，模型漏掉的链接**必须补回来**。结构化是为了标题更准，不是让它
   决定哪条资源值得收；漏一条就是彻底搜不到，而日志里没有任何痕迹。

跑法：``.venv/Scripts/python.exe -m unittest tests.test_qq_ai_merge -v``
不打网络：AI 的返回是构造出来的。
"""

from __future__ import annotations

import unittest

from app import qq_intake

Q = "https://pan.quark.cn/s/a1b2c3"
Q2 = "https://pan.quark.cn/s/z9y8x7"
B = "https://pan.baidu.com/s/1d2e3f"

TEXT = (
    "【更多资源关注公众号：xx影视】\n"
    "漫长的季节 2023 4K\n"
    f"链接：{Q}\n"
    "提取码：k9m2\n"
    f"百度备份：{B} 提取码 7x3p"
)


def merge(ai_items, text=TEXT):
    return qq_intake.merge_ai_items(text, ai_items)


class TitleTest(unittest.TestCase):
    """AI 的标题该被采纳，但空标题不能把整条资源变空。"""

    def test_ai_title_wins(self):
        got = merge([{"title": "漫长的季节 2023 4K", "shareUrl": Q, "sharePwd": "k9m2"}])
        titles = {i["shareUrl"]: i["title"] for i in got.items}
        self.assertEqual(titles[Q], "漫长的季节 2023 4K")

    def test_empty_title_falls_back_to_regex(self):
        """模型只吐了链接、标题留空——不能因此得到一条空标题记录。"""
        got = merge([{"title": "   ", "shareUrl": Q, "sharePwd": ""}])
        self.assertTrue(got.items[0]["title"].strip())
        self.assertNotIn(got.items[0]["title"], ("", "链接"))

    def test_title_is_truncated(self):
        got = merge([{"title": "长" * 400, "shareUrl": Q, "sharePwd": ""}])
        self.assertLessEqual(len(got.items[0]["title"]), qq_intake.MAX_TITLE)


class HallucinationTest(unittest.TestCase):
    """模型给的链接必须能在原文里对上，否则整条丢弃。"""

    def test_link_not_in_text_is_dropped(self):
        """id 记错一个字符 = 原文里没有这条链接，绝不能入库。"""
        fake = "https://pan.quark.cn/s/xxxxxx"
        got = merge([{"title": "漫长的季节", "shareUrl": fake, "sharePwd": "k9m2"}])
        urls = [i["shareUrl"] for i in got.items]
        self.assertNotIn(fake, urls)
        # 原文里的两条链接仍然都在（AI 只给了假的，正则补回来）
        self.assertIn(Q, urls)
        self.assertIn(B, urls)

    def test_fake_link_does_not_drop_real_ones(self):
        """模型编了一条，不代表这条消息作废。"""
        got = merge([
            {"title": "漫长的季节", "shareUrl": "https://pan.quark.cn/s/000000", "sharePwd": ""},
            {"title": "漫长的季节", "shareUrl": Q, "sharePwd": "k9m2"},
        ])
        self.assertEqual(len(got.items), 2)

    def test_malformed_url_ignored(self):
        got = merge([{"title": "漫长的季节", "shareUrl": "pan.quark.cn/s/a1b2c3", "sharePwd": ""}])
        self.assertEqual(len(got.items), 2)

    def test_duplicate_rows_collapsed(self):
        """同一链接写两遍（模型常见），只留一条。"""
        got = merge([
            {"title": "漫长的季节", "shareUrl": Q, "sharePwd": "k9m2"},
            {"title": "漫长的季节 4K", "shareUrl": Q, "sharePwd": "k9m2"},
        ])
        self.assertEqual(len([i for i in got.items if i["shareUrl"] == Q]), 1)


class PwdTest(unittest.TestCase):
    """提取码必须在原文里出现过，否则当猜的。"""

    def test_ai_pwd_accepted_when_present(self):
        got = merge([{"title": "漫长的季节", "shareUrl": Q, "sharePwd": "k9m2"}])
        self.assertEqual(got.items[0]["sharePwd"], "k9m2")

    def test_invented_pwd_rejected(self):
        """原文写的是 k9m2，模型给 abcd —— 用规则解析出来的真码。"""
        got = merge([{"title": "漫长的季节", "shareUrl": Q, "sharePwd": "abcd"}])
        self.assertEqual(got.items[0]["sharePwd"], "k9m2")

    def test_pwd_containing_sentence_rejected(self):
        got = merge([{"title": "漫长的季节", "shareUrl": Q, "sharePwd": "提取码是 k9m2"}])
        self.assertEqual(got.items[0]["sharePwd"], "k9m2")


class NoLossTest(unittest.TestCase):
    """模型漏掉的链接必须被正则补回来。"""

    def test_missing_link_still_ingested(self):
        got = merge([{"title": "漫长的季节", "shareUrl": Q, "sharePwd": "k9m2"}])
        urls = [i["shareUrl"] for i in got.items]
        self.assertIn(Q, urls)
        self.assertIn(B, urls)

    def test_empty_ai_result_equals_regex(self):
        """模型返回空数组时结果应与纯规则解析一致，不能少链接。"""
        plain = qq_intake.parse_message(TEXT)
        got = merge([])
        self.assertEqual(
            sorted(i["shareUrl"] for i in got.items),
            sorted(i["shareUrl"] for i in plain.items),
        )

    def test_batch_passes_items_through(self):
        items, dropped = qq_intake.parse_batch([{
            "id": "m1",
            "text": TEXT,
            "groupName": "资源群",
            "items": [{"title": "漫长的季节 2023 4K", "shareUrl": Q, "sharePwd": "k9m2"}],
        }])
        self.assertEqual(len(items), 2)
        self.assertEqual(dropped, [])
        self.assertTrue(all(i["sourceKind"] == "qq" for i in items))
        self.assertIn("漫长的季节 2023 4K", [i["title"] for i in items])

    def test_ai_items_do_not_bypass_source_level(self):
        items, _ = qq_intake.parse_batch([{
            "id": "m1", "text": TEXT, "groupName": "资源群",
            "items": [{"title": "漫长的季节", "shareUrl": Q, "sharePwd": ""}],
        }])
        self.assertEqual(items[0]["sourceLevel"], 1)
        self.assertEqual(items[0]["sourceName"], "资源群")


if __name__ == "__main__":
    unittest.main()
