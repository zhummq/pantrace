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
    expired_at: int = 0
    note: str = ""
    #: 这次转存占了哪些 fid。清理任务靠它把文件删掉，不保存就只能靠目录 diff 猜
    fids: list[str] = field(default_factory=list)
    #: 转存内容总字节数，用于容量统计
    size: int = 0

    @property
    def permanent(self) -> bool:
        """expired_at 为 0 表示读不到，不能当成"永久"下结论。"""
        return self.expired_at == 0


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
