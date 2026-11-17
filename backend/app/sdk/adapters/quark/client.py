"""夸克 / UC 客户端。

七步流程（取 stoken → 列文件 → 转存 → 等任务 → 建分享 → 等分享 → 取提取码）
原样保留，但每步都拆成了可单独测的方法，错误码也按语义分类了。

三个必须保留的约束：

1. **cookie 会自己变**。夸克每次响应都可能下发新的 `__puus`，不接就会在
   半小时后集体 401。这里在请求层回收并回写账号表。
2. **转存是异步的**。`save` 只返回 task_id，不等任务完成就建分享会得到一个
   空分享——线上表现为"点开是空的"，比报错更难查。
3. **分享只建 1 天，且要回读校验**。`expired_type` 的取值没有官方文档
   （实测 1=永久、2=1天、3=7天、4=30天），建完必须读一次 `expired_at`，
   不是 1 天就换候选值重建。不做永久分享：链接只活 30 分钟，永久分享
   除了在回收任务失灵时把资源永久挂在账号上，没有任何好处。
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
from ...types import Account, File, ShareBrief, ShareInfo, ShareLink, TaskResult
from .const import EXPIRE_TIERS, PROFILES

log = logging.getLogger("pantrace.sdk.quark")


def _norm_hours(expire_hours: int) -> int:
    """0 / 负数 = 调用方没配，按 24 小时（我们对外宣称的默认档）处理。

    别按 1 小时算：那会让 `_expiry_ok` 拿"1 小时"的带宽去校验一条 1 天的分享，
    每次都判不符、白建三份分享，日志里还全是"有效期异常"。
    """
    hours = int(expire_hours or 0)
    return hours if hours > 0 else 24


def _candidates(expire_hours: int) -> tuple[tuple[int, int], ...]:
    """按请求的小时数选出候选链 `((该档的小时数, expired_type), ...)`。

    `expired_type` 没有官方文档，只能按社区实测的档位近似（1 天 / 7 天 / 30 天），
    所以永远给一条"往后降级"的链路：首选不对就试更长的，至少能建出来。
    请求超过 30 天时只试最长档——再往后没有已知取值。

    返回里带上**该档**的小时数（不是请求值）：校验到期时间要用档位算，
    否则请求 72 小时、实际落到 7 天档，会被自己的自检判成"异常"再白建一份。
    """
    want = _norm_hours(expire_hours)
    start = next(
        (i for i, (hours, _) in enumerate(EXPIRE_TIERS) if hours >= want),
        len(EXPIRE_TIERS) - 1,
    )
    return EXPIRE_TIERS[start:]


def _expiry_ok(seconds: int, expect_hours: int) -> bool:
    """回读的有效期是不是我们要的那一档（按 0.5～1.5 倍容差）。

    给带宽而不是卡死，是因为上游算的是"分享时刻 + N 天"，偶尔有几秒到几分钟
    的偏差。读不到（0）时按"没法校验"放过——接口偶尔不返回这个字段，
    为它重建一次只会白留一份分享。
    """
    if not seconds:
        return True
    expect = _norm_hours(expect_hours) * 3600
    left = int(seconds) - time.time()
    return expect * 0.5 <= left <= expect * 1.5

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

# 建分享任务的等待时限（秒）。原来只等 20 秒，账号下分享一多就经常不够：
# 一超时，create_share 就当成"这一档失败"去换 7 天档重建，用户最后拿到的是
# 7 天那条。先等 45 秒；还超时就着**同一个 task_id** 再等 30 秒——任务在
# 服务端通常已经跑完，续等拿到的 share_id 仍是这一档，不必换档重建。
# 两轮都超时才交给上层按候选链降级。分享本身 1 天后自然过期，不做清理。
_SHARE_TASK_TIMEOUT = 45.0
_SHARE_TASK_RETRY_TIMEOUT = 30.0


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

    def create_share(self, fids: list[str], title: str, expire_hours: int = 24) -> ShareLink:
        """建分享，并**回读真实有效期**，确认它确实落在请求的那一档。

        不建永久分享：给用户的链接只活 30 分钟（到点删源文件），1 天够用；
        万一回收任务没跑，分享也会自己到期，不会把资源永久挂在我们账号上。

        `expired_type` 没有官方文档、社区资料互相冲突，所以不盲信常量：
        先按 `expire_hours` 选出最接近的档位（默认 24 小时 → 1 天），建完读一次
        `expired_at`，落在该档 0.5～1.5 倍之外就换下一个（更长）候选值重建。
        某个候选值直接报参数错也继续换；但登录态失效、空间满这类错换了也
        没用，原样抛给上层去换账号。
        """
        candidates = _candidates(expire_hours)
        last: ShareLink | None = None
        error: NetdiskError | None = None
        for index, (tier_hours, expire_type) in enumerate(candidates):
            try:
                link = self._create_share_once(fids, title, expire_type, tier_hours)
            except (AuthError, QuotaExceededError):
                raise
            except NetdiskError as exc:
                error = exc
                log.warning(
                    "%s 用 expired_type=%s 建分享失败（%s），换下一个候选值",
                    self.display, expire_type, exc,
                )
                continue

            if _expiry_ok(link.expired_at, tier_hours):
                if index:
                    log.warning(
                        "%s 分享有效期回落到 expired_type=%s（%s），expired_type=%s 可能已失效",
                        self.display, expire_type, link.note or "未返回到期时间",
                        candidates[0][1],
                    )
                return link

            last = link
            log.error(
                "%s 分享有效期不是约 %s 小时（expired_type=%s，%s），换下一个候选值重建",
                self.display, tier_hours, expire_type,
                link.note or f"expired_at={link.expired_at}",
            )
        if last:
            # 候选值都试过了仍不符预期：用最后建成的那个（至少能打开），日志里已记实情
            return last
        raise error or UpstreamError("创建分享失败")

    def _create_share_once(
        self, fids: list[str], title: str, expire_type: int, expect_hours: int = 24
    ) -> ShareLink:
        resp = self.http.post(
            "/share",
            json_body={
                "fid_list": fids,
                "title": (title or "资源分享")[:60],
                "url_type": 1,
                "expired_type": expire_type,
            },
            action="创建分享",
        )
        data = self._ok(resp, "创建分享")
        task_id = str(data.get("task_id") or "")
        if not task_id:
            raise UpstreamError("创建分享没有返回任务号", raw=resp)

        # 超时 ≠ 没建成。这个 task 在服务端照常在跑，只是我们没等到结果；
        # 而 share_id 就在结果里，超时时还没拿到。先就着同一个 task 再等一轮，
        # 拿到就还是这一档，不会因为一次慢响应就降级成 7 天。
        try:
            task = self.wait_task(task_id, timeout=max(_SHARE_TASK_TIMEOUT, self.timeout))
        except TaskTimeoutError:
            log.warning(
                "%s 建分享任务 %ss 未完成，同一个 task 再等 %ss",
                self.display, int(_SHARE_TASK_TIMEOUT), int(_SHARE_TASK_RETRY_TIMEOUT),
            )
            task = self.wait_task(task_id, timeout=_SHARE_TASK_RETRY_TIMEOUT)
        share_id = str(task.extra.get("share_id") or "")
        if not share_id:
            raise UpstreamError("分享任务完成但没有 share_id", raw=task.extra)

        pwd_resp = self.http.post("/share/password", json_body={"share_id": share_id}, action="读取分享信息")
        pwd_data = self._ok(pwd_resp, "读取分享信息")
        url = str(pwd_data.get("share_url") or f"{self.profile['share_host']}{share_id}")
        passcode = str(pwd_data.get("passcode") or "")
        if passcode and "pwd=" not in url:
            url = f"{url}?pwd={passcode}"

        # 夸克给的是毫秒，统一归一化成秒，下游不用再猜单位
        raw_expired = int(pwd_data.get("expired_at") or 0)
        expired_at = raw_expired // 1000 if raw_expired > 10_000_000_000 else raw_expired

        # 到期时间符合预期就不写 note（note 会进 mirrors.reason，后台里一条条
        # "分享 1 天后到期"纯属噪音）；只有对不上才留字，给排障用。
        note = ""
        if expired_at and not _expiry_ok(expired_at, expect_hours):
            hours = max(0, int((expired_at - time.time()) / 3600))
            note = f"分享有效期异常：约 {hours} 小时后到期"
            log.error(
                "%s 分享有效期不是约 %s 小时（expired_type=%s，实际约 %s 小时）：%s",
                self.display, expect_hours, expire_type, hours, url,
            )

        return ShareLink(
            url=url,
            share_id=share_id,
            passcode=passcode,
            expired_at=expired_at,
            note=note,
        )

    # ---- 分享管理（撤销违规分享用）----

    def list_shares(
        self, *, only_violated: bool = False, page: int = 1, size: int = 200
    ) -> list[ShareBrief]:
        """列账号下的分享，等价网页版「我的分享」。

        参数全是网页端抓的，没有文档：
          - `_share_type="0,1"` = 文件分享 + 文件夹分享，少了会漏
          - `_filter="violated"` = 只回被平台判定违规的那些，别的取值别乱传，
            传错等于不筛（会返回全部），所以这里只在 True 时**追加**它
        单页上限实测 500，超过会被截断成默认值，所以自己封顶。
        """
        page_size = max(1, min(int(size), 500))
        params: dict[str, object] = {
            "_page": max(1, int(page)),
            "_size": page_size,
            "_order_field": "created_at",
            "_order_type": "desc",
            "_fetch_total": 1,
            "_fetch_notify_follow": 1,
            "_share_type": "0,1",
        }
        if only_violated:
            params["_filter"] = "violated"
        resp = self.http.get("/share/mypage/detail", params=params, action="列出分享")
        data = self._ok(resp, "列出分享")

        out: list[ShareBrief] = []
        for entry in data.get("list") or []:
            share_id = str(entry.get("share_id") or "")
            if not share_id:
                # 没有 share_id 就没法撤，混进来只会在后面白跑一次请求
                continue
            out.append(
                ShareBrief(
                    share_id=share_id,
                    title=str(entry.get("title") or entry.get("file_name") or "")[:120],
                    created_at=int(entry.get("created_at") or 0),
                    # 上游对违规的标记字段名不稳定，这里只做"有就是有"的保守判断；
                    # 真正的依据是 list_shares 被调时传了 _filter=violated
                    violated=bool(only_violated or entry.get("violated") or entry.get("status")),
                )
            )
        return out

    def revoke_shares(self, share_ids: list[str], *, batch: int = 50) -> dict:
        """取消分享。分批打，单批失败不影响后面的批。

        和 `delete()` 的区别要记住：这个撤的是**分享条目**，文件还在网盘里。
        撤销后旧链接立刻 404，但空间要另外删文件才回来。
        """
        ids = [str(s) for s in (share_ids or []) if s]
        if not ids:
            return {"ok": 0, "fail": 0}
        step = max(1, min(int(batch), 100))
        ok = fail = 0
        for start in range(0, len(ids), step):
            chunk = ids[start:start + step]
            try:
                resp = self.http.post(
                    "/share/delete",
                    json_body={"share_ids": chunk},
                    action="取消分享",
                )
                self._ok(resp, "取消分享")
                ok += len(chunk)
            except NetdiskError as exc:
                # 已经失效/已被别人撤掉的分享也会报错，一律算失败但要能看见原因，
                # 否则"每轮都撤不掉那几条"会变成永远查不出来的噪音
                fail += len(chunk)
                log.warning("%s 取消分享失败（%s 条）：%s", self.display, len(chunk), exc)
        return {"ok": ok, "fail": fail}

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