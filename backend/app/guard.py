"""接口防刷：按 IP 的滑动窗口限流 + 匿名票据。

设计边界（2026-11 决策，改之前先读）：

- 不接账号、不收个人信息、搜索即用。票据不携带姓名/手机/设备指纹，
  只是服务端按来源 IP 签发的一张短期凭证，证明「这个 IP 刚刚来领过票」。
- 票据挡的是不带票的低级脚本（curl 一把梭），挡不住会复刻领票流程的攻击者。
  **真正的底线是 IP 限流**，票据只是加一层壳，两者都要开。
- 票据绑定 IP：换 IP 即失效。IP 取 X-Real-IP / XFF 最右一项（见 main.client_ip），
  客户端伪造不进来。
- 限流用单调时钟 + 滑动窗口，避免固定窗口在边界上放行两倍突发。
- 所有状态在内存里，重启即清空。这个站规模用不上 Redis；多进程部署时
  每个进程各自限流，实际阈值 = 配置值 × 进程数，接受这个误差。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque


class _WindowLimiter:
    """滑动窗口计数：窗口内命中上限则拒绝，并返回还需等几秒。

    用 time.monotonic() 而非 time.time()——系统时间被 NTP 往前调时，
    墙上时钟会让历史请求"消失"，窗口直接失效。
    """

    def __init__(self, limit: int, window: float):
        self.limit = max(1, int(limit))
        self.window = float(window)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, cost: int = 1) -> tuple[bool, int]:
        """返回 (是否放行, 需等待秒数)。放行时等待秒数为 0。

        `cost` 是这次请求占几份配额。默认 1；会真实花上游额度的接口
        （`refresh=1` 绕过缓存）按 3 份计，等于把刷新频率压到普通搜索的 1/3。
        """
        cost = max(1, int(cost))
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) + cost > self.limit:
                wait = int(self.window - (now - q[0])) + 1 if q else int(self.window)
                return False, max(1, wait)
            # 用时间戳占位而不是另存计数：窗口滑出时按戳出队，语义和单次请求一致
            q.extend([now] * cost)
            # 字典膨胀保护：只清已经过期的空队列，不动还活着的 IP
            if len(self._hits) > 4096:
                for k in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(k, None)
            return True, 0


_limiter: _WindowLimiter | None = None
_ticket_secret: bytes = b"pantrace-ticket"
_ticket_ttl: int = 300


def init(secret: str, ttl: int, rate_limit: int, window: float = 60.0) -> None:
    """由 main.py 启动时注入配置。重复调用会重置所有计数。"""
    global _limiter, _ticket_secret, _ticket_ttl
    _ticket_secret = (secret or "").encode("utf-8") or b"pantrace-ticket"
    _ticket_ttl = max(30, int(ttl))
    _limiter = _WindowLimiter(rate_limit, window)


def allow(ip: str, cost: int = 1) -> tuple[bool, int]:
    """IP 限流。未初始化时放行——防止配置遗漏把整站打死。

    `cost` 见 `_WindowLimiter.allow`：会真实花额度的请求传 3。
    """
    if _limiter is None:
        return True, 0
    return _limiter.allow(ip or "-", cost)


def _ip_hash(ip: str) -> str:
    """票据里只放 IP 的带密钥摘要，不暴露原始 IP。"""
    return hmac.new(_ticket_secret, (ip or "").encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def issue(ip: str) -> dict:
    """签发票据。payload = ip摘要.签发时刻.过期时刻.随机串，整体 HMAC 签名。"""
    now = int(time.time())
    exp = now + _ticket_ttl
    payload = f"{_ip_hash(ip)}.{now}.{exp}.{secrets.token_hex(6)}"
    sig = hmac.new(_ticket_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return {"ticket": f"{token}.{sig}", "expiresIn": _ticket_ttl}


def verify(token: str, ip: str) -> bool:
    """校验签名、有效期、IP 绑定。任何异常一律当作无效，不往外抛。"""
    if not token or "." not in token:
        return False
    try:
        raw, sig = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")

        expect = hmac.new(_ticket_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return False

        parts = payload.split(".")
        if len(parts) != 4:
            return False
        ip_hash, _iat, exp, _nonce = parts
        if int(exp) < int(time.time()):
            return False
        # 票据换 IP 即失效：compare_digest 防时序侧信道
        return hmac.compare_digest(ip_hash, _ip_hash(ip))
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        return False
