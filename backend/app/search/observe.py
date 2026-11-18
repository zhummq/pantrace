"""失败分类 + 源存活观测。

对应 pansou 的 `service/batch_search.go`（failureClass）与 `service/liveness.go`。

这一层不是"日志美化"。没有它，前面所有"每源独立报错"的机制你都看不见效果，
调试时会退化成"全是 0 条，不知道为什么"。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

try:  # httpx 是项目已有依赖；这里容错只是为了这个模块能被单独 import 做单测
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from .types import FailureClass


class HttpStatusError(Exception):
    """非 200 响应。对应 pansou 的 `httpStatusError`。

    为什么要一个独立异常类型，而不是"返回空列表 + 记一条日志"：
    状态码非 200 与"这个源真的没有内容"在类型上就必须是两件事。
    一旦混成同一个返回值，限流就会被记成零结果，然后同时污染存活统计和缓存，
    而且事后查不出来。这是 pansou 注释里反复强调的那条。
    """

    def __init__(self, status: int, source: str = "", body_hint: str = ""):
        self.status = status
        self.source = source
        self.body_hint = body_hint
        detail = f"HTTP {status}"
        if source:
            detail += f"（{source}）"
        if body_hint:
            detail += f" {body_hint[:80]}"
        super().__init__(detail)


def _classify_status(status: int) -> FailureClass:
    if status == 429:
        return FailureClass.RATE_LIMITED
    if status == 403:
        return FailureClass.FORBIDDEN
    if status == 404:
        return FailureClass.NOT_FOUND
    if status >= 500:
        return FailureClass.SERVER_ERROR
    return FailureClass.HTTP_STATUS


def classify_failure(exc: BaseException | None = None, status: int | None = None) -> FailureClass | None:
    """把异常 / HTTP 状态码归成一个失败类别。对应 pansou 的 `failureClass`。

    判定顺序和它一致：先看状态码（更具体），再看异常类型。
    """
    if status is not None and status != 200:
        return _classify_status(status)
    if exc is None:
        return None
    if isinstance(exc, HttpStatusError):
        return _classify_status(exc.status)
    # CancelledError 在 3.8+ 继承 BaseException，必须单独判，不能靠 Exception 兜
    if isinstance(exc, asyncio.CancelledError):
        return FailureClass.CANCELLED
    if isinstance(exc, asyncio.TimeoutError):
        return FailureClass.TIMEOUT
    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException):
            return FailureClass.TIMEOUT
        if isinstance(exc, httpx.TransportError):  # DNS / 连接 / TLS
            return FailureClass.NETWORK
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return FailureClass.NETWORK
    return FailureClass.OTHER


class Liveness(str, Enum):
    """源的存活分级。对应 pansou 的 `classify` 返回值。"""

    OK = "ok"
    DEGRADED = "degraded"                    # 失败次数超过一半
    FAILING = "failing"                      # 零产出且有过报错
    ZERO_YIELD = "zero_yield"                # 零产出但从不报错 ← 最隐蔽
    INSUFFICIENT_DATA = "insufficient_data"  # 样本不足，不下结论

    @property
    def is_sick(self) -> bool:
        return self in (Liveness.FAILING, Liveness.ZERO_YIELD, Liveness.DEGRADED)


# 与 pansou 同值：太少会被首轮冷启动误判（第一轮很多源确实什么都还没抓到）
LIVENESS_MIN_ROUNDS = 5
# 滑动窗口而不是累计计数：否则早期失败过就永久标坏，恢复后也洗不掉，
# 名单越积越长最后没人看 —— 与"一眼看出当前谁失效"的目标相反
LIVENESS_WINDOW = 20
LIVENESS_TOP_N = 40
LIVENESS_LAST_ERR_LEN = 120

_YIELD, _ZERO, _FAIL = 0, 1, 2


@dataclass
class LivenessEntry:
    ring: deque = field(default_factory=lambda: deque(maxlen=LIVENESS_WINDOW))
    last_yield: float = 0.0
    last_error: str = ""

    def push(self, code: int) -> None:
        self.ring.append(code)

    def counts(self) -> tuple[int, int, int, int]:
        rounds = len(self.ring)
        yielded = sum(1 for c in self.ring if c == _YIELD)
        failed = sum(1 for c in self.ring if c == _FAIL)
        zero = rounds - yielded - failed
        return rounds, yielded, failed, zero

    def classify(self) -> Liveness:
        rounds, yielded, failed, _ = self.counts()
        if rounds < LIVENESS_MIN_ROUNDS:
            return Liveness.INSUFFICIENT_DATA
        if yielded == 0:
            # 零产出分两种：报过错的是 failing，从不报错的是 zero_yield。
            # 后者是源改版后返回 200 + 空列表的典型形态，只有它能抓出来。
            return Liveness.FAILING if failed > 0 else Liveness.ZERO_YIELD
        if failed * 2 > rounds:
            return Liveness.DEGRADED
        return Liveness.OK


class LivenessRegistry:
    """进程级累积。必须是进程级：单次请求看不到"这个源连续多少轮没产出"。"""

    def __init__(self) -> None:
        self._plugins: dict[str, LivenessEntry] = {}
        self._channels: dict[str, LivenessEntry] = {}

    def record(self, kind: str, name: str, yielded: int, error: BaseException | None = None) -> None:
        """记一轮结果。yielded = 本轮产出的可用条目数。"""
        if not name:
            return
        table = self._plugins if kind == "plugin" else self._channels
        entry = table.setdefault(name, LivenessEntry())
        if error is not None:
            entry.push(_FAIL)
            entry.last_error = str(error)[:LIVENESS_LAST_ERR_LEN]
        elif yielded > 0:
            entry.push(_YIELD)
            entry.last_yield = time.time()
            entry.last_error = ""
        else:
            entry.push(_ZERO)

    def observe_plugin(self, name: str, yielded: int, error: BaseException | None = None) -> None:
        self.record("plugin", name, yielded, error)

    def observe_channel(self, name: str, yielded: int, error: BaseException | None = None) -> None:
        self.record("channel", name, yielded, error)

    def _snapshot_table(self, table: dict[str, LivenessEntry]) -> dict[str, dict]:
        # 只列需要关注的条目，正常的不占体积（pansou 同策略）
        out: dict[str, dict] = {}
        sick = [
            (name, e) for name, e in table.items()
            if e.classify().is_sick
        ]
        sick.sort(key=lambda kv: kv[1].classify().value)
        for name, e in sick[:LIVENESS_TOP_N]:
            rounds, yielded, failed, zero = e.counts()
            out[name] = {
                "status": e.classify().value,
                "rounds": rounds,
                "yielded": yielded,
                "failed": failed,
                "zero_yield": zero,
                "last_yield": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.last_yield)) if e.last_yield else None,
                "last_error": e.last_error,
            }
        return out

    def snapshot(self) -> dict[str, dict]:
        """给 /api/health 用。"""
        return {
            "plugins": self._snapshot_table(self._plugins),
            "channels": self._snapshot_table(self._channels),
        }


# 进程级单例，和 pansou 的 livenessGlobal 对应
liveness = LivenessRegistry()
