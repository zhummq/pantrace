"""`sdk/urls.py` 的平台识别回归。

这份名单以前等于"我们能转存的盘"，123 / 天翼被排除在外面。后果不是报错，
是 `parse().ok` 为 False → `store._prepare` 直接 `continue`：**静默丢数据**，
只有对着覆盖率报表才会发现"库里就是没有 123 的资源"，很难归因。

两个必须钉住的点：

1. 123 的短域名在轮换（123pan / 123684 / 123865 / 123912 / 123592），
   只写主域名会让别的域名下的**同一个分享**变成 unknown。
2. 天翼 `/web/share?code=xxx` 里的 `code` 是分享 id，不是提取码。
   `PASSCODE_RE` 里恰好也有 `code=`，一不小心就把 `Qn2mYv` 截成 `Qn2m` 当密码发出去。

2026-11 又补了 115 与 PikPak：两家都是"清洗层能提取、解析层不认"，
表现同样是静默丢数据。115 的 `115cdn.com` / `anxia.com` 是同一站点的镜像，
必须落到同一个 `pwd_id`，否则同一条资源会以两个 `dedup_key` 各存一份。

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


class TestPan115(unittest.TestCase):
    """115 的三个域名指向同一套分享 id，镜像站必须落到同一个 pwd_id。

    少认一个镜像域名，同一条资源就会以两个 `dedup_key` 各存一份：搜索结果里
    出现两条一模一样的记录，热度也被劈成两半。
    """

    def test_main_domain(self) -> None:
        p = parse("https://115.com/s/swabcdef?password=1234")
        self.assertEqual(p.platform, "115")
        self.assertEqual(p.pwd_id, "swabcdef")
        self.assertEqual(p.passcode, "1234")
        self.assertTrue(p.ok)

    def test_mirror_domains_share_id(self) -> None:
        ids = {
            parse(f"https://{host}/s/swabcdef").pwd_id
            for host in ("115.com", "www.115.com", "115cdn.com", "anxia.com")
        }
        self.assertEqual(ids, {"swabcdef"})

    def test_access_code_glued_to_url(self) -> None:
        # 微信文案：码粘在链接后面，且没有 ?password=
        p = parse("https://115.com/s/swabcdef（访问码：1a2b）")
        self.assertEqual(p.pwd_id, "swabcdef")
        self.assertEqual(p.passcode, "1a2b")
        self.assertNotIn("（", p.url)

    def test_id_allows_hyphen(self) -> None:
        p = parse("https://115.com/s/swabc-def")
        self.assertEqual(p.pwd_id, "swabc-def")

    def test_homepage_stays_unknown(self) -> None:
        self.assertFalse(parse("https://115.com/").ok)


class TestPikPak(unittest.TestCase):
    def test_share_link(self) -> None:
        p = parse("https://mypikpak.com/s/VOabc123")
        self.assertEqual(p.platform, "pikpak")
        self.assertEqual(p.pwd_id, "VOabc123")
        self.assertTrue(p.ok)

    def test_www_prefix(self) -> None:
        self.assertEqual(parse("https://www.mypikpak.com/s/VOabc_-123").pwd_id, "VOabc_-123")

    def test_password_in_query(self) -> None:
        p = parse("https://mypikpak.com/s/VOabc123?pwd=9z8y")
        self.assertEqual(p.pwd_id, "VOabc123")
        self.assertEqual(p.passcode, "9z8y")

    def test_homepage_stays_unknown(self) -> None:
        self.assertFalse(parse("https://mypikpak.com/").ok)


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
