"""迅雷 captcha / user_id 修复的回归测试。

线上症状：转存成功、但建引流水印目录报 `HTTP 400 验证码无效`。
根因：写操作（建目录）的 captcha 校验 meta.user_id，而 user_id 以前只在
      token 刷新时才赋值——新建 client + token 未过期时它一直是空串，
      captcha 带 "0" 被拒。

这里守住三件事：
1. access_token（JWT）能解出 user_id，坏 token 安全返回空串；
2. 构造 client 时 user_id 已就位，不再等刷新；
3. 建水印传 captcha_renew=True，且 meta.user_id 用的是真值不是 "0"。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_xunlei_captcha -v``
"""

from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

from app.sdk.adapters.xunlei.client import XunleiClient, _user_id_from_jwt
from app.sdk.types import Account


def _jwt(claims: dict) -> str:
    """造一个只有 payload 的 JWT（不验签，够测解析）。"""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"hdr.{payload}.sig"


def _account(token: str = "") -> Account:
    return Account(
        id=7,
        type="xunlei",
        name="test",
        access_token=token,
        refresh_token="refresh-me",  # _build_requester 要求非空
        temp_dir="pantrace",
    )


class TestJwtUserId(unittest.TestCase):
    def test_user_id_claim(self):
        self.assertEqual(_user_id_from_jwt(_jwt({"user_id": "12345"})), "12345")

    def test_sub_claim(self):
        self.assertEqual(_user_id_from_jwt(_jwt({"sub": "u-678"})), "u-678")

    def test_uid_claim(self):
        self.assertEqual(_user_id_from_jwt(_jwt({"uid": "999"})), "999")

    def test_user_id_wins_over_sub(self):
        self.assertEqual(
            _user_id_from_jwt(_jwt({"user_id": "first", "sub": "second"})), "first"
        )

    def test_garbage_token_returns_empty(self):
        for bad in ("", "not-a-jwt", "a.b", "a.!!!!!.c", "hdr.e30.sig"):
            with self.subTest(bad=bad):
                self.assertEqual(_user_id_from_jwt(bad), "")


class TestClientUserId(unittest.TestCase):
    def test_init_fills_user_id_from_token(self):
        client = XunleiClient(_account(_jwt({"user_id": "real-42"})))
        self.assertEqual(client._user_id, "real-42")

    def test_init_without_claims_leaves_empty(self):
        client = XunleiClient(_account(""))
        self.assertEqual(client._user_id, "")

    def test_captcha_meta_uses_real_user_id(self):
        """关键回归：captcha init 的 meta.user_id 必须是真值，不是 "0"。"""
        client = XunleiClient(_account(_jwt({"user_id": "real-42"})))
        client.http = mock.Mock()
        client.http.post.return_value = {"captcha_token": "cap-1", "expires_in": 300}

        token = client._captcha("POST:/drive/v1/files")

        self.assertEqual(token, "cap-1")
        meta = client.http.post.call_args.kwargs["json_body"]["meta"]
        self.assertEqual(meta["user_id"], "real-42")
        self.assertNotEqual(meta["user_id"], "0")

    def test_captcha_falls_back_to_jwt_when_uid_late(self):
        """_user_id 还没同步时，_captcha 现从 access_token 解一次兜底。"""
        client = XunleiClient(_account(_jwt({"user_id": "late-99"})))
        client._user_id = ""  # 模拟没同步
        client.http = mock.Mock()
        client.http.post.return_value = {"captcha_token": "cap-2", "expires_in": 300}

        client._captcha("POST:/drive/v1/files")

        meta = client.http.post.call_args.kwargs["json_body"]["meta"]
        self.assertEqual(meta["user_id"], "late-99")


class TestWatermarkCaptchaRenew(unittest.TestCase):
    def test_make_watermark_requests_renew(self):
        """建水印必须强制刷新 captcha，不能复用建转存目录那次的 token。"""
        client = XunleiClient(_account(_jwt({"user_id": "real-42"})))
        client._resolve_transfer_parent = mock.Mock(return_value="parent-fid")
        client._call = mock.Mock(return_value={"file": {"id": "wm-fid"}})

        result = client.make_watermark("广告名")

        self.assertEqual(result, "wm-fid")
        self.assertTrue(client._call.call_args.kwargs["captcha_renew"])

    def test_renew_flag_reaches_request_once(self):
        """captcha_renew 要从 _call 一路透传到 _request_once → _captcha。"""
        client = XunleiClient(_account(_jwt({"user_id": "real-42"})))
        with mock.patch.object(client, "_captcha", return_value="cap") as cap, \
             mock.patch.object(client, "_token", return_value="tok"), \
             mock.patch.object(client.http, "request", return_value={}):
            client._call("POST", "/drive/v1/files", body={}, captcha_renew=True)
        self.assertTrue(cap.call_args.kwargs["renew"])

    def test_call_without_flag_does_not_renew(self):
        client = XunleiClient(_account(_jwt({"user_id": "real-42"})))
        with mock.patch.object(client, "_captcha", return_value="cap") as cap, \
             mock.patch.object(client, "_token", return_value="tok"), \
             mock.patch.object(client.http, "request", return_value={}):
            client._call("GET", "/drive/v1/files")
        self.assertFalse(cap.call_args.kwargs.get("renew", False))


if __name__ == "__main__":
    unittest.main()
