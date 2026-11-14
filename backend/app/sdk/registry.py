"""网盘类型注册表。

adapter 自己 `@register` 一下就好，业务层不 import 具体网盘。
新增网盘不需要改这个文件——这是它存在的唯一理由。
"""

from __future__ import annotations

import threading
from typing import Type

from .base import PanClient
from .errors import UnsupportedError
from .types import Account

_LOCK = threading.Lock()
_REGISTRY: dict[str, Type[PanClient]] = {}
_ALIASES: dict[str, str] = {
    # 上游搜索用的平台 key 与 SDK 的类型名不完全一致，在这里对齐
    "uc": "uc",
    "quark": "quark",
    "baidu": "baidu",
    "xunlei": "xunlei",
    "guangya": "guangya",
    "c139": "c139",
    "yidong": "c139",
    "ali": "ali",
}


def register(cls: Type[PanClient]) -> Type[PanClient]:
    """类装饰器。type 为空直接报错，避免注册出调不通的条目。"""
    if not cls.type:
        raise ValueError(f"{cls.__name__} 没有声明 type")
    with _LOCK:
        _REGISTRY[cls.type] = cls
    return cls


def resolve_type(platform: str) -> str:
    return _ALIASES.get((platform or "").strip().lower(), (platform or "").strip().lower())


def create(
    platform: str,
    account: Account,
    *,
    timeout: float = 15.0,
    on_cookie=None,
    max_bytes: int = 0,
) -> PanClient:
    key = resolve_type(platform)
    cls = _REGISTRY.get(key)
    if cls is None:
        raise UnsupportedError(f"暂不支持 {platform or '该平台'} 的转存")
    client = cls(account, timeout=timeout, on_cookie=on_cookie)
    # 阈值来自配置，不该在五个 adapter 的构造函数里各抄一遍签名。
    # max_file_bytes 只在 mirror() 里读，构造完再盖掉类默认值就够
    if max_bytes:
        client.max_file_bytes = int(max_bytes)
    return client


def supported(platform: str) -> bool:
    return resolve_type(platform) in _REGISTRY


def available() -> list[str]:
    return sorted(_REGISTRY)


def _bootstrap() -> None:
    """导入所有 adapter 触发注册。

    放在函数里而不是模块顶部：adapter 会 import base，顶部互相 import
    就成环了。这里晚一步导入，环自然解开。
    """
    from . import adapters  # noqa: F401  （adapters/__init__.py 里逐个 import）


try:
    _bootstrap()
except Exception as exc:  # noqa: BLE001
    # 某个 adapter 写坏了不该让整个站起不来：搜索和看板还得能用
    import logging

    logging.getLogger("pantrace.sdk").warning("部分网盘 adapter 加载失败：%s", exc)
