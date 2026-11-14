"""光鸭云盘客户端。

三个和其它盘不一样的地方：

1. **接口分散在四个域**：userres v1（转存/任务）、userres v2（建分享）、
   assets（容量）、account（token）。不是同一个 base_url，所以请求要传完整地址。
2. **转存不返回新 fid**。`restore_share` 只给一个 taskId，转存完成后要去
   目录里做「文件名差集」才知道哪几个文件是新进来的。这是全套 SDK 里
   最脏的一段，但没别的办法——它的接口就是不给你 fid。
3. **成功判定没有统一 code**。success/ok 为 true、msg=success、code/status/result
   为 0/200 都算成功，只能按组合判断。

另外它有限流：相邻请求至少隔 50–100ms。并发跑太快会被直接掐断。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from urllib.parse import urlparse

from ...base import PanClient
from ...errors import (
    AuthError,
    ConfigError,
    NetdiskError,
    QuotaExceededError,
    ShareInvalidError,
    TaskTimeoutError,
    UpstreamError,
    ViolationError,
)
from ...http import Requester
from ...registry import register
from ...types import Account, File, ShareInfo, ShareLink, TaskResult

log = logging.getLogger("pantrace.sdk.guangya")

API_RES = "https://api.guangyapan.com/nd.bizuserres.s/v1"
API_RES_V2 = "https://api.guangyapan.com/userres/v1"
API_ACCOUNT = "https://account.guangyapan.com/v1"
WEB_ORIGIN = "https://www.guangyapan.com"
CLIENT_ID = "aMe-8VSlkrbQXpUR"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
# 转存后目录 diff 的重试次数与间隔
DIFF_RETRY = 15
DIFF_INTERVAL = 0.5


@register
class GuangyaClient(PanClient):
    type = "guangya"
    display = "光鸭云盘"

    def __init__(self, account: Account, *, timeout: float = 15.0, on_cookie=None):
        self._access = account.access_token or ""
        self._expires = account.expires_at or 0
        self._device = account.extra.get("device_id") or _device_id()
        self._last_req = 0.0
        super().__init__(account, timeout=timeout, on_cookie=on_cookie)

    def _build_requester(self) -> Requester:
        if not self.account.refresh_token and not self._access:
            raise ConfigError("光鸭账号缺少 refresh_token / access_token")
        return Requester(
            API_RES,
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": WEB_ORIGIN,
                "referer": f"{WEB_ORIGIN}/",
                "user-agent": UA,
                "dt": "4",
            },
            timeout=self.timeout,
        )

    # ---- 请求 ----

    def _throttle(self) -> None:
        gap = 0.05 + (os.urandom(1)[0] / 255) * 0.05
        wait = gap - (time.monotonic() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def _headers(self, token: str = "") -> dict[str, str]:
        headers = {
            "did": self._device,
            "traceparent": f"00-{os.urandom(16).hex()}-{os.urandom(8).hex()}-01",
        }
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers

    def _token(self) -> str:
        if self._access and self._expires - time.time() > 60:
            return self._access
        if not self.account.refresh_token:
            if self._access:
                return self._access
            raise AuthError("光鸭没有可用的 token")
        resp = self.http.post(
            f"{API_ACCOUNT}/auth/token",
            json_body={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self.account.refresh_token,
            },
            headers=self._headers(),
            action="刷新光鸭 token",
            retries=1,
        )
        token = str(resp.get("access_token") or "")
        if not token:
            raise AuthError("光鸭 token 刷新失败，需要重新授权")
        self._access = token
        self._expires = time.time() + int(resp.get("expires_in") or 3600) - 60
        self.account.access_token = token
        if resp.get("refresh_token"):
            self.account.refresh_token = str(resp["refresh_token"])
        if self.on_cookie is not None:
            self.on_cookie(f"__token__={token};__refresh__={self.account.refresh_token}")
        return token

    def _post(self, url: str, payload: dict, *, anonymous: bool = False, action: str = "") -> dict:
        self._throttle()
        token = "" if anonymous else self._token()
        return self.http.post(
            url,
            json_body=payload,
            headers=self._headers(token),
            action=action,
        )

    @staticmethod
    def _ok(payload: dict, action: str) -> dict:
        """光鸭没有统一成功码，按组合判断。宁可放过也不要误判成失败——
        误判会让一次本来成功的转存被判死，然后被缓存十分钟。"""
        if not isinstance(payload, dict):
            raise UpstreamError(f"{action}失败：响应不是对象")
        if payload.get("success") is True or payload.get("ok") is True:
            return payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for key in ("msg", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip().lower() == "success":
                return payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for key in ("code", "status", "result"):
            if str(payload.get(key)).lower() in ("0", "200", "ok", "success"):
                return payload.get("data") if isinstance(payload.get("data"), dict) else {}
        data = payload.get("data")
        if isinstance(data, dict) and data:
            return data
        message = str(payload.get("msg") or payload.get("message") or action)
        raise UpstreamError(f"{action}失败：{message}", raw=payload)

    # ---- 分享侧 ----

    def share_info(self, url: str, pwd: str = "") -> ShareInfo:
        from ...urls import parse

        parsed = parse(url)
        if not parsed.ok:
            raise ShareInvalidError("链接不是光鸭分享")
        share_id = parsed.pwd_id
        passcode = pwd or parsed.passcode

        summary = self._post(
            f"{API_RES}/get_share_summary", {"shareId": share_id}, anonymous=True, action="校验分享"
        )
        data = summary.get("data") if isinstance(summary.get("data"), dict) else {}
        if str(data.get("shareStatus")) not in ("1", "None") and data.get("shareStatus") is not None:
            raise ShareInvalidError(str(summary.get("msg") or "分享不存在或已失效"), raw=summary)

        token_resp = self._post(
            f"{API_RES}/get_share_access_token",
            {"shareId": share_id, "code": passcode},
            anonymous=True,
            action="取分享访问令牌",
        )
        access = _dig(token_resp, "accessToken", "access_token", "token")
        if not access:
            raise ShareInvalidError("获取分享访问令牌失败", raw=token_resp)

        files: list[File] = []
        total = 0
        for page in range(1, 6):
            listing = self._post(
                f"{API_RES}/get_share_page_files_list",
                {"accessToken": access, "parentId": "", "page": page, "pageSize": 50, "orderBy": 0, "sortType": 0},
                anonymous=True,
                action="读取分享文件",
            )
            body = self._ok(listing, "读取分享文件")
            items = body.get("list") or body.get("items") or []
            if not items:
                break
            for item in items:
                fid = str(item.get("fileId") or item.get("id") or "")
                if not fid:
                    continue
                size = int(item.get("fileSize") or item.get("size") or 0)
                total += size
                files.append(
                    File(
                        fid=fid,
                        name=str(item.get("fileName") or item.get("name") or ""),
                        size=size,
                        is_dir=bool(item.get("isDir") or item.get("dir")),
                    )
                )
            if len(items) < 50:
                break
        if not files:
            raise ShareInvalidError("分享里没有文件")

        return ShareInfo(
            platform="guangya",
            pwd_id=share_id,
            title=files[0].name if files else "光鸭资源",
            passcode=passcode,
            files=files,
            total_size=total,
            extra={"access_token": access, "total_size_hint": int(data.get("totalFileSize") or 0)},
        )

    def transfer(self, info: ShareInfo, to_dir: str = "") -> list[str]:
        parent = to_dir or ""
        before = {f.name for f in self.list_files(parent)}

        resp = self._post(
            f"{API_RES}/restore_share",
            {
                "accessToken": info.extra.get("access_token", ""),
                "fileIds": [f.fid for f in info.files],
                "parentId": parent,
            },
            action="转存",
        )
        self._ok(resp, "转存")
        task_id = _dig(resp, "taskId", "task_id")
        if task_id:
            self.wait_task(str(task_id), timeout=max(45.0, self.timeout * 3))

        # 目录差集：它是唯一能知道"哪几个是新文件"的办法
        for _ in range(DIFF_RETRY):
            after = self.list_files(parent)
            fresh = [f for f in after if f.name not in before]
            if fresh:
                return [f.fid for f in fresh]
            time.sleep(DIFF_INTERVAL)
        raise UpstreamError("转存后未找到新增文件（目录 diff 超时）")

    def task_status(self, task_id: str) -> TaskResult:
        resp = self._post(f"{API_RES}/get_task_status", {"taskId": task_id}, action="查询任务")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        status = int(data.get("status") or 0)
        if status == 3:
            raise UpstreamError(str(resp.get("msg") or "光鸭任务失败"), raw=resp)
        return TaskResult(task_id=task_id, status=status, done=status == 2, extra={"raw": resp})

    def create_share(self, fids: list[str], title: str, expire_hours: int = 0) -> ShareLink:
        resp = self._post(
            f"{API_RES_V2}/share_file",
            {"fileIds": fids, "title": (title or "资源分享")[:60]},
            action="创建分享",
        )
        data = self._ok(resp, "创建分享")
        url = str(data.get("shareUrl") or "")
        if not url:
            raise UpstreamError("光鸭创建分享没有返回 shareUrl", raw=resp)
        return ShareLink(url=url, passcode=str(data.get("code") or ""), share_id=str(data.get("shareId") or ""))

    # ---- 文件侧 ----

    def list_files(self, parent_id: str = "", page: int = 1, size: int = 100) -> list[File]:
        resp = self._post(
            f"{API_RES}/get_page_files_list",
            {"parentId": parent_id or "", "page": page, "pageSize": min(int(size), 200), "orderBy": 0, "sortType": 0},
            action="列目录",
        )
        body = self._ok(resp, "列目录")
        out: list[File] = []
        for item in body.get("list") or body.get("items") or []:
            fid = str(item.get("fileId") or item.get("id") or "")
            if not fid:
                continue
            out.append(
                File(
                    fid=fid,
                    name=str(item.get("fileName") or item.get("name") or ""),
                    size=int(item.get("fileSize") or item.get("size") or 0),
                    is_dir=bool(item.get("isDir") or item.get("dir")),
                )
            )
        return out

    def delete(self, fids: list[str]) -> None:
        if not fids:
            return
        resp = self._post(f"{API_RES}/delete_files", {"fileIds": fids}, action="删除文件")
        self._ok(resp, "删除文件")

    def probe(self) -> bool:
        try:
            self._token()
            return True
        except NetdiskError:
            return False


def _device_id() -> str:
    return hashlib.md5(os.urandom(16).hex().encode()).hexdigest()


def _dig(payload: dict, *keys: str) -> str:
    """在顶层和 data 里找第一个非空字符串字段。光鸭的返回层级不固定。"""
    scopes = [payload]
    if isinstance(payload.get("data"), dict):
        scopes.append(payload["data"])
    for scope in scopes:
        for key in keys:
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, int):
                return str(value)
    return ""
