"""夸克 / UC 客户端。

七步流程（取 stoken → 列文件 → 转存 → 等任务 → 建分享 → 等分享 → 取提取码）
原样保留，但每步都拆成了可单独测的方法，错误码也按语义分类了。

三个必须保留的约束：

1. **cookie 会自己变**。夸克每次响应都可能下发新的 `__puus`，不接就会在
   半小时后集体 401。这里在请求层回收并回写账号表。
2. **转存是异步的**。`save` 只返回 task_id，不等任务完成就建分享会得到一个
   空分享——线上表现为"点开是空的"，比报错更难查。
3. **分享到期时间要回读**。`expired_type=4` 到底是不是永久，各处文档说法不一，
   建完读一次 `expired_at` 才算数。
"""

from __future__ import annotations

import logging
import re
import time

from ... import cookie as cookie_util
from ...base import PanClient
from ...errors import (
    AuthError,
    ConfigError,
    NetdiskError,
    PasscodeRequiredError,
    PasscodeWrongError,
    QuotaExceededError,
    ShareExpiredError,
    ShareInvalidError,
    TaskTimeoutError,
    UpstreamError,
    ViolationError,
    map_error,
)
from ...http import Requester
from ...registry import register
from ...types import Account, File, ShareInfo, ShareLink, TaskResult
from .const import EXPIRE_FOREVER, PROFILES

log = logging.getLogger("pantrace.sdk.quark")

# 上游错误码 → 语义异常。这张表是整套 SDK 的价值所在：
# 上层只需要问"该换账号还是该下架资源"，不需要认识 41012 是什么
ERROR_MAP: dict[object, type[NetdiskError]] = {
    31001: AuthError,   # 未登录
    31002: AuthError,   # 登录态过期
    31003: AuthError,   # cookie 无效
    41004: ShareInvalidError,   # 文件不存在
    41005: ViolationError,      # 选中文件违规
    41006: ShareInvalidError,   # 分享不存在
    41007: PasscodeWrongError,  # 提取码错误
    41008: PasscodeRequiredError,
    41009: ShareInvalidError,   # 分享文件已删除
    41010: ViolationError,      # 文件涉及违规内容
    41011: ShareExpiredError,   # 分享地址已失效
    41012: ShareExpiredError,   # 分享已取消
    41026: ViolationError,      # 选中文件违规，不支持分享
    41031: ShareInvalidError,   # 分享者封禁，链接受限
}

# 目录 fid 是 32 位十六进制。纯数字那种是测试环境的假 id，不要当 fid 用
_FID_RE = re.compile(r"^[0-9a-fA-F]{16,40}$")


@register
class QuarkClient(PanClient):
    type = "quark"
    display = "夸克网盘"

    def __init__(self, account: Account, *, timeout: float = 15.0, on_cookie=None):
        # 认类属性优先，其次账号上的 type。UCClient 继承本类，
        # 靠这一步才能选到 UC 的 pr/UA；只看 account.type 会在账号类型
        # 没填全时静默退化成夸克，然后在 UC 上返回 400
        wanted = (getattr(self, "type", "") or account.type or "quark").lower()
        self.profile = PROFILES.get(wanted, PROFILES["quark"])
        self.type = self.profile["type"]
        self.display = self.profile["display"]
        super().__init__(account, timeout=timeout, on_cookie=on_cookie)
        self._dir_cache: dict[str, str] = {}

    # ---- 请求层 ----

    def _build_requester(self) -> Requester:
        if not self.account.cookie:
            raise ConfigError(f"{self.display} 账号未配置 cookie")
        profile = getattr(self, "profile", PROFILES["quark"])
        return Requester(
            profile["api"],
            headers={
                "Cookie": self.account.cookie,
                "Referer": profile["referer"],
                "Origin": profile["referer"],
                "User-Agent": profile["ua"],
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            },
            default_params={"pr": profile["pr"], "fr": "pc"},
            timeout=self.timeout,
            on_cookie=self._absorb_cookie,
        )

    def _absorb_cookie(self, fresh: str) -> None:
        """接住响应里刷新的 __puus，立刻用于后续请求，并回写账号。"""
        merged = cookie_util.merge(self.account.cookie, fresh)
        if merged == self.account.cookie:
            return
        self.account.cookie = merged
        self.http.headers["Cookie"] = merged
        if self.on_cookie is not None:
            self.on_cookie(merged)

    def _ok(self, payload: dict, action: str) -> dict:
        code = payload.get("code")
        if str(code) in ("0", "None"):
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        message = str(payload.get("message") or payload.get("msg") or "").strip()
        raise map_error(ERROR_MAP, code, message or f"{action}失败", raw=payload)

    # ---- 分享侧 ----

    def share_info(self, url: str, pwd: str = "") -> ShareInfo:
        from ...urls import parse

        parsed = parse(url)
        if not parsed.ok:
            raise ShareInvalidError("链接不是夸克/UC 分享")
        pwd_id = parsed.pwd_id
        passcode = pwd or parsed.passcode

        token_resp = self.http.post(
            "/share/sharepage/token",
            json_body={"pwd_id": pwd_id, "passcode": passcode},
            action="读取分享",
            # 分享被删/被取消时夸克直接回 HTTP 404，不这么传就变成"上游故障"
            gone_error=ShareInvalidError,
        )
        data = self._ok(token_resp, "读取分享")
        stoken = str(data.get("stoken") or "")
        if not stoken:
            raise UpstreamError("没拿到 stoken", raw=token_resp)
        title = str(data.get("title") or "")[:60]

        files: list[File] = []
        total = 0
        for page in range(1, 6):
            detail = self.http.get(
                "/share/sharepage/detail",
                params={
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    "passcode": passcode,
                    "pdir_fid": "0",
                    "force": 0,
                    "_page": page,
                    "_size": 200,
                    "_fetch_total": 1,
                    "_sort": "file_type:asc,updated_at:desc",
                },
                action="读取文件列表",
                gone_error=ShareInvalidError,
            )
            body = self._ok(detail, "读取文件列表")
            batch = body.get("list") or []
            if not batch:
                break
            for entry in batch:
                size = int(entry.get("size") or 0)
                files.append(
                    File(
                        fid=str(entry.get("fid") or ""),
                        name=str(entry.get("file_name") or ""),
                        size=size,
                        is_dir=str(entry.get("file_type")) == "0",
                        updated_at=int(entry.get("updated_at") or 0),
                        extra={"share_fid_token": entry.get("share_fid_token") or ""},
                    )
                )
                total += size
            if len(batch) < 200:
                break

        if not files:
            raise ShareInvalidError("分享里没有文件")
        return ShareInfo(
            platform=self.type,
            pwd_id=pwd_id,
            title=title,
            stoken=stoken,
            passcode=passcode,
            files=files,
            total_size=total,
        )

    # ---- 转存侧 ----

    def transfer(self, info: ShareInfo, to_dir: str = "") -> list[str]:
        target = self.ensure_dir(to_dir)
        save = self.http.post(
            "/share/sharepage/save",
            json_body={
                "pwd_id": info.pwd_id,
                "stoken": info.stoken,
                "to_pdir_fid": target,
                "pdir_fid": "0",
                "pdir_save_all": True,
                "scene": "link",
            },
            action="转存",
        )
        data = self._ok(save, "转存")
        task_id = str(data.get("task_id") or "")
        if not task_id:
            raise UpstreamError("转存没有返回任务号", raw=save)
        result = self.wait_task(task_id, timeout=max(30.0, self.timeout * 2))
        if not result.fids:
            raise UpstreamError("转存完成但没有文件 id", raw=result.extra)
        return result.fids

    def create_share(self, fids: list[str], title: str, expire_hours: int = 0) -> ShareLink:
        resp = self.http.post(
            "/share",
            json_body={
                "fid_list": fids,
                "title": (title or "资源分享")[:60],
                "url_type": 1,
                "expired_type": EXPIRE_FOREVER,
            },
            action="创建分享",
        )
        data = self._ok(resp, "创建分享")
        task_id = str(data.get("task_id") or "")
        if not task_id:
            raise UpstreamError("创建分享没有返回任务号", raw=resp)

        task = self.wait_task(task_id, timeout=max(20.0, self.timeout))
        share_id = str(task.extra.get("share_id") or "")
        if not share_id:
            raise UpstreamError("分享任务完成但没有 share_id", raw=task.extra)

        pwd_resp = self.http.post("/share/password", json_body={"share_id": share_id}, action="读取分享信息")
        pwd_data = self._ok(pwd_resp, "读取分享信息")
        url = str(pwd_data.get("share_url") or f"{self.profile['share_host']}{share_id}")
        passcode = str(pwd_data.get("passcode") or "")
        if passcode and "pwd=" not in url:
            url = f"{url}?pwd={passcode}"

        expired_at = int(pwd_data.get("expired_at") or 0)
        note = ""
        if expired_at:
            seconds = expired_at / 1000 if expired_at > 10_000_000_000 else expired_at
            if seconds < time.time() + 300 * 86400:
                days = max(0, int((seconds - time.time()) / 86400))
                note = f"分享非永久（{days} 天后到期），检查 expired_type 常量"
                log.warning("%s 分享不是永久：%s → %s", self.display, url, note)

        return ShareLink(
            url=url,
            share_id=share_id,
            passcode=passcode,
            expired_at=expired_at,
            note=note,
        )

    # ---- 任务 ----

    def task_status(self, task_id: str) -> TaskResult:
        resp = self.http.get(
            "/task",
            params={"task_id": task_id, "retry_index": 0},
            action="查询任务",
        )
        data = self._ok(resp, "查询任务")
        status = int(data.get("status") or 0)
        save_as = data.get("save_as") or {}
        fids = (
            save_as.get("save_as_top_fids")
            or save_as.get("save_as_select_top_fids")
            or []
        )
        if status == 3:
            # 夸克把"容量不足"和"文件违规"都归在失败里，只能靠 message 猜
            message = str(data.get("message") or "")
            if "空间" in message or "容量" in message:
                raise QuotaExceededError("账号容量不足")
            raise UpstreamError(f"任务失败：{message or '未知原因'}", raw=data)
        return TaskResult(
            task_id=task_id,
            status=status,
            done=status == 2,
            fids=[str(f) for f in fids if f],
            extra={"share_id": data.get("share_id") or "", "raw": data},
        )

    # ---- 文件侧 ----

    def list_files(self, parent_id: str = "", page: int = 1, size: int = 50) -> list[File]:
        resp = self.http.get(
            "/file/sort",
            params={
                "pdir_fid": parent_id or "0",
                "force": 0,
                "_page": page,
                "_size": min(int(size), 200),
                "_fetch_total": 1,
                "_sort": "file_type:asc,updated_at:desc",
            },
            action="列目录",
        )
        data = self._ok(resp, "列目录")
        out: list[File] = []
        for entry in data.get("list") or []:
            out.append(
                File(
                    fid=str(entry.get("fid") or ""),
                    name=str(entry.get("file_name") or ""),
                    size=int(entry.get("size") or 0),
                    is_dir=str(entry.get("file_type")) == "0",
                    updated_at=int(entry.get("updated_at") or 0),
                )
            )
        return out

    def delete(self, fids: list[str]) -> None:
        if not fids:
            return
        resp = self.http.post(
            "/file/delete",
            json_body={"action_type": 2, "exclude_fids": [], "filelist": fids},
            action="删除文件",
        )
        data = self._ok(resp, "删除文件")
        task_id = str(data.get("task_id") or "")
        if task_id:
            try:
                self.wait_task(task_id, timeout=15.0)
            except TaskTimeoutError:
                # 删除没等到完成不算失败：文件已经进回收站，用户看不到就够了
                log.info("%s 删除任务未在时限内完成：%s", self.display, task_id)

    def mkdir(self, name: str, parent_id: str = "0") -> str:
        resp = self.http.post(
            "/file",
            json_body={
                "file_name": name,
                "pdir_fid": parent_id or "0",
                "dir_init_lock": False,
                "dir_path": "",
            },
            action="建目录",
        )
        data = self._ok(resp, "建目录")
        return str(data.get("fid") or "")

    def keepalive(self) -> None:
        """刷 __puus。夸克不刷会在一段时间后失效，巡检和清理都靠它续命。"""
        try:
            self.http.get("/auth/pc/flush", action="保活")
        except NetdiskError as exc:
            log.info("%s 保活失败：%s", self.display, exc)

    def probe(self) -> bool:
        try:
            self.http.get("/auth/pc/flush", action="探测账号")
            return True
        except NetdiskError:
            return False

    # ---- 目录 ----

    def ensure_dir(self, dir_ref: str) -> str:
        """账号配的临时目录 → fid。

        配置里可能是 fid，也可能是目录名，还可能是空的。竞品直接把它当 fid
        用，填错就静默转存到根目录——清理任务按目录删，于是永远清不到。
        这里三种都接住：
          - 空        → 根目录 "0"
          - 像 fid 的 → 直接用
          - 其他      → 根目录下找同名目录，没有就建
        """
        ref = (dir_ref or "").strip()
        if not ref:
            return "0"
        if _FID_RE.match(ref):
            return ref
        if ref in self._dir_cache:
            return self._dir_cache[ref]
        try:
            for entry in self.list_files("", 1, 200):
                if entry.is_dir and entry.name == ref:
                    self._dir_cache[ref] = entry.fid
                    return entry.fid
            fid = self.mkdir(ref)
            if fid:
                self._dir_cache[ref] = fid
                return fid
        except NetdiskError as exc:
            log.warning("%s 临时目录处理失败，回退根目录：%s", self.display, exc)
        return "0"


@register
class UCClient(QuarkClient):
    type = "uc"
    display = "UC网盘"