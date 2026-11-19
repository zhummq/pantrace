"""所有网盘客户端的统一接口。

竞品那边五个盘的方法名各写各的（save / getShare / restoreShare /
createTransferTask），上层被迫写五个 strategy 分支，每个分支还要各处理
一遍错误码和任务轮询。这里反过来：**adapter 适配统一接口，业务层只走一条流水线**。

新增一个网盘要做的全部事情：

    class XXXClient(PanClient):
        type = "xxx"
        def share_info(...) -> ShareInfo
        def transfer(...) -> list[str]
        def list_files(...) -> list[File]
        def delete(...) -> None
        # create_share / wait_task / keepalive 有默认实现，不支持就别覆盖
        # 想参与违规巡检再加 list_shares / revoke_shares（不实现就自动跳过）

然后 `registry.register(XXXClient)`。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from . import adfilter
from .errors import (
    NetdiskError,
    TaskTimeoutError,
    TooLargeError,
    UnsupportedError,
    ViolationError,
)
from .http import Requester
from .types import Account, File, ShareBrief, ShareInfo, ShareLink, TaskResult

# 收尾动作（删违规副本、清广告）都发生在适配器的公共流程里，日志统一挂这个
# 名字，不要各 adapter 自己起的 logger——排查时要一次 grep 到全部网盘
log = logging.getLogger("pantrace.sdk")


class PanClient(ABC):
    """网盘客户端基类。一个实例绑定一个账号。"""

    type: str = ""
    display: str = ""
    #: 支持转存的最大单文件体积（字节），0 表示不限制
    max_file_bytes: int = 0
    #: 转存前过滤顶层广告附件用的关键词（空 = 不过滤）；由业务层在
    #: registry.create 之后覆盖，理由同 max_file_bytes
    ad_keywords: list[str] = []
    #: 判定广告的体积上限。超过这个体积的附件一律不判，见 adfilter 的说明
    ad_max_bytes: int = adfilter.DEFAULT_MAX_BYTES
    #: 转存引流水印文件夹名，业务层在 registry.create 之后盖进来（同
    #: ad_keywords 的手法，不进各 adapter 的构造签名）。空 = 关闭。
    watermark_name: str = ""

    def __init__(
        self,
        account: Account,
        *,
        timeout: float = 15.0,
        on_cookie: Callable[[str], None] | None = None,
    ):
        self.account = account
        self.timeout = timeout
        self.on_cookie = on_cookie
        self.http = self._build_requester()

    # ---- 子类实现 ----

    def _build_requester(self) -> Requester:
        raise NotImplementedError

    @abstractmethod
    def share_info(self, url: str, pwd: str = "") -> ShareInfo:
        """解析源分享：标题、stoken、文件列表、总体积。"""

    @abstractmethod
    def transfer(self, info: ShareInfo, to_dir: str = "") -> list[str]:
        """把源分享转存到 `to_dir`，返回新生成的 fid 列表。"""

    @abstractmethod
    def list_files(self, parent_id: str = "", page: int = 1, size: int = 50) -> list[File]:
        """列目录。parent_id 为空表示根目录。"""

    @abstractmethod
    def delete(self, fids: list[str]) -> None:
        """删除文件（进回收站或直删，看网盘）。"""

    # ---- 默认实现 ----

    def make_watermark(self, name: str, to_dir: str = "") -> str:
        """在转存落点目录里建一个**空文件夹**当引流水印，返回它的 fid。

        默认不支持——返回空串，转存照常进行，只是没有水印。支持建目录的盘
        （夸克/UC、迅雷、百度；光鸭/139 无现成建目录接口）覆盖这个方法。
        空文件夹会随 fids 一起进分享、一起
        被 30 分钟回收删掉，所以它是"用完即弃"的广告位，不占长期空间。

        `name` 由业务层的 watermark_name 配置传入，空串表示功能关闭。
        这里不判断空串——调用方 mirror() 已经判过；实现方拿到非空名字即可建。
        """
        return ""

    def create_share(self, fids: list[str], title: str, expire_hours: int = 24) -> ShareLink:
        """建分享。不支持建分享的盘（如迅雷）由上层直接用原链接兜底。

        `expire_hours` 是**分享本身**的有效期，不是我们给用户的链接窗口
        （那个由回收任务删文件实现）。默认 1 天：回收任务没跑时分享自己到期，
        资源不会永久挂在账号上。传 0 不代表永久，各适配器会按最短档处理。
        """
        raise UnsupportedError(f"{self.display or self.type} 不支持创建分享")

    def list_shares(self, *, only_violated: bool = False, page: int = 1, size: int = 200) -> list[ShareBrief]:
        """列我们账号下的分享。

        **只为撤销服务**，不做的盘不要假装能列。业务层拿到 UnsupportedError
        就当"这个盘不用巡检"跳过，不影响其他账号——所以默认实现直接抛。
        """
        raise UnsupportedError(f"{self.display or self.type} 不支持列出分享")

    def revoke_shares(self, share_ids: list[str], *, batch: int = 50) -> dict:
        """撤销分享（**不是**删文件）。返回 `{"ok": n, "fail": n}`。

        为什么需要它：删文件只能让链接变死链，分享条目还挂在账号上；违规
        分享挂着的代价是被限流/封号，必须从"我的分享"里撤掉。
        """
        raise UnsupportedError(f"{self.display or self.type} 不支持取消分享")

    def wait_task(self, task_id: str, timeout: float = 30.0) -> TaskResult:
        """轮询异步任务。默认实现按 0.5s 起步、1s 退避。

        超时抛 TaskTimeoutError 而不是返回未完成的结果——上层拿到
        "还没好"的 task 也不知道怎么处理，只会把空 fid 传下去建出一个空分享。
        """
        started = time.monotonic()
        interval = 0.5
        while time.monotonic() - started < timeout:
            result = self.task_status(task_id)
            if result.done:
                return result
            time.sleep(interval)
            interval = min(interval + 0.25, 1.0)
        raise TaskTimeoutError(f"{self.display or self.type} 任务超时（{int(timeout)}s）")

    def task_status(self, task_id: str) -> TaskResult:
        raise UnsupportedError(f"{self.display or self.type} 不支持任务查询")

    def keepalive(self) -> None:
        """保活。默认什么都不做；夸克需要定期刷 __puus。"""

    def probe(self) -> bool:
        """账号是否还能用。巡检任务调它。默认用 share_info 的方式试不出来，
        子类应覆盖成一次轻量请求。"""
        return True

    def probe_detail(self) -> dict:
        """探测详情。后台「测试」按钮用它把失败原因说清楚。

        默认只有 ok。**覆盖时不要再调 self.probe()**——两者互相调用会绕成环。
        有多套凭据的盘（百度就是 cookie + OAuth）在这里分别报状态，
        `ok` 只回答"能不能干主业务"，其余信息放 `detail` 里给后台看。
        """
        return {"ok": self.probe(), "reason": "", "detail": {}}

    def close(self) -> None:
        # 关的是 requests.Session。这里吞掉异常：close 常在 finally 里被调，
        # 它自己抛出来会把真正的失败原因盖掉，排查时只剩一句 AttributeError
        try:
            self.http.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- 统一流水线 ----

    def mirror(self, url: str, pwd: str = "", title: str = "", expire_hours: int = 24) -> ShareLink:
        """源分享 → 我们自己的分享。这是业务层唯一需要调的方法。

        失败不在这里兜底：异常原样抛给上层，由上层决定"换账号"还是
        "给原链接"。在这里吞掉异常，上层就永远不知道自己该做什么。
        """
        info = self.share_info(url, pwd)
        if not info.files:
            raise NetdiskError("分享里没有文件")
        # 位置很关键：必须在 transfer 之前。share_info 已经把顶层文件名都拿
        # 到手里了，这时候拦下来的广告根本不会进我们的网盘——不用删、不占
        # 容量、不进回收站，也不产生任何额外请求（纯内存判断）
        self._prefilter_ads(info)
        limit = self.max_file_bytes
        if limit and info.total_size > limit:
            raise TooLargeError(
                f"资源 {info.total_size / 1024 ** 3:.1f}G，超出转存容量阈值"
            )
        fids = self.transfer(info, self.account.temp_dir)
        if not fids:
            raise NetdiskError("转存没有返回文件 id")
        # 引流水印：转存完就地在落点目录建一个空文件夹，fid 并入 fids。
        # 它必须赶在 create_share 之前——分享要把它一起带出去，回收也靠这
        # 份 fids 删它。建水印失败只记日志，绝不让一条本已成功的转存变失败。
        fids = self._with_watermark(fids)
        try:
            link = self.create_share(fids, info.title or title or "资源分享", expire_hours)
        except ViolationError:
            # 违规文件已经转存下来了，只是建不了分享。不删就是孤儿：mirrors
            # 里没有它的行，回收任务永远扫不到，账号上白白挂着违规内容。
            # 这条约定原本只写在 errors.py 的表里，这里把它兑现。
            try:
                self.delete(fids)
                log.warning("%s 建分享被拒（违规），已删除转存副本 %s 个", self.display, len(fids))
            except NetdiskError as exc:
                log.error("%s 违规副本删除失败（%s 个）：%s", self.display, len(fids), exc)
            raise
        # 把 fids 带回上层：清理任务要知道删哪些文件，重新列目录去猜代价太大
        link.fids = list(fids)
        link.size = info.total_size
        return link

    def _with_watermark(self, fids: list[str]) -> list[str]:
        """把引流水印文件夹的 fid 并进转存 fids。失败/不支持时原样返回。

        这是"锦上添花"，不是主流程：建文件夹要额外请求，而且空目录能不能进
        分享各盘不一样。所以任何异常都在这里吞掉，只记日志——绝不能让用户
        因为一个广告文件夹拿不到本来已经转存成功的资源。
        """
        name = (self.watermark_name or "").strip()
        if not name:
            return fids
        try:
            wfid = self.make_watermark(name, self.account.temp_dir)
        except Exception as exc:  # noqa: BLE001 - 水印失败不影响转存，故兜到最宽
            log.info("%s 建引流水印文件夹「%s」失败，跳过：%s", self.display, name[:24], exc)
            return fids
        if not wfid:
            return fids
        log.info("%s 引流水印文件夹「%s」已建（fid=%s）", self.display, name[:24], wfid)
        return [*fids, wfid]

    def _prefilter_ads(self, info: ShareInfo) -> None:
        """转存前把顶层广告附件从待转清单里摘掉。就地改 `info`。

        只动"三重命中"的附件（关键词 + 附件后缀 + 体积小），目录和正片一律
        不动——判据在 `adfilter` 里，改之前先看那边的注释。

        **顶层全中时放弃过滤**：那种分享要么整条是垃圾，要么我们的关键词配
        歪了。无论哪种，给用户一个空分享都比多几个广告文件糟糕，所以原样
        全转，日志留一笔。
        """
        keywords = [
            k.strip().lower() for k in (self.ad_keywords or []) if k and k.strip()
        ]
        if not keywords or not info.files:
            return

        kept, dropped = adfilter.split(info.files, keywords, int(self.ad_max_bytes or 0))
        if not dropped:
            return

        names = "、".join(f.name[:20] for f in dropped[:3])
        if not kept:
            log.warning(
                "%s 顶层 %s 个条目全部命中广告判据，放弃过滤、整条转存：%s",
                self.display, len(dropped), names,
            )
            return

        info.excluded = [f.fid for f in dropped]
        info.files = kept
        # 体积跟着减：它要参与 max_file_bytes 判断，也算进 link.size
        info.total_size = max(
            0, int(info.total_size or 0) - sum(int(f.size or 0) for f in dropped)
        )
        log.info(
            "%s 转存前跳过 %s 个广告附件（剩 %s 个）：%s",
            self.display, len(dropped), len(kept), names,
        )

    # ---- 转存后收尾 ----

    #: 广告扫描的请求预算。这段现在跑在后台线程里（见 pan._queue_scrub），
    #: 不占用户等待时间，所以比原来宽松；但仍然是真实请求，翻页要有上限——
    #: 一个几千条文件的目录能把账号翻出风控
    AD_SCAN_REQUESTS = 40
    AD_SCAN_SECONDS = 20.0
    AD_SCAN_PAGE = 100
    AD_SCAN_MAX_PAGES = 5

    def scrub_ads(
        self,
        fids: list[str],
        keywords: list[str],
        *,
        max_depth: int = 2,
        max_bytes: int = adfilter.DEFAULT_MAX_BYTES,
        max_requests: int | None = None,
        seconds: float | None = None,
    ) -> list[str]:
        """删掉转存内容里夹带的广告文件，返回被删的 fid。

        这是**兜底**，不是主路径：顶层广告在 `transfer` 之前就被 `adfilter`
        拦掉了，这里只处理"目录内部夹着的那一份"，量很小。

        判据和转前拦截共用 `adfilter.is_ad`，两处不会各自漂移。三条硬规矩：
          - **只删文件，目录一个都不删**。目录里可能装着正片，删目录就是删正片。
          - 三重命中才删：附件型后缀 + 体积小于阈值 + 名字命中关键词。
          - **只看文件名不看路径**：路径里带上层目录名，"资源/微信"这种会误杀。
        """
        pats = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
        if not pats or not fids:
            return []

        budget = self.AD_SCAN_REQUESTS if max_requests is None else max(0, int(max_requests))
        deadline = time.monotonic() + (self.AD_SCAN_SECONDS if seconds is None else float(seconds))
        page_size = self.AD_SCAN_PAGE
        removed: list[str] = []

        def walk(parent_id: str, depth: int) -> None:
            nonlocal budget
            if depth > max_depth:
                return
            page = 1
            # 翻页：老版本只列第一页 100 条，超过 100 个子项的目录整块漏扫
            while page <= self.AD_SCAN_MAX_PAGES:
                if budget <= 0 or time.monotonic() > deadline:
                    return
                budget -= 1
                try:
                    children = self.list_files(parent_id, page, page_size)
                except NetdiskError as exc:
                    # 列不出来通常是"这是个文件，不是目录"，属正常情况，不打日志
                    log.debug("%s 广告扫描跳过 %s：%s", self.display, parent_id, exc)
                    return
                if not children:
                    return

                hits = [c for c in children if adfilter.is_ad(c, pats, max_bytes)]
                if hits:
                    try:
                        self.delete([c.fid for c in hits])
                        removed.extend(c.fid for c in hits)
                        log.info(
                            "%s 目录内删掉 %s 个广告附件：%s",
                            self.display, len(hits),
                            "、".join(h.name[:24] for h in hits[:3]),
                        )
                    except NetdiskError as exc:
                        log.warning("%s 广告文件删除失败：%s", self.display, exc)
                for child in children:
                    if child.is_dir:
                        walk(child.fid, depth + 1)
                if len(children) < page_size:
                    return
                page += 1

        for fid in fids:
            walk(fid, 1)
        return removed

    # ---- 小工具 ----

    @property
    def ready(self) -> bool:
        """配置是否齐。不同网盘凭据字段不同，子类覆盖。"""
        return bool(self.account.cookie or self.account.refresh_token)

    def __repr__(self) -> str:  # noqa: D105
        return f"<{self.__class__.__name__} type={self.type} account={self.account.id}>"


def poll(
    fn: Callable[[], Any],
    *,
    timeout: float = 30.0,
    interval: float = 0.5,
    done: Callable[[Any], bool] = bool,
    action: str = "等待任务",
) -> Any:
    """通用轮询。done 返回 True 就收工，超时抛 TaskTimeoutError。

    和 wait_task 的区别：这个不依赖网盘的任务接口，用于"轮询某个资源的
    状态直到出现某字段"这种一次性逻辑（比如百度转存后要轮询 filemetas）。
    """
    started = time.monotonic()
    last: Any = None
    while time.monotonic() - started < timeout:
        last = fn()
        if done(last):
            return last
        time.sleep(interval)
    raise TaskTimeoutError(f"{action}超时（{int(timeout)}s）")
