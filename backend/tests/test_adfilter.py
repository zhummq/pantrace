"""转存前广告过滤的离线测试。

这个模块的回归点只有一个：**不能把正片判成广告**。所以每条判据都要有一条
"差一点就误杀"的反例占着，改规则时先看这些用例过不过。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest discover -s tests -v``
"""

from __future__ import annotations

import unittest

from app.sdk import adfilter
from app.sdk.types import File

WORDS = ["公众号", "加群", "领福利", "更多资源", "微信"]


def f(name: str, size: int = 1024, is_dir: bool = False) -> File:
    return File(fid="fid-" + name, name=name, size=size, is_dir=is_dir)


class TestTripleCondition(unittest.TestCase):
    """三个条件必须同时满足，缺一个就放过。"""

    def test_typical_ad(self):
        self.assertTrue(adfilter.is_ad(f("加群领福利.txt"), WORDS))
        # 图片要求主干正好等于拦截词
        self.assertTrue(adfilter.is_ad(f("更多资源.jpg", 300 * 1024), WORDS))
        self.assertTrue(adfilter.is_ad(f("公众号.png", 800 * 1024), WORDS))

    def test_wechat_default_image_name(self):
        """微信保存的图片默认叫「微信图片_20240101.jpg」，命中"微信"但不是广告。

        图集资源里全是这种名字，只用"包含"判断能一次删掉用户几百张图。
        """
        for name in (
            "微信图片_20240101114514.jpg",
            "微信图片_20240101114515.png",
            "公众号摄影图集.jpg",
            "更多资源汇总.jpg",
        ):
            with self.subTest(name=name):
                self.assertFalse(adfilter.is_ad(f(name, 900 * 1024), WORDS))

    def test_keyword_but_wrong_suffix(self):
        # 正片名里带"公众号"是常态，后缀不在白名单就不动
        for name in (
            "某某电影[更多资源见公众号].mp4",
            "课程合集-加群获取.zip",
            "学习资料-微信整理.pdf",
            "讲义-公众号版.docx",
            "压缩包-领福利.rar",
        ):
            with self.subTest(name=name):
                self.assertFalse(adfilter.is_ad(f(name, 900 * 1024 * 1024), WORDS))

    def test_right_suffix_but_no_keyword(self):
        # 干净的 txt/图片是真资源，不能动
        for name in ("说明.txt", "封面.jpg", "readme.md"):
            with self.subTest(name=name):
                self.assertFalse(adfilter.is_ad(f(name), WORDS))

    def test_too_big(self):
        # 图集资源里的大图：后缀和名字都对，但体积过了阈值就不判
        self.assertFalse(
            adfilter.is_ad(f("公众号摄影图集.jpg", 3 * 1024 * 1024), WORDS)
        )

    def test_unknown_size(self):
        # 体积读不到 → 信息不全 → 放过
        self.assertFalse(adfilter.is_ad(f("加群领福利.txt", 0), WORDS))

    def test_directory_never(self):
        # 目录一律不判。删目录会连里面的正片一起没
        self.assertFalse(
            adfilter.is_ad(f("更多资源", 1024, is_dir=True), WORDS)
        )
        self.assertFalse(adfilter.is_ad(f("加群", 0, is_dir=True), WORDS))

    def test_empty_name(self):
        self.assertFalse(adfilter.is_ad(File(fid="x", name="", size=10), WORDS))

    def test_no_keywords(self):
        self.assertFalse(adfilter.is_ad(f("加群领福利.txt"), []))


class TestKeywordMatch(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertTrue(adfilter.hit_keyword("加群领福利.TXT", WORDS))

    def test_only_name_not_path(self):
        # 入参只有名字，没有路径——这是刻意的：路径里带上层目录名会误杀
        self.assertFalse(adfilter.hit_keyword("第01集.mp4", WORDS))


class TestSuffix(unittest.TestCase):
    def test_suffix(self):
        self.assertEqual(adfilter.suffix_of("a.TXT"), ".txt")
        self.assertEqual(adfilter.suffix_of("a.tar.gz"), ".gz")
        self.assertEqual(adfilter.suffix_of("无后缀"), "")
        self.assertEqual(adfilter.suffix_of(".gitignore"), "")
        self.assertEqual(adfilter.suffix_of("结尾有点."), "")


class TestSplit(unittest.TestCase):
    def test_keeps_order_and_separates(self):
        files = [
            f("正片.mp4", 800 * 1024 * 1024),
            f("加群领福利.txt"),
            f("资源目录", 0, is_dir=True),
            f("更多资源.jpg", 200 * 1024),
            f("说明.txt"),
        ]
        kept, dropped = adfilter.split(files, WORDS)
        self.assertEqual([x.name for x in dropped], ["加群领福利.txt", "更多资源.jpg"])
        self.assertEqual(
            [x.name for x in kept], ["正片.mp4", "资源目录", "说明.txt"]
        )

    def test_no_hits(self):
        files = [f("正片.mp4", 100), f("封面.jpg", 10)]
        kept, dropped = adfilter.split(files, WORDS)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_custom_threshold(self):
        files = [f("加群领福利.txt", 3 * 1024 * 1024)]
        self.assertEqual(adfilter.split(files, WORDS)[1], [])
        # 阈值放宽到 10MB 后才判
        self.assertEqual(len(adfilter.split(files, WORDS, 10 * 1024 * 1024)[1]), 1)


if __name__ == "__main__":
    unittest.main()
