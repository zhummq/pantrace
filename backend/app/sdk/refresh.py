"""同一账号的 token 刷新必须串行，刷出来的结果要给别的线程复用。

refresh_token 是**轮换式**的：换一次，旧的立即作废。两个客户端拿着同一份
旧 refresh_token 各去换一次，后到的那次就是 invalid_grant；有些实现还会把
整条 token family 一起吊销（OAuth 2.1 的 reuse detection）。线上表现就是
"配好当天能用、第二天全部请求 400"，而日志里只有一句 HTTP 400 看不出原因
——2026-11 迅雷账号就是这个形状。

所以刷新这一步按账号加锁，锁内先看有没有别人刚刷好的结果，有就直接用，
绝不重复去换。缓存按账号 id 存；后台改了凭据要 `forget()`，否则换了新的
refresh_token 这里还在用旧的。

**这是进程内的**：本服务单进程部署（uvicorn 单 worker），够用。哪天起了
多 worker，这里要换成跨进程的锁。
"""

from __future__ import annotations

import threading
import time

_guard = threading.Lock()
_locks: dict[object, threading.Lock] = {}
_latest: dict[object, tuple[str, float]] = {}

#: 缓存里的 token 剩余多少秒就不算"新鲜"。和适配器 _token() 里的阈值保持一致，
#: 否则会出现"这层说新鲜、适配器说过期"的来回抖动
SKEW = 60.0


def lock_for(key: object) -> threading.Lock:
    """按账号取锁。同一个账号串行，不同账号互不影响。"""
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def reuse(key: object, now: float | None = None) -> tuple[str, float] | None:
    """别的线程刚刷出来的 `(access_token, expires_at)`；没有或快过期就 None。"""
    with _guard:
        rec = _latest.get(key)
    if rec is None:
        return None
    access, expires = rec
    if expires - (now if now is not None else time.time()) <= SKEW:
        return None
    return access, expires


def remember(key: object, access: str, expires: float) -> None:
    with _guard:
        _latest[key] = (access, float(expires))


def forget(key: object) -> None:
    """凭据被改过（后台重填 refresh_token）时必须调，否则会拿旧 token 去打。"""
    with _guard:
        _latest.pop(key, None)
