"""百度网盘客户端。

它和别的盘最大的不同：**两套凭据，各管一段，谁也不能替谁**。

- Cookie（BDUSS）：读分享、转存、建分享。网页端接口只认它。
- OAuth（access_token）：自己盘里的文件管理——容量、列目录、批量元信息、
  删除。官方 OpenAPI 只认它，实现见 `openapi.py`。

**没有"二选一"这回事**：只填 token 转不了存，只填 cookie 用不了官方接口。
项目要的是转存，所以 cookie 是必填项，OAuth 是加分项（巡检和清理更稳）。

百度还要求很多网页接口带 `bdstoken`，它跟着 cookie 走，所以要单独取一次并
缓存。form-urlencoded 而不是 JSON，这点也别改：百度那套老接口对 JSON body
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
    message_of,
)
from ...http import Requester
from ...registry import register
from ...types import Account, File, ShareInfo, ShareLink, TaskResult
from .openapi import BaiduOpenApi

log = logging.getLogger("pantrace.sdk.baidu")

API = "https://pan.baidu.com"
#: OAuth 的 token 端点随 OpenAPI 一起挪到了 openapi.py，这里别再加回来
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
        # OAuth 那条线：只管盘内文件管理（列目录/元信息/删除/容量）。
        # 它和转存用的 cookie 是两套凭据，别指望 token 能转存——百度网页端
        # 接口只认 BDUSS，这一点竞品自己也是这么分的（fs_openapi vs fs_share_api）。
        self.openapi = BaiduOpenApi(
            account,
            timeout=timeout,
            on_token=self._on_token if on_cookie is not None else None,
        )

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

    def _on_token(self, info: dict) -> None:
        """把续期结果交给业务层落库。

        复用 on_cookie 的 `__token__` 约定，业务层不用认识第二种回调形态。
        百度的 refresh_token 是**轮换式**的：旧的用完即废，不落库下次刷新
        直接 invalid_grant，账号就废了——所以这个回调不是可选的装饰。
        """
        if self.on_cookie is None:
            return
        self.on_cookie(
            f"__token__={info.get('access_token', '')};"
            f"__refresh__={info.get('refresh_token', '')};"
            f"__expires__={int(info.get('expires_at') or 0)}"
        )

    def _ensure_oauth(self) -> None:
        """access_token 快过期就用 refresh_token 换一张新的。

        实现挪到了 `openapi.ensure_token()`：那边才知道 client_id/secret
        缺没缺。这里只做"cookie 模式不碰 OAuth"的分流。
        """
        if self.account.cookie:
            return  # cookie 模式不碰 OAuth
        if not (self.account.access_token or self.account.refresh_token):
            raise ConfigError("百度账号缺少 access_token / refresh_token")
        self.openapi.ensure_token()

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
        # 以前这里静默返回空串，probe 只能回一句"登录态不可用"——排查时分不清
        # 是 cookie 掉了、还是接口改了。OAuth 模式下这个接口确实回空（它跟着
        # cookie 走），所以要把话说清楚。
        errno = resp.get("errno")
        if str(errno) in ("0", "None"):
            raise AuthError(
                "取 bdstoken 失败：返回空值。这个值跟着 Cookie 走，"
                "OAuth token 拿不到——网页端转存必须填 BDUSS",
                raw=resp,
            )
        if str(errno) == "-6":
            raise AuthError(
                "百度 Cookie 已失效（errno -6）：BDUSS 多半被踢了，重新登录 pan.baidu.com 复制一条",
                code=errno,
                raw=resp,
            )
        raise map_error(ERRNO_MAP, errno, message_of(resp) or "取 bdstoken 失败", raw=resp)

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

    def close(self) -> None:
        """两个 Requester（网页端 / 官方 OpenAPI）都要关。"""
        super().close()
        try:
            self.openapi.close()
        except Exception:  # noqa: BLE001
            pass

    def probe(self) -> bool:
        return bool(self.probe_detail().get("ok"))

    def probe_detail(self) -> dict:
        """两套凭据分开测，各自报结果——竞品的 check-account 也是这么干的。

        `ok` 只看 cookie。这个项目要的是**转存**，而百度网页端接口只认 BDUSS；
        OAuth 通了只说明"盘内文件管理可用"，说明不了能转存，所以它在 detail
        里单列、不参与判定。

        反过来，只有 OAuth 没有 cookie 时必须判死：否则账号会被排进轮换，
        每次点击都失败一次，还把 fail_count 加满——那比不配账号更坑。
        """
        detail: dict[str, str] = {"cookie": "未配置", "oauth": "未配置"}
        notes: list[str] = []
        cookie_ok = False

        if self.account.cookie:
            try:
                if self.bdstoken():
                    detail["cookie"] = "ok"
                    cookie_ok = True
                else:
                    detail["cookie"] = "fail: bdstoken 为空"
            except NetdiskError as exc:
                detail["cookie"] = f"fail: {exc}"

        if self.openapi.configured:
            try:
                info = self.openapi.uinfo()
                who = str(info.get("netdisk_name") or info.get("baidu_name") or "")
                detail["oauth"] = f"ok（{who}）" if who else "ok"
            except NetdiskError as exc:
                detail["oauth"] = f"fail: {exc}"
                notes.append(f"OAuth 不可用：{exc}")

        if not self.account.cookie:
            notes.insert(0, "缺少 Cookie：百度转存走网页端接口，只认 BDUSS，OAuth token 转不了存")
        elif not cookie_ok:
            notes.insert(0, f"Cookie 不可用：{detail['cookie'].removeprefix('fail: ')}")

        return {"ok": cookie_ok, "reason": "；".join(notes), "detail": detail}


def _query_auth_safe(client: BaiduClient) -> dict:
    """取鉴权参数，失败不抛——cookie 模式本来就不需要 access_token。"""
    try:
        return client._query_auth()
    except NetdiskError:
        return {}
