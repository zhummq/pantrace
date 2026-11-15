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
    PasscodeRequiredError,
    PasscodeWrongError,
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
    # 分享不存在/已被删除。必须是负数：百度对分享侧用 -130，正数 130 是"文件不存在"，
    # 两者混在一起时，一条死链会被当成我们账号的文件问题，排查方向整个跑偏
    -130: ShareInvalidError,
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

#: 新版分享接口（wxlist）用字符串 errtype，"mis_105" = 分享已删除/被取消。
#: 实测死链返回 `{errno:-130, errtype:"mis_105"}`；老代码只认整数 key，
#: 于是这类响应掉进 "上游失败（code=-130）"，看不出是链接废了
_ERRTYPE_PREFIX = "mis_"

#: "mispwd-9" = 提取码缺失**或**错误。百度对这两种情况的返回一模一样：
#: 空码和错码都是 `{errno:-130, errtype:"mispwd-9", data:{list:[]}}`
#: （实测，别指望从响应里分辨），只能由调用方按"有没有传码"来定。
_ERRTYPE_MISPWD = "mispwd"


@register
class BaiduClient(PanClient):
    type = "baidu"
    display = "百度网盘"
    max_file_bytes = 100 * 1024 ** 3

    def __init__(self, account: Account, *, timeout: float = 15.0, on_cookie=None):
        self._bdstoken = ""
        self._bdstoken_at = 0.0
        self._uk = ""
        #: 已经确认存在的目录，一次会话里只查一遍
        self._ensured_dirs: set[str] = set()
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
        if isinstance(errtype, str) and errtype.startswith(_ERRTYPE_MISPWD):
            # 缺码还是错码，这一层判不了（百度两种回得一模一样），先按"缺码"抛，
            # share_info 知道调用方有没有传码，再改判成"码不对"。
            raise PasscodeRequiredError(
                "这条分享需要提取码", code=errno, raw=payload
            )
        if errtype is not None:
            exc = ERRTYPE_MAP.get(errtype)
            if exc is not None:
                raise exc(message or "分享不可访问", code=errno, raw=payload)
            if isinstance(errtype, str) and errtype.startswith(_ERRTYPE_PREFIX):
                raise ShareInvalidError(
                    f"分享已失效（{errtype}）：可能被删除、取消或链接本身不对",
                    code=errno,
                    raw=payload,
                )
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
        try:
            body = self._ok(resp, "读取百度分享")
        except PasscodeRequiredError as exc:
            # 传了码还退回来，那就是码不对。这条分享本身是活的——别让上层
            # 把它当死链下架，也别把账算到账号头上。
            if passcode:
                raise PasscodeWrongError(
                    f"提取码 {passcode} 不对，分享本身还在", code=exc.code, raw=exc.raw
                ) from exc
            raise
        # wxlist 的成功响应把业务字段包在 data 里：
        #   {"errno":0, "data":{"list":[...], "title":..., "uk":..., "shareid":..., "seckey":...}}
        # 老代码直接在顶层取 list，拿到的是 None——于是**每一条正常的分享**都被判成
        # "分享里没有文件"，后台手工验链全是红的。这里是实测踩出来的，别再改回顶层。
        if isinstance(body.get("data"), dict):
            body = body["data"]
        entries = body.get("list") or []
        if not entries:
            # errno=0 但没文件：提取码不对时百度也这么回（不是每个接口都肯给错误码）
            raise ShareInvalidError("分享里没有文件（提取码可能不对，或分享已被清空）")

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

    def _dir_exists(self, path: str) -> bool:
        """目录在不在。filemetas 对不存在的路径回 errno=12，那不是异常，是答案。"""
        resp = self.http.get(
            "/api/filemetas",
            params={
                "target": json.dumps([path], ensure_ascii=False, separators=(",", ":")),
                "dlink": 0,
                "web": 1,
                "bdstoken": self.bdstoken(),
            },
            action="查目录",
        )
        if int(resp.get("errno") or 0) != 0:
            return False
        info = resp.get("info") or []
        return bool(info) and int(info[0].get("isdir") or 0) == 1

    def _ensure_dir(self, path: str) -> None:
        """转存目标目录不存在就建出来，逐级建。

        百度 /api/create **不是幂等的**：路径已存在时再建一次，它会给你一个
        "pantrace_20261115_180205" 这样的新目录（实测）。所以每级都必须先查
        再建，绝不能图省事直接 create。

        原先这里什么都不做，全靠用户自己去网页端把临时目录建好；没建就直接
        转存，百度回一句「转存路径不存在（code=2）」，用户看着像配置错了。
        目录该由我们负责。
        """
        path = "/" + path.strip("/")
        if path == "/" or path in self._ensured_dirs:
            return
        segments = [s for s in path.split("/") if s]
        for depth in range(1, len(segments) + 1):
            sub = "/" + "/".join(segments[:depth])
            if self._dir_exists(sub):
                continue
            resp = self.http.post(
                "/api/create",
                params={"bdstoken": self.bdstoken(), "clienttype": 0, "web": 1},
                data={"path": sub, "isdir": 1, "block_list": "[]"},
                action="建转存目录",
            )
            self._ok(resp, "建转存目录")
            created = str(resp.get("path") or resp.get("name") or "")
            if created and created != sub:
                # create 撞名会改名，说明有人抢在前面建了同名目录，或者我们的
                # 存在性判断过期了。不纠正的话文件会落进意料之外的目录。
                raise UpstreamError(
                    f"建目录时被改名（{sub} → {created}），转存已中止", raw=resp
                )
        self._ensured_dirs.add(path)

    def transfer(self, info: ShareInfo, to_dir: str = "") -> list[str]:
        path = (to_dir or "/").rstrip("/") or "/"
        if not path.startswith("/"):
            path = "/" + path
        # 目标目录由后台「临时目录」配置来，写 pantrace 就该有 /pantrace
        self._ensure_dir(path)
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
        # 百度分享侧（channel=weixin）习惯把业务字段包一层 data。顶层找不到
        # extra/task_id 就往 data 里再找一次——否则会误报"转存没有返回任务号"，
        # 而接口其实成功、文件也已经进盘了，只是我们没看见
        if not body.get("extra") and not body.get("task_id") and isinstance(body.get("data"), dict):
            body = body["data"]

        # 异步转存：有 extra.list 说明已经完成，否则要等 taskquery
        extra = body.get("extra") or {}
        pairs = extra.get("list") or []
        if not pairs:
            task_id = str(body.get("task_id") or "")
            if not task_id:
                raise UpstreamError("转存没有返回任务号", raw=body)
            pairs = self.wait_task(task_id, timeout=max(30.0, self.timeout * 2)).fids
            return [_target_path(p) for p in pairs]

        return [_target_path(item.get("to")) for item in pairs if item.get("to")]

    def create_share(self, fids: list[str], title: str, expire_hours: int = 24) -> ShareLink:
        # period 只认三档：0=永久、1=1天、7=7天。**0 不再使用**：这个参数以前
        # 从没被上层传过，默认 0 就等于给每条资源建永久分享——回收任务一旦没跑
        # （进程挂了、mirrors 行丢了），分享会一直挂在我们账号上，外人随时能打开，
        # 本地也没有 fids 可删。传 0/负数一律按 1 天建，不把默认值交回上游语义
        hours = int(expire_hours or 0)
        period = 7 if hours > 24 else 1
        # 这里收的可能是 fs_id，也可能是 transfer 返回的**路径**。
        # /share/set 两个字段都认：fid_list 收数字 id，path_list 收绝对路径，
        # 但绝不能不分类就往 fid_list 里塞——isdigit() 一过滤就成了空数组，
        # 百度只会回一句 "path_list or fid_list param need"，看不出真正原因。
        fid_ids: list[int] = []
        paths: list[str] = []
        for item in fids:
            text = str(item or "").strip()
            if not text:
                continue
            if text.isdigit():
                fid_ids.append(int(text))
            else:
                paths.append(text if text.startswith("/") else "/" + text)
        form: dict[str, object] = {
            "period": period,
            "pwd": DEFAULT_PWD,
            "eflag_disable": "true",
            "channel_list": "[]",
            "schannel": 4,
        }
        if fid_ids:
            form["fid_list"] = json.dumps(fid_ids)
        if paths:
            form["path_list"] = json.dumps(paths)
        if not fid_ids and not paths:
            raise UpstreamError("没有可分享的文件（转存没有返回目标路径）")

        resp = self.http.post(
            "/share/set",
            params={
                "channel": "chunlei",
                "clienttype": 0,
                "app_id": 250528,
                "web": 1,
                **_query_auth_safe(self),
            },
            data=form,
            action="创建分享",
        )
        body = self._ok(resp, "创建分享")
        if not body.get("shorturl") and isinstance(body.get("data"), dict):
            body = body["data"]
        shorturl = str(body.get("shorturl") or body.get("link") or "").strip()
        if not shorturl:
            raise UpstreamError("创建分享没有返回 shorturl", raw=body)
        # 现在的 /share/set 的 shorturl 回的是**完整 URL**
        # （"https://pan.baidu.com/s/1F9Uxu_..."），老版本只回尾段。
        # 一律拼域名就会得到 "https://pan.baidu.com/s/https://..."，
        # 这个链接自己都打不开，回读还会报 code=2。
        base = shorturl if shorturl.startswith("http") else f"{SHARE_HOST}{shorturl}"
        sep = "&" if "?" in base else "?"
        url = base if "pwd=" in base else f"{base}{sep}pwd={DEFAULT_PWD}"
        share_id = base.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        return ShareLink(
            url=url,
            share_id=share_id,
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
        if not body.get("status") and isinstance(body.get("data"), dict):
            body = body["data"]
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


def _target_path(value: object) -> str:
    """转存结果里的 `to` 是**目标路径**，不是 fs_id。

    实测：转存目录分享后 `extra.list[].to == "/复仇者联盟4：终局之战 (2019)(1)"`。
    老代码把它当 fs_id 往下传，`create_share` 里 `isdigit()` 一过滤就空了，
    百度回 "path_list or fid_list param need"——错在转存之后，看着却像参数没拼对。

    统一成"相对根目录、不带前导斜杠"：`delete()` 那边是 `f"/{f}"`，
    mirrors 里存的也是这一形态。万一是数字（fs_id），原样留着给 fid_list 用。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    return text.lstrip("/") or "/"
