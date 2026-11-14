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

然后 `registry.register(XXXClient)`。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from .errors import NetdiskError, TaskTimeoutError, TooLargeError, UnsupportedError
from .http import Requester
from .types import Account, File, ShareInfo, ShareLink, TaskResult


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
        link = self.create_share(fids, info.title or title or "资源分享", expire_hours)
        # 把 fids 带回上层：清理任务要知道删哪些文件，重新列目录去猜代价太大
        link.fids = list(fids)
        link.size = info.total_size
        return link

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
