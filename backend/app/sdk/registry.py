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

# 类型名 → adapter 模块名。UC 和夸克同一套接口、同一个模块，只是 profile 不同
_MODULE_OF: dict[str, str] = {"uc": "quark"}


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
        # 分清"这个盘没做"和"这个盘做了但没加载起来"——后者多半是缺依赖，
        # 报错里带上原始异常，比一句"暂不支持"省掉半小时排查
        mod = _MODULE_OF.get(key, key)
        why = (load_errors().get(mod) or "").strip()
        if why:
            raise UnsupportedError(f"{platform or key} 的适配器没能加载：{why}")
        raise UnsupportedError(f"暂不支持 {platform or '该平台'} 的转存")
    client = cls(account, timeout=timeout, on_cookie=on_cookie)
    # 阈值来自配置，不该在五个 adapter 的构造函数里各抄一遍签名。
    # max_file_bytes 只在 mirror() 里读，构造完再盖掉类默认值就够
    if max_bytes:
        client.max_file_bytes = int(max_bytes)
    return client


def load_errors() -> dict[str, str]:
    """各 adapter 的加载失败原因（成功的为空串）。"""
    try:
        from .adapters import LOAD_ERRORS
    except Exception:  # noqa: BLE001
        return {}
    return dict(LOAD_ERRORS)


def supported(platform: str) -> bool:
    return resolve_type(platform) in _REGISTRY


def available() -> list[str]:
    return sorted(_REGISTRY)


def _bootstrap() -> None:
    """导入所有 adapter 触发注册。

    放在函数里而不是模块顶部：adapter 会 import base，顶部互相 import
    就成环了。这里晚一步导入，环自然解开。

    单个 adapter 的失败已经在 adapters/__init__.py 里逐个兜住，
    这里的 try/except 只是防"adapters 包本身"出问题（比如语法错误）。
    """
    from . import adapters  # noqa: F401  （adapters/__init__.py 里逐个 import）


try:
    _bootstrap()
except Exception as exc:  # noqa: BLE001
    # 某个 adapter 写坏了不该让整个站起不来：搜索和看板还得能用
    import logging

    logging.getLogger("pantrace.sdk").warning("部分网盘 adapter 加载失败：%s", exc)
