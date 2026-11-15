"""百度网盘官方 OpenAPI 客户端（OAuth 模式）。

对应竞品 `xiageba2/packages/netdisk-sdk-js/packages/baidu-sdk/src/fs_openapi.ts`。

**先说清定位，别搞混。这套东西不负责转存。**

百度官方 OpenAPI 没有"转存他人分享"这个能力——竞品也一样：它的
`/share/wxlist`、`/share/transfer`、`/share/set` 全在 `fs_share_api.ts` 里，
走的是网页端接口 + BDUSS Cookie。所以百度账号天生是"双持"：

| 凭据 | 负责 | 对应竞品文件 |
|---|---|---|
| Cookie（BDUSS） | 读分享、转存、建分享 | fs_api.ts / fs_share_api.ts |
| OAuth（access_token） | 自己盘里的文件管理：容量、列目录、批量元信息、删除 | fs_openapi.ts |

OAuth 这条路换来的是：不依赖 bdstoken、不占网页风控、refresh_token 能自动续。
后台巡检和临时文件清理走它，比 cookie 稳得多——cookie 掉了要人肉重新登录，
而 refresh_token 能自己撑 30 天。

**和竞品的关键差别**：竞品有自己申请的应用（client_id/secret + 绑定授权目录），
所以 `listall("/")` 是通的。我们如果只是拿了一个没开文件权限的 token，百度会回
`20020 该路径不在允许的访问范围内` 或 `-7`。这不是代码错，是应用权限没开，
所以下面把它映射成 ConfigError 并原样带上百度的话——别让人误以为是 cookie 又掉了。

接口形态都是踩过的：
- `xpan/multimedia?method=listall|filemetas` 是 **POST + form-urlencoded**
- `xpan/file?method=filemanager&opera=...` 也是 **POST + form**
- `xpan/nas?method=uinfo`、`api/quota` 是 GET
- **UA 必须是 `pan.baidu.com`**，换成浏览器 UA 会被拒
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ...errors import (
    AuthError,
    ConfigError,
    NetdiskError,
    NotFoundError,
    TaskTimeoutError,
    UnsupportedError,
    UpstreamError,
    message_of,
)
from ...http import Requester
from ...types import Account, File

log = logging.getLogger("pantrace.sdk.baidu.openapi")

API = "https://pan.baidu.com"
OAUTH_TOKEN = "https://openapi.baidu.com/oauth/2.0/token"

#: xpan 的错误码和网页端那套不是一回事，别和 client.ERRNO_MAP 混用。
#: 表里只放能一眼定性的码，需要看上下文的（-7 / 20020）在 _check 里单独判。
OPENAPI_ERRNO_MAP: dict[object, type[NetdiskError]] = {
    -6: AuthError,          # access_token 无效/过期
    4: TaskTimeoutError,    # 请求超时，可重试
    12: UpstreamError,      # 批量操作部分失败
    31066: UpstreamError,   # 命中接口频率限制，退避重试，别当成账号坏了
}

#: 这些码说明"应用没这个权限"，换 token 没用，得去开放平台改配置
_PERMISSION_ERRNOS = (20020, 31064, 31034)


class BaiduOpenApi:
    """百度官方 OpenAPI 的薄封装。

    刻意不和 `BaiduClient` 共用一个 Requester：主客户端的 UA 是浏览器 UA
    （网页接口要），xpan 要的是 `pan.baidu.com`；而且 cookie 模式下不该把
    BDUSS 一起带到官方接口上。多一个 Session 的代价，比调错一次小。
    """

    def __init__(
        self,
        account: Account,
        *,
        timeout: float = 15.0,
        on_token=None,
    ):
        self.account = account
        self.timeout = timeout
        #: 续期成功后回调，业务层负责落库（百度是轮换式 refresh_token，
        #: 不落库下次刷新直接 invalid_grant，账号就废了）
        self.on_token = on_token
        self.http = Requester(
            API,
            headers={
                "User-Agent": "pan.baidu.com",
                "Referer": "https://pan.baidu.com/",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=timeout,
        )

    # ---- 凭据 ----

    @property
    def _client_id(self) -> str:
        return str(self.account.extra.get("client_id") or "").strip()

    @property
    def _client_secret(self) -> str:
        return str(self.account.extra.get("client_secret") or "").strip()

    @property
    def configured(self) -> bool:
        """有没有可用的一套 OAuth 凭据（token 或 可续期的 refresh_token）。"""
        if not (self.account.access_token or self.account.refresh_token):
            return False
        # 只有 refresh_token 也够：能换出 access_token
        if self.account.refresh_token and (self._client_id and self._client_secret):
            return True
        return bool(self.account.access_token)

    def ensure_token(self) -> str:
        """返回一个还没过期的 access_token，需要时用 refresh_token 换。

        这里抛 ConfigError 而不是静默失败：百度这条链路上缺 client_id/secret
        是最常见的配置错误，含糊地返回空串只会让人去怀疑 cookie。
        """
        now = int(time.time())
        if self.account.access_token and self.account.expires_at - now > 300:
            return self.account.access_token

        if not self.account.refresh_token:
            if self.account.access_token:
                # 没有到期时间但也没法续，先拿去用，让上游自己判过期
                return self.account.access_token
            raise ConfigError("百度 OAuth 模式缺少 access_token / refresh_token")
        if not (self._client_id and self._client_secret):
            raise ConfigError(
                "百度 OAuth 续期需要 client_id + client_secret（开放平台应用凭据），"
                "当前账号没配。只填 token 的话，30 天后就再也续不上了"
            )

        resp = self.http.get(
            OAUTH_TOKEN,
            params={
                "grant_type": "refresh_token",
                "refresh_token": self.account.refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            action="刷新百度 OAuth token",
            retries=1,
        )
        token = str(resp.get("access_token") or "")
        if not token:
            detail = message_of(resp) or "上游没返回 access_token"
            raise AuthError(f"百度 token 续期失败：{detail}，需要重新授权")

        refresh = str(resp.get("refresh_token") or self.account.refresh_token)
        expires_at = now + int(resp.get("expires_in") or 2592000)
        self.account.access_token = token
        self.account.refresh_token = refresh
        self.account.expires_at = expires_at
        if self.on_token is not None:
            try:
                self.on_token({"access_token": token, "refresh_token": refresh, "expires_at": expires_at})
            except Exception:  # noqa: BLE001
                log.warning("百度 OAuth 凭据回写失败，下次续期可能失效")
        return token

    # ---- 请求层 ----

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        action: str,
        probe_path: str = "",
    ) -> dict:
        token = self.ensure_token()
        merged = {"access_token": token, **(params or {})}
        payload = self.http.request(
            method,
            path,
            params=merged,
            data=data,
            action=action,
        )
        return self._check(payload, action, probe_path=probe_path)

    def _check(self, payload: dict, action: str, *, probe_path: str = "") -> dict:
        errno = payload.get("errno")
        if errno is None or str(errno) == "0":
            return payload

        message = message_of(payload)
        code = errno

        # 权限类：换 token、换账号都没用，是开放平台那边没开
        if errno in _PERMISSION_ERRNOS:
            raise ConfigError(
                f"{action}失败：应用没有该目录的访问权限（errno={errno}）"
                f"{'：' + message if message else ''}。"
                "百度开放平台要在应用里开通文件权限并绑定授权目录，"
                "个人应用默认只能在 /apps/<应用名> 下活动",
                code=code,
                raw=payload,
            )

        # -7 在根目录上基本等于"没权限"，在具体路径上才是"不存在"
        if errno == -7:
            if probe_path in ("", "/"):
                raise ConfigError(
                    f"{action}失败：应用无权访问根目录（errno=-7）。"
                    "检查开放平台的应用权限与授权目录；只做查询的 token 通常是这个原因",
                    code=code,
                    raw=payload,
                )
            raise NotFoundError(f"{action}失败：路径不存在或无权限", code=code, raw=payload)

        exc = OPENAPI_ERRNO_MAP.get(errno)
        if exc is not None:
            raise exc(message or f"{action}失败", code=code, raw=payload)
        raise UpstreamError(message or f"{action}失败：errno={errno}", code=code, raw=payload)

    def close(self) -> None:
        self.http.close()

    # ---- 只读 ----

    def uinfo(self) -> dict:
        """用户信息。最轻的一个接口，拿来做 OAuth 存活探测。"""
        return self._request(
            "GET", "/rest/2.0/xpan/nas", params={"method": "uinfo"}, action="读取百度账号信息"
        )

    def quota(self) -> dict:
        """容量。`total` / `used` / `free` 单位都是字节。"""
        return self._request(
            "GET",
            "/api/quota",
            params={"checkfree": "1", "checkexpire": "1"},
            action="读取百度容量",
        )

    def listall(
        self,
        path: str = "/",
        *,
        start: int = 0,
        limit: int = 1000,
        recursion: int = 0,
    ) -> list[File]:
        """递归列目录（`xpan/multimedia?method=listall`）。

        竞品用它扫临时目录找新转存进来的文件——转存接口只回 fid，
        要拿文件名/路径/大小就得靠这个补。
        """
        resp = self._request(
            "POST",
            "/rest/2.0/xpan/multimedia",
            params={
                "method": "listall",
                "path": path,
                "start": start,
                "limit": min(int(limit), 10000),
                "recursion": recursion,
                "web": 1,
            },
            action=f"列目录 {path}",
            probe_path=path,
        )
        out: list[File] = []
        for entry in resp.get("list") or []:
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

    def filemetas(self, fsids: list[str], *, dlink: bool = False) -> list[dict]:
        """批量查文件元信息，`dlink=True` 时连下载地址一起给。"""
        ids = [int(f) for f in fsids if str(f).isdigit()]
        if not ids:
            return []
        resp = self._request(
            "POST",
            "/rest/2.0/xpan/multimedia",
            params={
                "method": "filemetas",
                "fsids": json.dumps(ids),
                "dlink": 1 if dlink else 0,
            },
            action="查询文件元信息",
        )
        return list(resp.get("info") or [])

    # ---- 写 ----

    def delete(self, paths: list[str]) -> None:
        """按路径删文件（进回收站）。路径要带前导 `/`，和网页端不一样。"""
        if not paths:
            return
        normalized = [p if p.startswith("/") else "/" + p for p in paths]
        self._request(
            "POST",
            "/rest/2.0/xpan/file",
            params={"method": "filemanager", "opera": "delete"},
            data={"async": 0, "onnest": "fail", "filelist": json.dumps(normalized)},
            action="删除文件",
        )

    def delete_fids(self, fsids: list[str]) -> None:
        """按 fid 删：先查元信息拿路径，再删。

        清理任务手上只有 fid，`filemanager` 只认路径——这一步是竞品
        `clean_temp.ts` 里省掉的（它直接持有路径），我们两边都对得上。
        """
        ids = [f for f in fsids if str(f).isdigit()]
        if not ids:
            return
        metas = self.filemetas(ids)
        paths = [str(m.get("path") or "") for m in metas if m.get("path")]
        if not paths:
            raise NotFoundError("按 fid 删除失败：查不到对应路径")
        self.delete(paths)


def as_openapi(client) -> BaiduOpenApi:
    """从 BaiduClient 上取 openapi 实例；没配就抛 UnsupportedError。

    给清理/巡检这类"可选能力"用：调用方拿到异常就当这个账号没有 OAuth，
    跳过它，不影响 cookie 那条主链路。
    """
    api = getattr(client, "openapi", None)
    if api is None or not api.configured:
        raise UnsupportedError("这个百度账号没配 OAuth 凭据")
    return api
