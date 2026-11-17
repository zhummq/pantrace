"""SDK 的公共数据结构。

只用 dataclass，不用 pydantic：这些对象在每次点击转存时都会新建一堆，
pydantic 的校验开销在这里没有收益，而且它们不需要序列化给前端。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class File:
    """网盘里的一个条目（文件或目录）。"""

    fid: str
    name: str = ""
    size: int = 0
    is_dir: bool = False
    updated_at: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def human_size(self) -> str:
        size = float(self.size or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
            size /= 1024
        return f"{size:.1f}TB"


@dataclass(slots=True)
class ShareInfo:
    """解析出来的源分享。stoken / share_id 各网盘叫法不同，统一放这里。"""

    platform: str
    pwd_id: str = ""
    title: str = ""
    stoken: str = ""
    passcode: str = ""
    #: 上游那条**源分享**的到期时刻，Unix 秒；0 = 读不到。和 ShareLink 同一个
    #: 约定，别一处给秒一处给毫秒——2026-11 百度就踩过 `* 1000` 的坑
    expired_at: int = 0
    files: list[File] = field(default_factory=list)
    total_size: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskResult:
    """异步任务结果（转存、建分享都是异步的）。"""

    task_id: str = ""
    status: int = 0
    done: bool = False
    fids: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ShareLink:
    """我们自己建出来的分享。"""

    url: str
    share_id: str = ""
    passcode: str = ""
    #: 分享在上游的到期时刻，**Unix 秒**（各适配器必须归一化成秒再填）。
    #: 0 = 上游没给/读不到，**不是**"永久"——不要拿它和 time.time() 相减去猜
    #: 还剩多久，除非先确认这个字段确实有值
    expired_at: int = 0
    note: str = ""
    #: 这次转存占了哪些 fid。清理任务靠它把文件删掉，不保存就只能靠目录 diff 猜
    fids: list[str] = field(default_factory=list)
    #: 转存内容总字节数，用于容量统计
    size: int = 0

    @property
    def expiry_unknown(self) -> bool:
        """上游没返回到期时间（`expired_at=0`）。

        原来叫 `permanent`，名字是错的：0 的含义是"读不到"，不是"永久"。
        真按"永久"去理解，就会把一条读不到有效期的分享当成永远可用。
        目前没有调用方，留着是为了给上层一个统一的判断口子。
        """
        return self.expired_at == 0


@dataclass(slots=True)
class ShareBrief:
    """我们账号下的一条分享（列"我的分享"用）。

    只为批量撤销服务：`share_id` 是撤销时要传的键，其余字段都是给日志和
    后台看的，别指望它们齐全——各网盘返回的字段名和有无都不一样。
    """

    share_id: str
    title: str = ""
    created_at: int = 0
    violated: bool = False


@dataclass(slots=True)
class Account:
    """一个网盘账号。cookie 与 token 字段按网盘类型取用。

    temp_dir 是转存落点：竞品的做法是每个账号配一个临时目录，
    清理任务只在目录内删文件，靠这个前缀防误删。
    """

    id: int = 0
    type: str = ""
    name: str = ""
    cookie: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    temp_dir: str = ""
    status: int = 1
    # 运行期统计，不落库
    fail_count: int = 0
    last_error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.status == 1
