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
                # 直接给 FakeResponse 时原样返回：天翼那条判定要读 XML 正文，
                # 不是 JSON，只能这样喂进去。
                if isinstance(payload, FakeResponse):
                    return payload
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


class TestTianyi(BaseCase):
    """天翼：只认它自己的 XML 错误码枚举，码表外一律判不了。"""

    def route(self, body: str, status: int = 200):
        self.use({"getShareInfoByCodeV2": FakeResponse(None, status=status, text=body)})

    def test_share_vo_is_ok(self):
        self.route(
            '<?xml version="1.0" encoding="UTF-8"?><shareVO>'
            "<shareId>123</shareId><fileName>电影合集</fileName>"
            "<needAccessCode>0</needAccessCode></shareVO>"
        )
        state, _ = check.check_one("tianyi", "https://cloud.189.cn/t/AbCdEf123")
        self.assertEqual(state, check.STATE_OK)

    def test_need_access_code_is_locked(self):
        self.route("<shareVO><shareId>0</shareId><needAccessCode>1</needAccessCode></shareVO>")
        state, _ = check.check_one("tianyi", "https://cloud.189.cn/t/AbCdEf123")
        self.assertEqual(state, check.STATE_LOCKED)

    def test_known_error_code_is_bad(self):
        self.route("<error><code>ShareExpiredError</code><message>分享已过期</message></error>")
        state, summary = check.check_one("tianyi", "https://cloud.189.cn/t/AbCdEf123")
        self.assertEqual(state, check.STATE_BAD)
        self.assertIn("过期", summary)

    def test_access_code_error_is_locked_not_bad(self):
        self.route("<error><code>AccessCodeError</code><message>访问码错误</message></error>")
        state, _ = check.check_one("tianyi", "https://cloud.189.cn/t/AbCdEf123")
        self.assertEqual(state, check.STATE_LOCKED)

    def test_unknown_error_code_is_uncertain_not_bad(self):
        # 189 的码表会变，新码不认识时不许当成死链
        self.route("<error><code>SomeBrandNewError</code><message>系统繁忙</message></error>")
        state, _ = check.check_one("tianyi", "https://cloud.189.cn/t/AbCdEf123")
        self.assertEqual(state, check.STATE_UNCERTAIN)

    def test_html_page_is_uncertain_not_bad(self):
        self.route("<html><body>页面不存在</body></html>")
        state, _ = check.check_one("tianyi", "https://cloud.189.cn/t/AbCdEf123")
        self.assertEqual(state, check.STATE_UNCERTAIN)

    def test_empty_body_is_uncertain(self):
        self.route("")
        state, _ = check.check_one("tianyi", "https://cloud.189.cn/t/AbCdEf123")
        self.assertEqual(state, check.STATE_UNCERTAIN)

    def test_access_code_from_share_text(self):
        # 天翼的码常写在链接后面的「（访问码：xxxx）」，要拼进 shareCode 一起发
        seen: dict = {}

        def fake_request(method, url, **kwargs):
            seen.update(kwargs)
            return 200, "<shareVO><shareId>1</shareId><fileName>x</fileName></shareVO>"

        orig, check._request_raw = check._request_raw, fake_request
        try:
            state, _ = check.check_one("tianyi", "https://cloud.189.cn/t/AbCdEf123（访问码：8xy2）")
        finally:
            check._request_raw = orig
        self.assertEqual(state, check.STATE_OK)
        self.assertEqual(seen["params"]["shareCode"], "AbCdEf123（访问码：8xy2）")


class TestXunlei(BaseCase):
    """迅雷：share_status 是服务端枚举，非 OK 才算死；403 是风控不是死链。"""

    def route(self, payload, status: int = 200):
        self.use({
            "captcha/init": {"captcha_token": "tk"},
            "drive/v1/share": FakeResponse(payload, status=status),
        })

    def test_share_status_ok(self):
        self.route({"share_status": "OK", "file_count": 3})
        state, _ = check.check_one("xunlei", "https://pan.xunlei.com/s/VNabc123")
        self.assertEqual(state, check.STATE_OK)

    def test_file_count_without_status_is_ok(self):
        self.route({"file_count": 5, "share_name": "剧集"})
        state, _ = check.check_one("xunlei", "https://pan.xunlei.com/s/VNabc123")
        self.assertEqual(state, check.STATE_OK)

    def test_404_is_bad(self):
        self.route(None, status=404)
        state, _ = check.check_one("xunlei", "https://pan.xunlei.com/s/VNabc123")
        self.assertEqual(state, check.STATE_BAD)

    def test_403_is_uncertain_not_bad(self):
        self.route(None, status=403)
        state, _ = check.check_one("xunlei", "https://pan.xunlei.com/s/VNabc123")
        self.assertEqual(state, check.STATE_UNCERTAIN)

    def test_pass_code_error_is_locked(self):
        self.route({"error": "invalid_pass_code", "error_description": "提取码错误"})
        state, _ = check.check_one("xunlei", "https://pan.xunlei.com/s/VNabc123")
        self.assertEqual(state, check.STATE_LOCKED)

    def test_deleted_status_is_bad(self):
        self.route({"share_status": "SHARE_STATUS_DELETED", "share_status_text": "分享已删除"})
        state, summary = check.check_one("xunlei", "https://pan.xunlei.com/s/VNabc123")
        self.assertEqual(state, check.STATE_BAD)
        self.assertIn("删除", summary)

    def test_unknown_error_is_uncertain(self):
        self.route({"error": "internal_error", "error_description": "服务开小差"})
        state, _ = check.check_one("xunlei", "https://pan.xunlei.com/s/VNabc123")
        self.assertEqual(state, check.STATE_UNCERTAIN)

    def test_share_id_invalid_detail_is_bad(self):
        # 实测真实返回：请求格式对、只有 id 不成立，这时才是死链
        self.route({
            "error": "invalid_argument", "error_code": 3, "error_description": "请求参数错误",
            "error_details": [{"detail": "share_id can not be empty or share_id invalid"}],
        })
        state, _ = check.check_one("xunlei", "https://pan.xunlei.com/s/VNabc123")
        self.assertEqual(state, check.STATE_BAD)


class Test115(BaseCase):
    """115：没有提取码时判不了死，这支只允许 ok / locked / uncertain。"""

    def route(self, payload):
        self.use({"/webapi/share/snap": payload})

    def test_snap_with_files_is_ok(self):
        self.route({"state": True, "errno": 0, "data": {
            "count": 2, "share_state": 1, "list": [{"n": "a"}, {"n": "b"}],
            "shareinfo": {"snap_id": "s1", "share_title": "电影"},
        }})
        state, _ = check.check_one("115", "https://115.com/s/swg8xyz", "ab12")
        self.assertEqual(state, check.STATE_OK)

    def test_public_share_without_code_is_ok(self):
        # 115 有不带码的公开分享，不能因为没码就先判 locked
        self.route({"state": True, "errno": 0, "data": {
            "count": 1, "share_state": 1, "shareinfo": {"snap_id": "s1"},
        }})
        state, _ = check.check_one("115", "https://115.com/s/swg8xyz")
        self.assertEqual(state, check.STATE_OK)

    def test_wrong_code_is_locked(self):
        self.route({"state": False, "errno": 40004, "error": "密码错误"})
        state, _ = check.check_one("115", "https://115.com/s/swg8xyz", "ab12")
        self.assertEqual(state, check.STATE_LOCKED)

    def test_forbid_reason_with_code_is_bad(self):
        self.route({"state": True, "errno": 0, "data": {
            "share_state": 2, "shareinfo": {"forbid_reason": "分享已取消"},
        }})
        state, _ = check.check_one("115", "https://115.com/s/swg8xyz", "ab12")
        self.assertEqual(state, check.STATE_BAD)

    def test_dead_text_without_code_stays_uncertain(self):
        # 没带码时接口分不清"码不对"和"分享没了"，绝不能判死
        self.route({"state": False, "errno": 40001, "error": "分享不存在"})
        state, _ = check.check_one("115", "https://115.com/s/swg8xyz")
        self.assertEqual(state, check.STATE_UNCERTAIN)

    def test_unknown_share_state_is_uncertain(self):
        self.route({"state": True, "errno": 0, "data": {
            "share_state": 9, "shareinfo": {"snap_id": "s1"},
        }})
        state, _ = check.check_one("115", "https://115.com/s/swg8xyz", "ab12")
        self.assertEqual(state, check.STATE_UNCERTAIN)

    def test_password_from_url_query(self):
        # 115 的码常挂在 ?password=，上游 sharePwd 常为空，得自己从 URL 取
        self.route({"state": False, "errno": 40001, "error": "分享不存在"})
        state, _ = check.check_one("115", "https://115.com/s/swg8xyz?password=ab12")
        self.assertEqual(state, check.STATE_BAD)

    def test_no_code_param_error_asks_for_code(self):
        # 实测：不带 receive_code 时 115 一律回 990002 参数错误，问不出结论
        self.route({"state": False, "errno": 990002, "error": "参数错误。"})
        state, _ = check.check_one("115", "https://115.com/s/swg8xyz")
        self.assertEqual(state, check.STATE_LOCKED)

    def test_param_error_with_code_is_uncertain_not_bad(self):
        # 带码时也可能是码格式不对，"参数错误"证明不了分享没了
        self.route({"state": False, "errno": 990002, "error": "参数错误。"})
        state, _ = check.check_one("115", "https://115.com/s/swg8xyz", "ab12")
        self.assertEqual(state, check.STATE_UNCERTAIN)


class TestScope(BaseCase):
    def test_unimplemented_platform_is_unsupported(self):
        state, _ = check.check_one("guangya", "https://www.guangyapan.com/s/abc")
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
