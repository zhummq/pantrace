"""百度网盘客户端。

和其它盘最大的不同：**两套认证**。
- cookie 模式（BDUSS）：绝大多数接口都能用，最省事；
- OAuth 模式：access_token 两小时过期，必须用 refresh_token 续，且每次续
  都会换发新的 refresh_token —— 不落库就只续得了一次。

百度还要求很多接口带 `bdstoken`，它跟着 cookie 走，所以要单独取一次并缓存。
form-urlencoded 而不是 JSON，这点也别改：百度那套老接口对 JSON body
会返回 errno=2 参数错误，看起来像字段填错了。
"""

from __future__ import annotations

import json
import logging
import time

from ...base import PanClient, poll
from ...errors import (
    AuthError,
    ConfigError,
    NetdiskError,
    NotFoundError,
    QuotaExceededError,
    ShareExpiredError,
    ShareInvalidError,
    TaskTimeoutError,
    UnsupportedError,
    UpstreamError,
    ViolationError,
    map_error,
)
from ...http import Requester
from ...registry import register
from ...types import Account, File, ShareInfo, ShareLink, TaskResult

log = logging.getLogger("pantrace.sdk.baidu")

API = "https://pan.baidu.com"
OAUTH_TOKEN = "https://openapi.baidu.com/oauth/2.0/token"
SHARE_HOST = "https://pan.baidu.com/s/"
# 我们建分享时统一用这个码。百度要求 schannel=4 时 pwd 必填，且必须是 4 位
DEFAULT_PWD = "6666"

ERRNO_MAP: dict[object, type[NetdiskError]] = {
    -6: AuthError,        # 登录态失效
    -7: AuthError,        # bdstoken 无效
    4: TaskTimeoutError,  # 请求超时，可重试
    105: ShareInvalidError,
    110: ShareInvalidError,
    112: ShareExpiredError,
    113: ShareInvalidError,
    115: ShareInvalidError,
    130: NotFoundError,
    132: QuotaExceededError,
    31066: QuotaExceededError,
    9007: ViolationError,
}

# 百度分享侧用 errtype 而不是 errno 表达失效原因
ERRTYPE_MAP: dict[object, type[NetdiskError]] = {
    0: ShareInvalidError,   # 文件已被删除
    1: ShareExpiredError,   # 分享已被取消
    3: ViolationError,      # 侵权/色情/反动，无法访问
}


@register
class BaiduClient(PanClient):
    type = "baidu"
    display = "百度网盘"
    max_file_bytes = 100 * 1024 ** 3

    def __init__(self, account: Account, *, timeout: float = 15.0, on_cookie=None):
        self._bdstoken = ""
        self._bdstoken_at = 0.0
        self._uk = ""
        super().__init__(account, timeout=timeout, on_cookie=on_cookie)

    # ---- 请求层 ----

    def _build_requester(self) -> Requester:
        cookies = self.account.cookie or ""
        if not cookies and not self.account.refresh_token and not self.account.access_token:
            raise ConfigError("百度网盘账号既没有 cookie 也没有 refresh_token")
        return Requester(
            API,
            headers={
                "Cookie": cookies,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Referer": "http://pan.baidu.com/",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=self.timeout,
        )

    def _ok(self, payload: dict, action: str) -> dict:
        """百度用 errno 表达成功（0）与失败。缺 errno 的响应按 data 处理。"""
        if "errno" not in payload:
            data = payload.get("data")
            return data if isinstance(data, dict) else payload
        errno = payload.get("errno")
        if str(errno) in ("0", "None"):
            return payload
        message = str(payload.get("show_msg") or payload.get("errmsg") or "").strip()
        # errtype 优先：分享失效的三种原因只有它说得清
        errtype = payload.get("errtype")
        if errtype is not None:
            exc = ERRTYPE_MAP.get(errtype)
            if exc is not None:
                raise exc(message or "分享不可访问", code=errno, raw=payload)
        raise map_error(ERRNO_MAP, errno, message or f"{action}失败", raw=payload)

    def _ensure_oauth(self) -> None:
        """access_token 快过期就用 refresh_token 换一张新的。

        百度的刷新是**轮换式**：旧 refresh_token 用完即废。回调必须落库，
        否则下次刷新直接报 invalid_grant，账号就废了。
        """
        if self.account.cookie:
            return  # cookie 模式不碰 OAuth
        if not self.account.refresh_token:
            raise ConfigError("百度账号缺少 refresh_token")
        now = int(time.time())
        if self.account.access_token and self.account.expires_at - now > 300:
            return
        resp = self.http.get(
            OAUTH_TOKEN,
            params={
                "grant_type": "refresh_token",
                "refresh_token": self.account.refresh_token,
                "client_id": self.account.extra.get("client_id", ""),
                "client_secret": self.account.extra.get("client_secret", ""),
            },
            action="刷新百度 token",
            retries=1,
        )
        token = str(resp.get("access_token") or "")
        if not token:
            raise AuthError("百度 token 刷新失败，需要重新授权")
        self.account.access_token = token
        self.account.refresh_token = str(resp.get("refresh_token") or self.account.refresh_token)
        self.account.expires_at = now + int(resp.get("expires_in") or 2592000)
        if self.on_cookie is not None:
            # 这里复用 on_cookie 回调存 token，业务层按 type 判断怎么落库
            self.on_cookie(f"__token__={token};__refresh__={self.account.refresh_token}")

    def _query_auth(self) -> dict:
        """cookie 模式补 access_token；OAuth 模式补 access_token。"""
        self._ensure_oauth()
        return {"access_token": self.account.access_token} if self.account.access_token and not self.account.cookie else {}

    def bdstoken(self) -> str:
        if self._bdstoken and time.time() - self._bdstoken_at < 3600:
            return self._bdstoken
        resp = self.http.get(
            "/api/gettemplatevariable",
            params={"fields": json.dumps(["bdstoken"])},
            action="取 bdstoken",
        )
        data = resp.get("result") if isinstance(resp.get("result"), dict) else resp
        token = str((data or {}).get("bdstoken") or "")
        if token:
            self._bdstoken = token
            self._bdstoken_at = time.time()
        return token

    # ---- 分享侧 ----

    def share_info(self, url: str, pwd: str = "") -> ShareInfo:
        from ...urls import parse

        parsed = parse(url)
        if not parsed.ok:
            raise ShareInvalidError("链接不是百度网盘分享")
        # share/init?surl=xxx 拿到的是 surl，urls.parse 补了前导 1，
        # 这里要还回去——wxlist 认的是原始 surl
        shorturl = parsed.pwd_id
        if "share/init" in url and shorturl.startswith("1"):
            shorturl = shorturl[1:]
        passcode = pwd or parsed.passcode

        resp = self.http.post(
            "/share/wxlist",
            params={
                "channel": "weixin",
                "version": "2.2.2",
                "clienttype": 25,
                "web": 1,
                **_query_auth_safe(self),
            },
            data={
                "root": 1,
                "shorturl": shorturl,
                "pwd": passcode,
                "dir": "/",
                "page": 1,
                "num": 1000,
            },
            action="读取百度分享",
        )
        body = self._ok(resp, "读取百度分享")
        entries = body.get("list") or []
        if not entries:
            raise ShareInvalidError("分享里没有文件")

        files: list[File] = []
        total = 0
        for entry in entries:
            size = int(entry.get("size") or 0)
            total += size
            files.append(
                File(
                    fid=str(entry.get("fs_id") or ""),
                    name=str(entry.get("server_filename") or ""),
                    size=size,
                    is_dir=int(entry.get("isdir") or 0) == 1,
                    updated_at=int(entry.get("server_mtime") or 0),
                )
            )
        return ShareInfo(
            platform="baidu",
            pwd_id=shorturl,
            title=str(body.get("title") or "")[:60],
            passcode=passcode,
            files=files,
            total_size=total,
            extra={
                "shareid": body.get("shareid"),
                "uk": body.get("uk"),
                "seckey": body.get("seckey") or "",
            },
        )

    def transfer(self, info: ShareInfo, to_dir: str = "") -> list[str]:
        path = (to_dir or "/").rstrip("/") or "/"
        if not path.startswith("/"):
            path = "/" + path
        fsids = [int(f.fid) for f in info.files if str(f.fid).isdigit()]
        if not fsids:
            raise ShareInvalidError("分享里没有可转存的文件")

        resp = self.http.post(
            "/share/transfer",
            params={
                "async": 1,
                "ondup": "newcopy",
                "shareid": info.extra.get("shareid", ""),
                "from": info.extra.get("uk", ""),
                "sekey": info.extra.get("seckey", ""),
                **_query_auth_safe(self),
            },
            data={"path": path, "fsidlist": json.dumps(fsids)},
            action="转存",
        )
        body = self._ok(resp, "转存")

        # 异步转存：有 extra.list 说明已经完成，否则要等 taskquery
        extra = body.get("extra") or {}
        pairs = extra.get("list") or []
        if not pairs:
            task_id = str(body.get("task_id") or "")
            if not task_id:
                raise UpstreamError("转存没有返回任务号", raw=body)
            pairs = self.wait_task(task_id, timeout=max(30.0, self.timeout * 2)).fids
            return [str(p) for p in pairs]

        return [str(item.get("to") or "") for item in pairs if item.get("to")]

    def create_share(self, fids: list[str], title: str, expire_hours: int = 0) -> ShareLink:
        period = 0 if not expire_hours else (1 if expire_hours <= 24 else 7)
        resp = self.http.post(
            "/share/set",
            params={
                "channel": "chunlei",
                "clienttype": 0,
                "app_id": 250528,
                "web": 1,
                **_query_auth_safe(self),
            },
            data={
                "period": period,
                "pwd": DEFAULT_PWD,
                "eflag_disable": "true",
                "channel_list": "[]",
                "schannel": 4,
                "fid_list": json.dumps([int(f) for f in fids if str(f).isdigit()]),
            },
            action="创建分享",
        )
        body = self._ok(resp, "创建分享")
        shorturl = str(body.get("shorturl") or "")
        if not shorturl:
            raise UpstreamError("创建分享没有返回 shorturl", raw=body)
        return ShareLink(
            url=f"{SHARE_HOST}{shorturl}?pwd={DEFAULT_PWD}",
            share_id=shorturl,
            passcode=DEFAULT_PWD,
            expired_at=int(body.get("expiretime") or 0) * 1000,
        )

    # ---- 任务 ----

    def task_status(self, task_id: str) -> TaskResult:
        resp = self.http.get(
            "/share/taskquery",
            params={"taskid": task_id},
            action="查询转存任务",
        )
        body = self._ok(resp, "查询转存任务")
        status = str(body.get("status") or "")
        done = status == "success"
        failed = status in ("failed", "canceled")
        if failed:
            raise UpstreamError(f"转存任务失败：{status}", raw=body)
        pairs = body.get("list") or []
        return TaskResult(
            task_id=task_id,
            done=done,
            fids=[str(item.get("to") or "") for item in pairs if item.get("to")],
            extra={"status": status, "raw": body},
        )

    # ---- 文件侧 ----

    def list_files(self, parent_id: str = "", page: int = 1, size: int = 100) -> list[File]:
        directory = parent_id or "/"
        if not directory.startswith("/"):
            directory = "/" + directory
        resp = self.http.get(
            "/api/list",
            params={
                "dir": directory,
                "page": page,
                "num": min(int(size), 1000),
                "order": "time",
                "desc": 1,
                **_query_auth_safe(self),
            },
            action="列目录",
        )
        body = self._ok(resp, "列目录")
        out: list[File] = []
        for entry in body.get("list") or []:
            out.append(
                File(
                    fid=str(entry.get("fs_id") or ""),
                    name=str(entry.get("server_filename") or ""),
                    size=int(entry.get("size") or 0),
                    is_dir=int(entry.get("isdir") or 0) == 1,
                    updated_at=int(entry.get("server_mtime") or 0),
                    extra={"path": entry.get("path") or ""},
                )
            )
        return out

    def delete(self, fids: list[str]) -> None:
        if not fids:
            return
        paths = [f"/{f}" for f in fids]
        resp = self.http.post(
            "/api/filemanager",
            params={
                "async": 2,
                "onnest": "fail",
                "opera": "delete",
                "bdstoken": self.bdstoken(),
                "newVerify": 1,
                "clienttype": 0,
                **_query_auth_safe(self),
            },
            data={"filelist": json.dumps(paths)},
            action="删除文件",
        )
        self._ok(resp, "删除文件")

    def keepalive(self) -> None:
        self._bdstoken_at = 0.0
        try:
            self.bdstoken()
        except NetdiskError as exc:
            log.info("百度保活失败：%s", exc)

    def probe(self) -> bool:
        try:
            return bool(self.bdstoken())
        except NetdiskError:
            return False


def _query_auth_safe(client: BaiduClient) -> dict:
    """取鉴权参数，失败不抛——cookie 模式本来就不需要 access_token。"""
    try:
        return client._query_auth()
    except NetdiskError:
        return {}
