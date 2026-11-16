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
        self._temp_dir_id = ""
        self._user_id = ""
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
        # 写操作（如建目录）的 captcha 会校验 meta.user_id，传 "0" 会被
        # captcha_invalid 拒掉（实测）；读操作不挑。刷新响应里有真实 user_id
        self._user_id = str(resp.get("user_id") or resp.get("sub") or "")
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
                    "user_id": self._user_id or "0",
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

    def _call(
        self,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
        action: str = "",
        gone_error: type[NetdiskError] | None = None,
    ) -> dict:
        """带 Authorization + x-captcha-token 的业务请求。

        gone_error：分享侧 404/410 该抛什么。不传的话 http 层只能报裸
        "HTTP 404"，后台验链看不出是源链接死了还是账号挂了。
        """
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
            gone_error=gone_error,
        )

    # ---- 转存目标目录 ----

    def _ensure_transfer_dir(self) -> str:
        """解析转存目标目录的 parent_id。

        迅雷 restore 的 parent_id **不能传空串**：网页客户端和竞品实际都传
        真实目录 ID（竞品的 tempDir 由管理员手填），空串会被接口 404 拒掉，
        表现成一句看不出原因的"转存失败：HTTP 404"。

        账号没配 temp_dir 时，找根目录下的 pantrace 目录（与百度盘同名约定），
        没有就建一个。目录该由我们负责，不能指望用户去网页端手工建。
        """
        if self.account.temp_dir:
            return self.account.temp_dir
        if self._temp_dir_id:
            return self._temp_dir_id
        resp = self._call(
            "GET",
            "/drive/v1/files",
            query={
                "parent_id": "",
                "page_token": "",
                "limit": 200,
                "thumbnail_size": "SIZE_SMALL",
                "filters": json.dumps(
                    {"phase": {"eq": "PHASE_TYPE_COMPLETE"}, "trashed": {"eq": False}}
                ),
            },
            action="找转存目录",
        )
        for item in resp.get("files") or []:
            fid = str(item.get("id") or "")
            if (
                fid
                and (item.get("kind") == "drive#folder" or item.get("is_dir"))
                and str(item.get("name") or "") == "pantrace"
            ):
                self._temp_dir_id = fid
                return fid
        resp = self._call(
            "POST",
            "/drive/v1/files",
            body={
                "kind": "drive#folder",
                "name": "pantrace",
                "parent_id": "",
                "space": "",
            },
            action="建转存目录",
        )
        fid = str((resp.get("file") or {}).get("id") or "")
        if not fid:
            raise UpstreamError("迅雷建转存目录没返回目录 id", raw=resp)
        self._temp_dir_id = fid
        return fid

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
            gone_error=ShareInvalidError,
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
        # parent_id 空串会被 restore 404 拒掉，必须解析成真实目录 ID
        parent = to_dir or self._ensure_transfer_dir()
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
            # 404 多数是"分享还挂着、文件已被删/违规屏蔽"，映射成源链接失效，
            # 别让后台验链报一句裸 HTTP 404 看不出该修哪头
            gone_error=ShareInvalidError,
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

    def create_share(self, fids: list[str], title: str, expire_hours: int = 24) -> ShareLink:
        # 迅雷按天，向上取整：请求 30 小时就给 2 天，不是 1 天（截断会让安全网
        # 比配置更早失效）。上限 7 天是接口自己的限制，填更大也只能给 7 天
        hours = max(0, int(expire_hours or 0))
        days = max(1, min((hours + 23) // 24, 7))
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
        # DELETE /drive/v1/files 实测 501 unimplemented（迅雷根本没实现这个方法）。
        # 网页端和竞品都走 POST /drive/v1/files:batchDelete，换过来回收任务才能删掉文件
        self._call(
            "POST",
            "/drive/v1/files:batchDelete",
            body={"ids": fids, "space": ""},
            action="删除文件",
        )

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
