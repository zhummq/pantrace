"""网盘 SDK 的统一错误体系。

竞品那套只有 `ApiError` / `HttpError` 两个类，错误码判断散在业务层——
`[41012, 41010, 41007, ...].includes(code)` 这种写法在每个 strategy 里各写一遍。
这里按**动作**分类：上层只判断"这个错该不该换账号、该不该下架资源"，
不需要认识任何一个网盘的码。

改之前先读这张表：

| 异常 | 上层该做什么 |
|---|---|
| AuthError           | 账号坏了：标记账号失效，换下一个，别重试 |
| ShareInvalidError   | 源链接坏了：可以给原链接兜底，累计计数后下架 |
| PasscodeRequired/Wrong | 源链接缺码/错码：不该下架，提示用户 |
| ViolationError      | 内容违规：下架 + 删掉已转存的文件 |
| QuotaExceededError  | 账号满了：换账号，不重试 |
| TaskTimeoutError    | 上游慢：可以重试一次 |
| UpstreamError       | 其他上游故障：可以重试 |
| ConfigError         | 我们自己的配置问题：重试没意义，要报警 |

新增网盘时只做一件事：在它的 ERROR_MAP 里把码映射到下面已有的类。
映射不出来，说明遇到新情况——**先加类，别塞进 UpstreamError**，
否则线上排查时又只剩一句"转存失败"。
"""

from __future__ import annotations

from typing import Mapping


class NetdiskError(Exception):
    """所有网盘异常的共同父类。

    四个类属性是给上层做决策用的，不是给人看的文案：
    retryable   —— 同样条件下再试一次有没有意义
    account_bad —— 该账号是不是废了
    share_bad   —— 源分享是不是废了（可以下架）
    """

    kind = "unknown"
    retryable = False
    account_bad = False
    share_bad = False

    def __init__(self, message: str, *, code: object = None, raw: object = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.raw = raw

    def __str__(self) -> str:  # noqa: D105
        if self.code is None:
            return self.message
        return f"{self.message}（code={self.code}）"


class AuthError(NetdiskError):
    """登录态失效。cookie / refresh_token 过期或未配置。"""

    kind = "auth"
    account_bad = True


class ShareInvalidError(NetdiskError):
    """源分享已失效：被取消、被删除、不存在。"""

    kind = "share_invalid"
    share_bad = True


class ShareExpiredError(ShareInvalidError):
    kind = "share_expired"


class PasscodeRequiredError(ShareInvalidError):
    """需要提取码但上游没给。用户的问题，不是资源的问题。"""

    kind = "passcode_required"


class PasscodeWrongError(ShareInvalidError):
    kind = "passcode_wrong"


class ViolationError(ShareInvalidError):
    """文件违规，不能转存/不能分享。要连已转存的文件一起删掉。"""

    kind = "violation"


class QuotaExceededError(NetdiskError):
    """账号容量不足。换账号有意义，原地重试没有。"""

    kind = "quota"


class NotFoundError(NetdiskError):
    kind = "not_found"


class TaskTimeoutError(NetdiskError):
    """异步任务没在时限内完成。可以重试一次。"""

    kind = "task_timeout"
    retryable = True


class UpstreamError(NetdiskError):
    """上游返回了没见过的错误，或网络层失败。"""

    kind = "upstream"
    retryable = True


class ConfigError(NetdiskError):
    """我们自己的配置问题：缺 client_id、缺临时目录之类。"""

    kind = "config"


class UnsupportedError(NetdiskError):
    """这个网盘不支持这个动作（比如迅雷没有建分享接口）。"""

    kind = "unsupported"


class TooLargeError(NetdiskError):
    """资源体积超出转存阈值。

    和账号、和链接都无关，是资源本身的属性。换账号也没用，更不该记在
    账号头上——否则一条 100G 的合集能让三个账号各背一次失败计数。
    """

    kind = "too_large"


def map_error(
    table: Mapping[object, type[NetdiskError]],
    code: object,
    message: str,
    *,
    raw: object = None,
) -> NetdiskError:
    """错误码 → 语义异常。表里没有的码一律落到 UpstreamError。

    注意 `code` 有可能是字符串（有的网盘返回 "41012"），查表前统一转成
    字符串比对，避免 TS 那边 `code == 41012` 这种隐式转换的坑搬到 Python 上。
    """
    normalized = str(code).strip() if code is not None else ""
    for key, exc in table.items():
        if str(key) == normalized:
            return exc(message, code=code, raw=raw)
    return UpstreamError(message or "上游返回未知错误", code=code, raw=raw)


def message_of(payload: object) -> str:
    """从各种形状的错误体里抠一句能给人看的话。

    网盘返回的字段名不统一：message / msg / error_description / errmsg /
    error_msg / show_msg 都见过。宁可退化成空串，也不要往上抛 dict。
    """
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "msg", "errmsg", "error_msg", "error_description", "show_msg", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
