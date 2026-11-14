"""网盘操作 SDK。

分层：

    sdk/
      errors.py     语义异常（按动作分类，不按网盘）
      types.py      Account / ShareInfo / File / TaskResult / ShareLink
      cookie.py     cookie 解析、合并、脱敏
      http.py       请求层（注入、重试、cookie 回收）
      base.py       PanClient 统一接口 + mirror 流水线
      urls.py       分享链接识别
      registry.py   类型注册表 + 工厂
      adapters/     每个网盘一个目录

用法：

    from app.sdk import registry, urls
    from app.sdk.types import Account

    parsed = urls.parse(link)
    client = registry.create(parsed.platform, Account(type=parsed.platform, cookie=...))
    link = client.mirror(parsed.url, parsed.passcode, title)

上层不 import 具体网盘。新增网盘 = 新增一个 adapter 目录 + 在
`adapters/__init__.py` 里 import 一次。
"""

from __future__ import annotations

from . import registry, urls
from .base import PanClient, poll
from .errors import (
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
    TooLargeError,
    UnsupportedError,
    UpstreamError,
    ViolationError,
)
from .types import Account, File, ShareInfo, ShareLink, TaskResult

__all__ = [
    "registry",
    "urls",
    "PanClient",
    "poll",
    "Account",
    "File",
    "ShareInfo",
    "ShareLink",
    "TaskResult",
    "NetdiskError",
    "AuthError",
    "ConfigError",
    "NotFoundError",
    "PasscodeRequiredError",
    "PasscodeWrongError",
    "QuotaExceededError",
    "ShareExpiredError",
    "ShareInvalidError",
    "TaskTimeoutError",
    "TooLargeError",
    "UnsupportedError",
    "UpstreamError",
    "ViolationError",
]
