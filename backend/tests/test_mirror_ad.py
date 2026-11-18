"""转存前拦截 + 后台清理的接线测试。

adfilter 的单测管"判据对不对"，这个文件管"接线有没有接错"：mirror 是否真的
在 transfer 之前过滤、全中时会不会给用户一个空分享、清理是否真的不在请求
线程里跑。这三条错了，判据再准也没用。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest discover -s tests -v``
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from app import pan
from app.sdk.base import PanClient
from app.sdk.types import Account, File, ShareInfo, ShareLink

WORDS = ["加群", "公众号", "更多资源"]
MB = 1024 * 1024


def f(name: str, size: int, is_dir: bool = False) -> File:
    return File(fid="fid-" + name, name=name, size=size, is_dir=is_dir)


class FakeClient(PanClient):
    """不发任何网络请求的假网盘，只把 mirror 的流程跑完。"""

    type = "fake"
    display = "假盘"

    def __init__(self, files, *, keywords=None, max_bytes=2 * MB):
        # 不走 PanClient.__init__：那里面要建 Requester
        self.account = Account(id=1, type="fake")
        self.timeout = 5.0
        self.on_cookie = None
        self.max_file_bytes = 0
        self.ad_keywords = list(WORDS if keywords is None else keywords)
        self.ad_max_bytes = max_bytes
        self._files = files
        self.seen: ShareInfo | None = None

    def share_info(self, url: str, pwd: str = "") -> ShareInfo:
        return ShareInfo(
            platform="fake",
            title="测试资源",
            files=list(self._files),
            total_size=sum(x.size for x in self._files),
        )

    def transfer(self, info: ShareInfo, to_dir: str = "") -> list[str]:
        self.seen = info
        return ["new-fid"]

    def create_share(self, fids, title, expire_hours: int = 24) -> ShareLink:  # noqa: D102
        return ShareLink(url="https://example.com/s/1")

    def list_files(self, parent_id: str = "", page: int = 1, size: int = 50):
        return []

    def delete(self, fids) -> None:  # noqa: D102
        return None


class TestPrefilterInMirror(unittest.TestCase):
    def test_ad_dropped_before_transfer(self):
        client = FakeClient(
            [f("正片.mp4", 800 * MB), f("加群领福利.txt", 3 * 1024), f("封面.jpg", 200 * 1024)]
        )
        client.mirror("https://example.com/s/x")

        # transfer 拿到的是过滤后的清单
        self.assertEqual([x.name for x in client.seen.files], ["正片.mp4", "封面.jpg"])
        # 被剔除的 fid 单独留给夸克做 exclude_fids
        self.assertEqual(client.seen.excluded, ["fid-加群领福利.txt"])
        # 体积跟着减，别把广告的体积算进阈值判断和容量统计
        self.assertEqual(client.seen.total_size, 800 * MB + 200 * 1024)

    def test_nothing_hit_untouched(self):
        client = FakeClient([f("正片.mp4", 800 * MB), f("说明.txt", 1024)])
        client.mirror("https://example.com/s/x")
        self.assertEqual(len(client.seen.files), 2)
        self.assertEqual(client.seen.excluded, [])

    def test_all_hit_falls_back_to_full_transfer(self):
        """顶层全中：宁可把广告一起转进去，也不给用户一个空分享。"""
        client = FakeClient(
            [f("加群领福利.txt", 1024), f("更多资源.jpg", 300 * 1024)]
        )
        client.mirror("https://example.com/s/x")
        self.assertEqual(len(client.seen.files), 2)
        self.assertEqual(client.seen.excluded, [])

    def test_disabled_when_no_keywords(self):
        client = FakeClient([f("加群领福利.txt", 1024)], keywords=[])
        client.mirror("https://example.com/s/x")
        self.assertEqual(len(client.seen.files), 1)

    def test_directory_never_dropped(self):
        client = FakeClient(
            [f("加群资源", 0, is_dir=True), f("正片.mp4", 800 * MB)]
        )
        client.mirror("https://example.com/s/x")
        self.assertEqual([x.name for x in client.seen.files], ["加群资源", "正片.mp4"])


class TestScrubIsOffTheRequestPath(unittest.TestCase):
    """原则二：清理一个字节都不能占用户从点击到拿到链接的时间。"""

    def setUp(self):
        pan.configure(ad_filter=True, ad_keywords="加群,公众号", ad_depth=2, ad_max_mb=2.0)

    def tearDown(self):
        pan.configure(ad_filter=False, ad_keywords="")

    def test_queue_returns_immediately(self):
        done = []

        def slow_client(account):
            time.sleep(0.6)
            done.append(time.monotonic())
            raise RuntimeError("假失败，线程里应该被吞掉")

        link = ShareLink(url="https://example.com/s/1", fids=["a", "b"])
        account = Account(id=7, type="fake")
        started = time.monotonic()
        with mock.patch.object(pan, "_client_for", slow_client):
            pan._queue_scrub(account, link)
            elapsed = time.monotonic() - started
        # 排队本身必须远快于清理动作
        self.assertLess(elapsed, 0.1, f"排队花了 {elapsed:.2f}s，清理还在主链路上")

    def test_queue_skips_when_disabled(self):
        pan.configure(ad_filter=False)
        link = ShareLink(url="https://example.com/s/1", fids=["a"])
        with mock.patch.object(pan, "_client_for") as bogus:
            pan._queue_scrub(Account(id=7, type="fake"), link)
        bogus.assert_not_called()

    def test_queue_skips_without_keywords(self):
        pan.configure(ad_filter=True, ad_keywords="  ,  ")
        link = ShareLink(url="https://example.com/s/1", fids=["a"])
        with mock.patch.object(pan, "_client_for") as bogus:
            pan._queue_scrub(Account(id=7, type="fake"), link)
        bogus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
