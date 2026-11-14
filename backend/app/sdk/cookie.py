"""Cookie 工具：解析、合并、脱敏。

为什么单独一个模块：网盘的登录态全靠 cookie 串，而它是唯一**不能进日志**的东西。
把脱敏放在这里，所有打印都要过 `mask()`，避免哪天有人在报错里随手 `f"{cookie}"`
就把别人的账号卖了。
"""

from __future__ import annotations

import re

# 日志里必须打码的字段。__puus / BDUSS / STOKEN 泄漏等于交出账号
_SENSITIVE = re.compile(
    r"(?i)\b(__puus|__pus|__kp|__uid|bduss|stoken|ptoken|stoken|sid|puid|"
    r"access_token|refresh_token|token|sign|sig|auth)\b=([^;]*)"
)


def parse(cookie: str) -> dict[str, str]:
    """'a=1; b=2' → {'a': '1', 'b': '2'}。空值也保留，覆盖时要能覆盖成空。"""
    out: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def merge(old: str, *new_parts: str) -> str:
    """合并 cookie，新值覆盖旧值，保留旧串里的顺序。

    顺序有意义：网盘的 cookie 校验偶尔认第一个同名项，乱序会导致
    "明明填了正确 cookie 却一直 401"。所以这里用 dict 保序（3.7+ 保证），
    而不是 set。
    """
    merged = parse(old)
    for part in new_parts:
        for key, value in parse(part).items():
            merged[key] = value
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def pick(cookie: str, *names: str) -> str:
    """取指定字段拼回 cookie 串，用于某些只认个别字段的接口。"""
    parsed = parse(cookie)
    return "; ".join(f"{n}={parsed[n]}" for n in names if n in parsed)


def from_set_cookie(headers: list[str] | str | None) -> str:
    """把响应头里的 Set-Cookie 压成一段 cookie 串。

    只取 `k=v` 部分，丢掉 Path / Expires / HttpOnly 这些属性，
    否则合并回请求头会变成一坨非法内容。
    """
    if not headers:
        return ""
    items = [headers] if isinstance(headers, str) else list(headers)
    pairs = []
    for item in items:
        first = str(item or "").split(";")[0].strip()
        if first and "=" in first:
            pairs.append(first)
    return "; ".join(pairs)


def mask(cookie: str) -> str:
    """打日志用。保留前 4 位，其余打码，字段名不动。"""
    if not cookie:
        return ""

    def _sub(match: re.Match[str]) -> str:
        value = match.group(2)
        head = value[:4]
        return f"{match.group(1)}={head}{'*' * 6}"

    return _SENSITIVE.sub(_sub, str(cookie))
