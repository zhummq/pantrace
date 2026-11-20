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

import base64
import json
import logging
import re
import time

from ... import refresh
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

# 迅雷的资源 ID 长这样：VP6UOc2WqXgWNUa17d0f_bI8A1（V 开头 20-30 位）
_DIR_ID_RE = re.compile(r"V[A-Za-z0-9_-]{20,30}")


def _user_id_from_jwt(token: str) -> str:
    """从 access_token（JWT）的 payload 里读 user_id，不验签。

    为什么不验签：这里只是要一个**标识**，不是用它做鉴权决策。签名校验由
    上游在每次请求时做，我们解码 payload 纯粹是为了给 captcha 的 meta.user_id
    填一个真值——读不懂就返回空串，让调用方退回 "0"，绝不让解析失败影响转存。

    user_id 以前只在 token **刷新时**才从 refresh 响应拿，导致新建 client 且
    token 未过期时它一直是空串，建目录的 captcha 带 "0" 被 upstream 判
    captcha_invalid。JWT payload 里本来就有，直接读，不用等刷新。
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1] + "=" * (-len(parts[1]) % 4)  # base64url 补 padding
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if not isinstance(claims, dict):
            return ""
        for key in ("user_id", "sub", "uid", "userId"):
            value = claims.get(key)
            if value:
                return str(value)
    except Exception:  # noqa: BLE001 - 解码失败就当没拿到，绝不影响主流程
        pass
    return ""


@register
class XunleiClient(PanClient):
    type = "xunlei"
    display = "迅雷云盘"

    def __init__(self, account: Account, *, timeout: float = 15.0, on_cookie=None):
        self._access_token = account.access_token or ""
        self._expires_at = account.expires_at or 0
        self._captchas: dict[str, tuple[str, float]] = {}
        self._temp_dir_id = ""
        self._temp_dir_name = ""
        # 直接从 access_token 解 user_id 作初值：以前这里写死空串，只有 token
        # 刷新时才会被赋值。新建 client + token 未过期 → 一直空 → captcha 带
        # "0" → 建目录被拒。水印是新增的建目录动作，把这个坑稳定触发了。
        self._user_id = _user_id_from_jwt(self._access_token)
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
        # 刷新按账号串行：refresh_token 轮换，两个线程各拿一份旧的去换，
        # 后到的那个会被判 invalid_grant，严重时整条 token family 被吊销。
        # 见 sdk/refresh.py 的说明
        key = int(self.account.id or 0)
        with refresh.lock_for(key):
            hit = refresh.reuse(key, now)
            if hit is not None:
                # 等锁期间别人刚刷好了，直接用，别再换一次
                self._access_token, self._expires_at = hit[0], int(hit[1])
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
            refresh_token = str(resp.get("refresh_token") or "")
            if not token:
                raise AuthError("迅雷 token 刷新失败，需要重新授权")
            self._access_token = token
            self._expires_at = now + int(resp.get("expires_in") or 3600) - 60
            # 写操作（如建目录）的 captcha 会校验 meta.user_id，传 "0" 会被
            # captcha_invalid 拒掉（实测）；读操作不挑。刷新响应里有真实 user_id。
            # 只在响应给了值时才覆盖：JWT 里解出来的可能是更全的 user_id，
            # 别让一个缺字段的 refresh 响应把它清回空串。
            refreshed_uid = str(resp.get("user_id") or resp.get("sub") or "")
            if refreshed_uid:
                self._user_id = refreshed_uid
            self.account.access_token = token
            if refresh_token:
                self.account.refresh_token = refresh_token
            if self.on_cookie is not None:
                # __expires__ 必须带上：不带的话 pan._credential_hook 只能按 12 小时
                # 兜底写库，而迅雷 access_token 实际只有 expires_in（一般 1 小时）。
                # 结果是库里记着"还有 11 小时才过期"，`_token()` 于是一直拿死 token
                # 去请求，全量 401，且永远不会触发刷新——账号表现成"配好第二天就用不了"。
                self.on_cookie(
                    f"__token__={token};__refresh__={refresh_token}"
                    f";__expires__={self._expires_at}"
                )
            # 放在 on_cookie 之后：回写走 update_account，它会 forget 掉缓存
            refresh.remember(key, token, self._expires_at)
        return self._access_token

    def _captcha(self, action: str, renew: bool = False) -> str:
        # 最后一道兜底：token 在实例生命周期内被换过（构造时还没有、后来刷新
        # 拿到）而 _user_id 没同步时，现解一次。空着传 "0" 会被写操作拒。
        if not self._user_id:
            self._user_id = _user_id_from_jwt(self._access_token)
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
        captcha_renew: bool = False,
    ) -> dict:
        """带 Authorization + x-captcha-token 的业务请求。

        gone_error：分享侧 404/410 该抛什么。不传的话 http 层只能报裸
        "HTTP 404"，后台验链看不出是源链接死了还是账号挂了。

        401 会自动重试一次：token 被上游吊销、或在别处被轮换掉，库里却还写着
        没过期——不丢缓存重刷的话，这一小时内每次请求都拿同一个死 token 撞墙。
        """
        try:
            return self._request_once(
                method, path, query=query, body=body,
                action=action, gone_error=gone_error, captcha_renew=captcha_renew,
            )
        except (AuthError, UpstreamError) as exc:
            if getattr(exc, "code", None) != 401:
                raise
            log.warning("%s 请求被判未授权（401），丢掉缓存 token 重刷一次", self.display)
            self._access_token = ""
            self._expires_at = 0
            refresh.forget(int(self.account.id or 0))
            return self._request_once(
                method, path, query=query, body=body,
                action=action, gone_error=gone_error, captcha_renew=captcha_renew,
            )

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
        action: str = "",
        gone_error: type[NetdiskError] | None = None,
        captcha_renew: bool = False,
    ) -> dict:
        action_key = f"{method.upper()}:{path}"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "x-captcha-token": self._captcha(action_key, renew=captcha_renew),
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

    def _resolve_transfer_parent(self, to_dir: str = "") -> str:
        """解析 restore 的 parent_id，必须落到真实目录 ID 上。

        后台「临时目录」填名字（如 pantrace）是最自然的写法——百度那边填的
        就是路径。但迅雷的 restore 把 parent_id 当目录 ID 用，名字不是 ID，
        接口直接 404，报错文案还长得和"源链接失效"一模一样，排查会完全走偏
        （实测踩过：本地复现 parent_id='pantrace' → 404，换成真实 ID → 200）。

        统一收口：配置长得像 ID 就直接用；否则当目录名，找根目录下同名目录，
        没有就建；都空则用默认名 pantrace。
        """
        conf = (to_dir or self.account.temp_dir or "").strip().strip("/")
        if conf and _DIR_ID_RE.fullmatch(conf):
            return conf
        name = conf or "pantrace"
        if self._temp_dir_id and self._temp_dir_name == name:
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
                and str(item.get("name") or "") == name
            ):
                self._temp_dir_id, self._temp_dir_name = fid, name
                return fid
        resp = self._call(
            "POST",
            "/drive/v1/files",
            body={"kind": "drive#folder", "name": name, "parent_id": "", "space": ""},
            action="建转存目录",
        )
        fid = str((resp.get("file") or {}).get("id") or "")
        if not fid:
            raise UpstreamError(f"迅雷建转存目录 {name} 没返回目录 id", raw=resp)
        self._temp_dir_id, self._temp_dir_name = fid, name
        return fid

    def delete_watermark(self, name: str, to_dir: str = "") -> bool:
        """改名时清理旧水印目录：在落点目录找同名文件夹，找到就删。"""
        parent = self._resolve_transfer_parent(to_dir)
        wid = self.find_child_dir(parent, name)
        if not wid:
            return False
        try:
            self.delete([wid])
            log.info("%s 已清理旧水印目录「%s」（id=%s）", self.display, name[:24], wid)
            return True
        except NetdiskError as exc:
            log.info("%s 清理旧水印目录「%s」失败：%s", self.display, name[:24], exc)
            return False

    def make_watermark(self, name: str, to_dir: str = "") -> str:
        """引流水印：在转存落点目录里建一个空文件夹，返回它的 fid。

        parent 复用 _resolve_transfer_parent——它和 transfer 拿到的是同一个
        落点目录，水印才会和转存项同级、一起进分享、一起被回收。
        """
        parent = self._resolve_transfer_parent(to_dir)
        # 和夸克同理：同名已存在会撞名，先查到就复用，别让它拖累水印进 fids
        existing = self.find_child_dir(parent, name)
        if existing:
            return existing
        try:
            resp = self._call(
                "POST",
                "/drive/v1/files",
                body={"kind": "drive#folder", "name": name, "parent_id": parent, "space": ""},
                action="建水印目录",
                # 强制重新取 captcha：建转存目录和建水印走的是同一个 action key
                # (POST:/drive/v1/files)，缓存的 token 可能被上游用过一次就失效。
                captcha_renew=True,
            )
            return str((resp.get("file") or {}).get("id") or "")
        except (AuthError, UpstreamError) as exc:
            # 排查留痕：captcha 这类 400 以前日志只有一句"验证码无效"，分不清
            # 是 user_id 没拿到还是 token 复用。带上真实 user_id 一眼就能定位。
            log.warning(
                "%s 建水印目录失败 user_id=%s parent=%s action=%s：%s",
                self.display, self._user_id or "(空)", parent, "POST:/drive/v1/files", exc,
            )
            # 查完到建之间被并发抢先建了：再列一次捞已存在目录的 id。
            # 只兜 UpstreamError（撞名在这类里）；AuthError 是登录态问题，别白查。
            if isinstance(exc, UpstreamError):
                return self.find_child_dir(parent, name)
            raise

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
        # parent_id 必须是真实目录 ID：名字/空串都先解析（见 _resolve_transfer_parent）
        parent = self._resolve_transfer_parent(to_dir)
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
        return bool(self.probe_detail().get("ok"))

    def probe_detail(self) -> dict:
        """真发一次请求，并把失败原因原样带出去。

        以前这里只调 `_token()`，有两个毛病，2026-11 迅雷账号"后台显示正常、
        实际全 401"就是它们叠出来的：

        1. **不发网络**。库里 `expires_at` 还没到时 `_token()` 直接返回缓存
           token，一次请求都不打，于是账号早废了也判 True。
        2. **吞掉原因**。异常被压成 False，上层只能回一句"账号登录态不可用"，
           分不清是 refresh_token 被吊销、目录没权限还是网络不通。

        现在 token 和列根目录两步分开报，`detail` 里带 token 剩余秒数。
        """
        detail: dict[str, object] = {}
        try:
            self._token()
        except NetdiskError as exc:
            return {"ok": False, "reason": str(exc), "detail": {**detail, "stage": "token"}}
        detail["tokenLeft"] = max(0, int(self._expires_at - time.time()))
        try:
            files = self.list_files("", 1, 1)
        except NetdiskError as exc:
            return {"ok": False, "reason": str(exc), "detail": {**detail, "stage": "list"}}
        detail["rootFiles"] = len(files)
        return {"ok": True, "reason": "", "detail": detail}


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
