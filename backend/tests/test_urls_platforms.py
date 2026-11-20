"""`sdk/urls.py` 的平台识别回归。

这份名单以前等于"我们能转存的盘"，123 / 天翼被排除在外面。后果不是报错，
是 `parse().ok` 为 False → `store._prepare` 直接 `continue`：**静默丢数据**，
只有对着覆盖率报表才会发现"库里就是没有 123 的资源"，很难归因。

两个必须钉住的点：

1. 123 的短域名在轮换（123pan / 123684 / 123865 / 123912 / 123592），
   只写主域名会让别的域名下的**同一个分享**变成 unknown。
2. 天翼 `/web/share?code=xxx` 里的 `code` 是分享 id，不是提取码。
   `PASSCODE_RE` 里恰好也有 `code=`，一不小心就把 `Qn2mYv` 截成 `Qn2m` 当密码发出去。

跑法：``.venv/Scripts/python.exe -m unittest tests.test_urls_platforms -v``
"""

from __future__ import annotations

import unittest

from app.sdk.urls import parse


class TestPan123(unittest.TestCase):
    def test_main_domain(self) -> None:
        p = parse("https://www.123pan.com/s/abc-DEF")
        self.assertEqual(p.platform, "123")
        self.assertEqual(p.pwd_id, "abc-DEF")
        self.assertTrue(p.ok)

    def test_html_suffix_is_stripped(self) -> None:
        # 分享页复制的链接常常带 .html，留着会让 share id 变成 abc-DEF.html
        p = parse("https://www.123pan.com/s/abc-DEF.html")
        self.assertEqual(p.pwd_id, "abc-DEF")

    def test_rotating_short_domains(self) -> None:
        # 同一个分享换域名发布是常态，五个域名必须落到同一个 pwd_id 上
        ids = {
            parse(f"https://{host}/s/xyz789").pwd_id
            for host in ("123684.com", "123865.com", "123912.com", "123592.com")
        }
        self.assertEqual(ids, {"xyz789"})

    def test_passcode_in_query(self) -> None:
        p = parse("https://123684.com/s/xyz123?pwd=abcd")
        self.assertEqual(p.passcode, "abcd")

    def test_query_does_not_pollute_id(self) -> None:
        p = parse("https://www.123pan.com/s/abc-DEF?pwd=1234")
        self.assertEqual(p.pwd_id, "abc-DEF")


class TestTianyi(unittest.TestCase):
    def test_short_path(self) -> None:
        p = parse("https://cloud.189.cn/t/AbCdEfGh")
        self.assertEqual(p.platform, "tianyi")
        self.assertEqual(p.pwd_id, "AbCdEfGh")
        self.assertTrue(p.ok)

    def test_access_code_glued_to_url(self) -> None:
        # 微信里复制出来的文案就是长这样，码直接粘在链接屁股后面
        p = parse("https://cloud.189.cn/t/AbCdEfGh（访问码：3f2a）")
        self.assertEqual(p.pwd_id, "AbCdEfGh")
        self.assertEqual(p.passcode, "3f2a")

    def test_web_share_code_is_not_passcode(self) -> None:
        """`code=` 是分享 id。把它当提取码是这套解析最贵的一处误判：
        用户拿着 `Qn2m` 去提取，看到的永远是"提取码错误"。"""
        p = parse("https://cloud.189.cn/web/share?code=Qn2mYv")
        self.assertEqual(p.pwd_id, "Qn2mYv")
        self.assertEqual(p.passcode, "")

    def test_web_share_with_extra_query(self) -> None:
        p = parse("https://cloud.189.cn/web/share?x=1&code=Qn2mYv")
        self.assertEqual(p.pwd_id, "Qn2mYv")
        self.assertEqual(p.passcode, "")

    def test_web_share_explicit_pwd(self) -> None:
        p = parse("https://cloud.189.cn/web/share?code=Qn2mYv&pwd=9a8b")
        self.assertEqual(p.passcode, "9a8b")


class TestStillBlocked(unittest.TestCase):
    """放开不等于全收。详情页、小说站这类"看着像资源、点开不是分享"的还得挡。"""

    def test_detail_page_stays_unknown(self) -> None:
        for url in (
            "https://www.123pan.com/",
            "https://cloud.189.cn/web/share",
            "https://pan.baidu.com/",
            "https://example.com/s/abc",
        ):
            with self.subTest(url=url):
                self.assertFalse(parse(url).ok, url)


if __name__ == "__main__":
    unittest.main()
