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

    def create_share(self, fids: list[str], title: str, expire_hours: int = 0) -> ShareLink:
        """建分享。不支持建分享的盘（如迅雷）由上层直接用原链接兜底。"""
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

    def mirror(self, url: str, pwd: str = "", title: str = "", expire_hours: int = 0) -> ShareLink:
        """源分享 → 我们自己的分享。这是业务层唯一需要调的方法。

        失败不在这里兜底：异常原样抛给上层，由上层决定"换账号"还是
        "给原链接"。在这里吞掉异常，上层就永远不知道自己该做什么。
        """
        info = self.share_info(url, pwd)
        if not info.files:
            raise NetdiskError("分享里没有文件")
        limit = self.max_file_bytes
        if limit and info.total_size > limit:
            raise TooLargeError(
                f"资源 {info.total_size / 1024 ** 3:.1f}G，超出转存容量阈值"
            )
        fids = self.transfer(info, self.account.temp_dir)
        if not fids:
            raise NetdiskError("转存没有返回文件 id")
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

    # ---- 转存后收尾 ----

    #: 广告扫描的请求预算。这些请求都在用户点击的链路上——为了几个广告文件
    #: 把跳转拖到十几秒是不划算的，宁可漏删。
    AD_SCAN_REQUESTS = 24
    AD_SCAN_SECONDS = 8.0

    def scrub_ads(
        self,
        fids: list[str],
        keywords: list[str],
        *,
        max_depth: int = 2,
        max_requests: int | None = None,
        seconds: float | None = None,
    ) -> list[str]:
        """删掉转存内容里夹带的广告文件，返回被删的 fid。

        规则和竞品一致：递归下探 `max_depth` 层，文件名命中关键词就删；目录
        本身命中就连目录一起删，否则只删里面命中的文件。

        两个刻意的限制：
          - **只看文件名，不看路径**：路径里带上层目录名，"资源/微信"这种
            会被误杀。
          - **顶层文件删不到**：转存接口只回 fid 不回文件名，判断不了它是
            不是广告。要补只能多打一次 info 接口，不值。
        """
        pats = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
        if not pats or not fids:
            return []

        def matched(name: str) -> bool:
            low = (name or "").lower()
            return any(p in low for p in pats)

        budget = self.AD_SCAN_REQUESTS if max_requests is None else max(0, int(max_requests))
        deadline = time.monotonic() + (self.AD_SCAN_SECONDS if seconds is None else float(seconds))
        removed: list[str] = []

        def walk(parent_id: str, depth: int) -> None:
            nonlocal budget
            if depth > max_depth or budget <= 0 or time.monotonic() > deadline:
                return
            budget -= 1
            try:
                children = self.list_files(parent_id, 1, 100)
            except NetdiskError as exc:
                # 列不出来通常是"这是个文件，不是目录"，属正常情况，不打日志
                log.debug("%s 广告扫描跳过 %s：%s", self.display, parent_id, exc)
                return

            hits = [c for c in children if matched(c.name)]
            if hits:
                try:
                    self.delete([c.fid for c in hits])
                    removed.extend(c.fid for c in hits)
                    log.info(
                        "%s 转存内容里删掉 %s 个广告文件：%s",
                        self.display, len(hits),
                        "、".join(h.name[:24] for h in hits[:3]),
                    )
                except NetdiskError as exc:
                    log.warning("%s 广告文件删除失败：%s", self.display, exc)
            for child in children:
                if child.is_dir and not matched(child.name):
                    walk(child.fid, depth + 1)

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
