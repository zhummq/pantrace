"""链接存活检测的离线测试。

只测"判定映射"和"出口过滤"两件事——真正该盯的回归点：
1. 错误码被归成哪一态。判成 bad 会让结果从页面消失，误杀的代价最大。
2. 网络/未知异常必须落到 uncertain，不能是 bad。
3. annotate 不能就地改传入的 item（那是缓存里的对象）。

跑法：``.venv/Scripts/python.exe -m unittest discover -s tests -v``
不打真实网络：所有请求由 FakeSession 接管。
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app import check


class FakeResponse:
    def __init__(self, payload, status: int = 200, text: str = "", location: str = ""):
        self._payload = payload
        self.status_code = status
        # 走 HTML 判定的分支（百度 share/init）要用到正文和 Location
        self.text = text
        self.headers = {"location": location} if location else {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """按 URL 关键字路由的假会话，记录调用次数。"""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url))
        for needle, payload in self.routes.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(payload)
        raise AssertionError(f"未预期的请求：{method} {url}")


def quark_routes(token: dict, detail: dict | None = None) -> dict:
    routes: dict[str, object] = {"/share/sharepage/token": token}
    if detail is not None:
        routes["/share/sharepage/detail"] = detail
    return routes


class BaseCase(unittest.TestCase):
    def setUp(self):
        check._session = None
        check._HOST_INTERVAL = 0.0
        self._tmp = tempfile.TemporaryDirectory()
        check.init(Path(self._tmp.name) / "t.db", enabled=True, hide_bad=True, per_page=20)

    def tearDown(self):
        check._session = None
        self._tmp.cleanup()

    def use(self, routes: dict) -> FakeSession:
        session = FakeSession(routes)
        check._session = session
        return session


class TestQuark(BaseCase):
    def test_ok_when_detail_has_files(self):
        self.use(quark_routes(
            {"code": 0, "data": {"stoken": "st+1/="}},
            {"code": 0, "data": {"list": [{"fid": "1"}]}},
        ))
        state, summary = check.check_one("quark", "https://pan.quark.cn/s/abc123")
        self.assertEqual(state, check.STATE_OK)
        self.assertIn("1", summary)

    def test_cancelled_share_is_bad(self):
        self.use(quark_routes({"code": 41012, "message": "好友已取消了分享"}))
        state, _ = check.check_one("quark", "https://pan.quark.cn/s/abc123")
        self.assertEqual(state, check.STATE_BAD)

    def test_passcode_required_is_locked_not_bad(self):
        self.use(quark_routes({"code": 41008, "message": "需要提取码"}))
        state, _ = check.check_one("quark", "https://pan.quark.cn/s/abc123")
        self.assertEqual(state, check.STATE_LOCKED)

    def test_unknown_code_with_bad_word(self):
        self.use(quark_routes({"code": 41099, "message": "分享不存在"}))
        state, _ = check.check_one("quark", "https://pan.quark.cn/s/abc123")
        self.assertEqual(state, check.STATE_BAD)

    def test_unknown_code_without_hint_is_uncertain(self):
        """没见过的码不许当失效——那会静默吃掉一条好资源。"""
        self.use(quark_routes({"code": 41099, "message": "系统繁忙"}))
        state, _ = check.check_one("quark", "https://pan.quark.cn/s/abc123")
        self.assertEqual(state, check.STATE_UNCERTAIN)

    def test_empty_share_is_bad(self):
        self.use(quark_routes(
            {"code": 0, "data": {"stoken": "s"}},
            {"code": 0, "data": {"list": []}},
        ))
        state, summary = check.check_one("quark", "https://pan.quark.cn/s/abc123")
        self.assertEqual(state, check.STATE_BAD)
        self.assertIn("没有内容", summary)

    def test_network_error_is_uncertain(self):
        import requests

        self.use(quark_routes(requests.ConnectionError("boom")))
        state, summary = check.check_one("quark", "https://pan.quark.cn/s/abc123")
        self.assertEqual(state, check.STATE_UNCERTAIN)
        self.assertIn("网络异常", summary)

    def test_non_json_is_uncertain(self):
        self.use(quark_routes(None))
        state, _ = check.check_one("quark", "https://pan.quark.cn/s/abc123")
        self.assertEqual(state, check.STATE_UNCERTAIN)

    def test_passcode_from_url_is_sent(self):
        session = self.use(quark_routes(
            {"code": 0, "data": {"stoken": "s"}},
            {"code": 0, "data": {"list": [1]}},
        ))
        check.check_one("quark", "https://pan.quark.cn/s/abc123?pwd=8k2m")
        request = session.calls[0]
        self.assertEqual(request[0], "POST")


class TestAliyunAnd123(BaseCase):
    def test_aliyun_cancelled(self):
        self.use({"/get_share_by_anonymous": {
            "code": "ShareLink.Cancelled", "message": "share_link is cancelled by the creator",
        }})
        state, _ = check.check_one("aliyun", "https://www.alipan.com/s/7H1P3TqeXey")
        self.assertEqual(state, check.STATE_BAD)

    def test_aliyun_ok(self):
        self.use({"/get_share_by_anonymous": {"share_name": "电影", "file_count": 3}})
        state, _ = check.check_one("ali", "https://www.alipan.com/s/7H1P3TqeXey")
        self.assertEqual(state, check.STATE_OK)

    def test_123_expired(self):
        self.use({"/api/share/info": {"code": 5104, "message": "分享链接已失效"}})
        state, _ = check.check_one("123", "https://www.123pan.com/s/abcd-efgh")
        self.assertEqual(state, check.STATE_BAD)

    def test_123_with_pwd_needs_code(self):
        self.use({"/api/share/info": {"code": 0, "data": {"HasPwd": True}}})
        state, _ = check.check_one("123", "https://www.123pan.com/s/abcd-efgh")
        self.assertEqual(state, check.STATE_LOCKED)

    def test_123_with_pwd_and_code_present(self):
        self.use({"/api/share/info": {"code": 0, "data": {"HasPwd": True}}})
        state, _ = check.check_one("123", "https://www.123pan.com/s/abcd-efgh", "ab12")
        self.assertEqual(state, check.STATE_OK)


class TestBaidu(BaseCase):
    """百度不带提取码时走 share/init 页：200 要码、404 失效、3xx 公开分享。"""

    def setUp(self):
        super().setUp()
        self._orig = check._request_page

    def patch(self, status: int, text: str = "", location: str = ""):
        check._request_page = lambda url, **kw: (status, text, location)

    def tearDown(self):
        check._request_page = self._orig
        super().tearDown()

    def test_init_200_with_pwd_hint_is_locked(self):
        self.patch(200, text="<html>请输入提取码</html>")
        state, _ = check.check_one("baidu", "https://pan.baidu.com/s/1abcDEF")
        self.assertEqual(state, check.STATE_LOCKED)

    def test_init_404_is_bad(self):
        self.patch(404)
        state, summary = check.check_one("baidu", "https://pan.baidu.com/s/1abcDEF")
        self.assertEqual(state, check.STATE_BAD)
        self.assertIn("失效", summary)

    def test_init_redirect_is_ok(self):
        self.patch(302, location="https://pan.baidu.com/s/1abcDEF")
        state, _ = check.check_one("baidu", "https://pan.baidu.com/s/1abcDEF")
        self.assertEqual(state, check.STATE_OK)

    def test_init_unknown_page_is_uncertain_not_bad(self):
        # 200 但不是提取码页：可能是中转页/安全验证，绝不能判失效
        self.patch(200, text="<html>请稍候</html>")
        state, _ = check.check_one("baidu", "https://pan.baidu.com/s/1abcDEF")
        self.assertEqual(state, check.STATE_UNCERTAIN)


class TestScope(BaseCase):
    def test_unimplemented_platform_is_unsupported(self):
        state, _ = check.check_one("xunlei", "https://pan.xunlei.com/s/abc")
        self.assertEqual(state, check.STATE_UNSUPPORTED)

    def test_key_is_share_id_not_url(self):
        a = check.key_of("quark", "https://pan.quark.cn/s/abc123")
        b = check.key_of("quark", "https://pan.quark.cn/s/abc123?pwd=8k2m")
        self.assertEqual(a, b)
        self.assertEqual(a, "quark:abc123")


class TestAnnotate(BaseCase):
    def _insert(self, key: str, platform: str, state: str, ttl: int = 3600):
        now = time.time()
        conn = sqlite3.connect(str(Path(self._tmp.name) / "t.db"))
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO link_check VALUES(?,?,?,?,?,?,?)",
                (key, platform, "https://x/" + key, state, "", now, now + ttl),
            )
        conn.close()

    def test_bad_is_dropped_and_input_not_mutated(self):
        self._insert("quark:dead", "quark", check.STATE_BAD)
        self._insert("quark:live", "quark", check.STATE_OK)
        items = [
            {"platform": "quark", "shareUrl": "https://pan.quark.cn/s/dead"},
            {"platform": "quark", "shareUrl": "https://pan.quark.cn/s/live"},
            {"platform": "quark", "shareUrl": "https://pan.quark.cn/s/unknown"},
        ]
        kept, dropped = check.annotate(items)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0]["checkState"], check.STATE_OK)
        self.assertNotIn("checkState", kept[1])
        # 缓存里那份必须保持干净
        self.assertNotIn("checkState", items[1])

    def test_expired_row_is_ignored(self):
        self._insert("quark:old", "quark", check.STATE_BAD, ttl=-10)
        kept, dropped = check.annotate(
            [{"platform": "quark", "shareUrl": "https://pan.quark.cn/s/old"}]
        )
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 1)

    def test_hide_bad_off_keeps_card_with_state(self):
        self._insert("quark:dead", "quark", check.STATE_BAD)
        check._hide_bad = False
        try:
            kept, dropped = check.annotate(
                [{"platform": "quark", "shareUrl": "https://pan.quark.cn/s/dead"}]
            )
        finally:
            check._hide_bad = True
        self.assertEqual(dropped, 0)
        self.assertEqual(kept[0]["checkState"], "bad")


if __name__ == "__main__":
    unittest.main()
