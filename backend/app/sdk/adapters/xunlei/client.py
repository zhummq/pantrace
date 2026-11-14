"""迅雷云盘客户端。

迅雷是这几个盘里最麻烦的一个：**每个接口都要带 captcha token**。
调用前先 `POST /v1/shield/captcha/init`，把返回的 token 放进
`x-captcha-token` 头；同一个 action 缓存到过期。少了这一步，
接口返回的是 403 而不是业务错误，看起来像"没权限"。

另外它的 token 刷新和百度一样是轮换式：旧的 refresh_token 用完即废。

转存后拿到的不是 fid，而是 `task.params.trace_file_ids`（一个 JSON 字符串
包着 map），要先解出来才能建分享。这个字段名不看源码猜不到。
"""

from __future__ import annotations

import json
import logging
import time

from ...base import PanClient
from ...errors import (
    AuthError,
    ConfigError,
    NetdiskError,
    ShareExpiredError,
    ShareInvalidError,
    TaskTimeoutError,
    UpstreamError,
    ViolationError,
)
from ...http import Requester
from ...registry import register
from ...types import Account, File, ShareInfo, ShareLink, TaskResult

log = logging.getLogger("pantrace.sdk.xunlei")

USER_BASE = "https://xluser-ssl.xunlei.com"
PAN_BASE = "https://api-pan.xunlei.com"
CLIENT_ID = "Xqp0kJBXWhwaTpB6"
DEVICE_ID = "925b7631473a13716b791d7f28289cad"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

# 分享状态不是错误码，是字符串，只能这么映射
SHARE_STATUS_MAP: dict[str, type[NetdiskError]] = {
    "SENSITIVE_RESOURCE": ViolationError,
    "EXPIRED": ShareExpiredError,
    "DELETED": ShareInvalidError,
    "PROHIBITED": ViolationError,
}


@register
class XunleiClient(PanClient):
    type = "xunlei"
    display = "迅雷云盘"

    def __init__(self, account: Account, *, timeout: float = 15.0, on_cookie=None):
        self._access_token = account.access_token or ""
        self._expires_at = account.expires_at or 0
        self._captchas: dict[str, tuple[str, float]] = {}
        super().__init__(account, timeout=timeout, on_cookie=on_cookie)

    def _build_requester(self) -> Requester:
        if not self.account.refresh_token:
            raise ConfigError("迅雷账号未配置 refresh_token")
        return Requester(
            PAN_BASE,
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/json",
                "Origin": "https://pan.xunlei.com",
                "Referer": "https://pan.xunlei.com/",
                "x-client-id": CLIENT_ID,
                "x-device-id": DEVICE_ID,
            },
            timeout=self.timeout,
        )

    # ---- 认证 ----

    def _token(self) -> str:
        now = int(time.time())
        if self._access_token and self._expires_at - now > 60:
            return self._access_token
        resp = self.http.post(
            f"{USER_BASE}/v1/auth/token",
            json_body={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self.account.refresh_token,
            },
            action="刷新迅雷 token",
            retries=1,
        )
        token = str(resp.get("access_token") or "")
        refresh = str(resp.get("refresh_token") or "")
        if not token:
            raise AuthError("迅雷 token 刷新失败，需要重新授权")
        self._access_token = token
        self._expires_at = now + int(resp.get("expires_in") or 3600) - 60
        self.account.access_token = token
        if refresh:
            self.account.refresh_token = refresh
        if self.on_cookie is not None:
            self.on_cookie(f"__token__={token};__refresh__={refresh}")
        return token

    def _captcha(self, action: str, renew: bool = False) -> str:
        cached = self._captchas.get(action)
        if not renew and cached and cached[1] > time.time():
            return cached[0]
        resp = self.http.post(
            f"{USER_BASE}/v1/shield/captcha/init",
            json_body={
                "client_id": CLIENT_ID,
                "action": action,
                "device_id": DEVICE_ID,
                "captcha_token": cached[0] if cached else "",
                "meta": {
                    "username": "",
                    "phone_number": "",
                    "email": "",
                    "package_name": "pan.xunlei.com",
                    "client_version": "1.45.0",
                    "captcha_sign": "1.fe2108ad808a74c9ac0243309242726c",
                    "timestamp": "1645241033384",
                    "user_id": "0",
                },
            },
            action="取验证令牌",
            retries=1,
        )
        token = str(resp.get("captcha_token") or "")
        if not token:
            if resp.get("url"):
                raise AuthError("迅雷要求人工验证，需要在网页登录一次")
            raise AuthError("迅雷没有返回 captcha_token")
        self._captchas[action] = (token, time.time() + int(resp.get("expires_in") or 300) - 10)
        return token

    def _call(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None, action: str = "") -> dict:
        """带 Authorization + x-captcha-token 的业务请求。"""
        action_key = f"{method.upper()}:{path}"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "x-captcha-token": self._captcha(action_key),
        }
        return self.http.request(
            method,
            f"{PAN_BASE}{path}",
            params=query,
            json_body=body,
            headers=headers,
            action=action or path,
        )

    # ---- 分享侧 ----

    def share_info(self, url: str, pwd: str = "") -> ShareInfo:
        from ...urls import parse

        parsed = parse(url)
        if not parsed.ok:
            raise ShareInvalidError("链接不是迅雷分享")
        share_id = parsed.pwd_id
        passcode = pwd or parsed.passcode

        resp = self._call(
            "GET",
            "/drive/v1/share",
            query={
                "share_id": share_id,
                "pass_code": passcode,
                "limit": 100,
                "pass_code_token": "",
                "page_token": "",
                "thumbnail_size": "SIZE_SMALL",
            },
            action="读取迅雷分享",
        )
        status = str(resp.get("share_status") or "OK")
        if status != "OK":
            exc = SHARE_STATUS_MAP.get(status)
            text = str(resp.get("share_status_text") or status)
            if exc is not None:
                raise exc(text, code=status, raw=resp)
            raise ShareInvalidError(text, code=status, raw=resp)

        raw_files = resp.get("files") or []
        if not raw_files:
            raise ShareInvalidError("分享里没有文件")
        files: list[File] = []
        total = 0
        for item in raw_files:
            fid = str(item.get("id") or item.get("file_id") or "")
            if not fid:
                continue
            size = int(item.get("size") or 0)
            total += size
            files.append(
                File(
                    fid=fid,
                    name=str(item.get("name") or ""),
                    size=size,
                    is_dir=bool(item.get("is_dir")) or item.get("kind") == "drive#folder",
                )
            )
        return ShareInfo(
            platform="xunlei",
            pwd_id=share_id,
            title=str(resp.get("title") or "")[:60],
            passcode=passcode,
            files=files,
            total_size=total,
            extra={"pass_code_token": resp.get("pass_code_token") or ""},
        )

    def transfer(self, info: ShareInfo, to_dir: str = "") -> list[str]:
        parent = to_dir or ""
        resp = self._call(
            "POST",
            "/drive/v1/share/restore",
            body={
                "parent_id": parent,
                "share_id": info.pwd_id,
                "pass_code_token": info.extra.get("pass_code_token", ""),
                "ancestor_ids": [],
                "specify_parent_id": True,
                "file_ids": [f.fid for f in info.files],
            },
            action="转存",
        )
        task_id = str(resp.get("restore_task_id") or "")
        if not task_id:
            raise UpstreamError("迅雷转存没有返回任务号", raw=resp)
        return self.wait_task(task_id, timeout=max(45.0, self.timeout * 3)).fids

    def task_status(self, task_id: str) -> TaskResult:
        resp = self._call("GET", f"/drive/v1/tasks/{task_id}", action="查询转存任务")
        progress = int(resp.get("progress") or 0)
        if progress < 100:
            return TaskResult(task_id=task_id, done=False, extra={"raw": resp})
        trace = (resp.get("params") or {}).get("trace_file_ids")
        fids = _trace_ids(trace)
        if not fids:
            raise UpstreamError("迅雷任务完成但没有 trace_file_ids", raw=resp)
        return TaskResult(task_id=task_id, status=progress, done=True, fids=fids, extra={"raw": resp})

    def create_share(self, fids: list[str], title: str, expire_hours: int = 0) -> ShareLink:
        days = 1 if not expire_hours else max(1, min(int(expire_hours // 24) or 1, 7))
        resp = self._call(
            "POST",
            "/drive/v1/share",
            body={
                "file_ids": fids,
                "share_to": "copy",
                "params": {"subscribe_push": "false", "WithPassCodeInLink": "true"},
                "title": (title or "云盘资源分享")[:60],
                "restore_limit": -1,
                "expiration_days": days,
            },
            action="创建分享",
        )
        share_url = str(resp.get("share_url") or "")
        if not share_url:
            raise UpstreamError("迅雷创建分享没有返回 share_url", raw=resp)
        passcode = str(resp.get("pass_code") or "")
        if passcode and "pwd=" not in share_url:
            share_url = f"{share_url}?pwd={passcode}"
        return ShareLink(url=share_url, share_id=str(resp.get("share_id") or ""), passcode=passcode)

    # ---- 文件侧 ----

    def list_files(self, parent_id: str = "", page: int = 1, size: int = 100) -> list[File]:
        resp = self._call(
            "GET",
            "/drive/v1/files",
            query={
                "parent_id": parent_id or "",
                "page_token": "",
                "limit": min(int(size), 100),
                "thumbnail_size": "SIZE_SMALL",
                "filters": json.dumps({"phase": {"eq": "PHASE_TYPE_COMPLETE"}}),
            },
            action="列目录",
        )
        out: list[File] = []
        for item in resp.get("files") or []:
            fid = str(item.get("id") or "")
            if not fid:
                continue
            out.append(
                File(
                    fid=fid,
                    name=str(item.get("name") or ""),
                    size=int(item.get("size") or 0),
                    is_dir=bool(item.get("is_dir")) or item.get("kind") == "drive#folder",
                )
            )
        return out

    def delete(self, fids: list[str]) -> None:
        if not fids:
            return
        self._call("DELETE", "/drive/v1/files", query={"file_ids": ",".join(fids)}, action="删除文件")

    def probe(self) -> bool:
        try:
            self._token()
            return True
        except NetdiskError:
            return False


def _trace_ids(raw: object) -> list[str]:
    """trace_file_ids 是个 JSON 字符串：{"a":"id1","b":"id2"}。只取值。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, dict):
        return [str(v) for v in raw.values() if isinstance(v, str)]
    if isinstance(raw, list):
        return [str(v) for v in raw if isinstance(v, str)]
    return []
