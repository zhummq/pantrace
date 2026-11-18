"""插件自注册 + 白名单加载。

对应 pansou 的 `plugin.RegisterGlobalPlugin` 与
`PluginManager.RegisterGlobalPluginsWithFilter`。

**默认全关这条要保留**：`enabled` 为空 = 一个源都不启用。新加的源必须显式批准
才会上线 —— 一个源出问题不该靠"我记得去关掉它"来兜。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import BasePlugin, build_clients
from .cache import SearchCache
from .pool import WorkerPool

log = logging.getLogger("pantrace.search.registry")

_PKG = "app.search.plugins"
_REGISTRY: dict[str, type[BasePlugin]] = {}
_LOADED: list[BasePlugin] = []


def register(cls: type[BasePlugin]) -> type[BasePlugin]:
    """装饰器：把一个插件类登记进全局表。等价于 Go 的 `init()` + `RegisterGlobalPlugin`。

    只登记类，不实例化 —— 实例化放在 load_all 里，那时配置和共享资源都就绪了。
    （pansou 的坑：插件在包 init 阶段构造，读到的是还没加载的配置。）
    """
    if not getattr(cls, "name", ""):
        raise ValueError(f"{cls.__name__} 缺少类属性 name")
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"插件名重复：{cls.name}（{_REGISTRY[cls.name]} 与 {cls}）")
    _REGISTRY[cls.name] = cls
    return cls


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


def get_registered_class(name: str) -> type[BasePlugin] | None:
    """按名取插件类。对应 pansou 的 `plugin.GetPluginByName`（O(1) 哈希查找）。

    排序打分要用它读 `level`，所以必须存在；不能改成遍历实例列表 ——
    实例只在 load_all 之后才有，而打分在合并阶段就可能跑。
    """
    return _REGISTRY.get(name)


def _discover() -> None:
    """扫 plugins/ 目录，等价于 Go 在 main.go 里那 75 行空导入。"""
    try:
        pkg = importlib.import_module(_PKG)
    except ModuleNotFoundError:
        log.warning("插件包 %s 不存在，跳过", _PKG)
        return
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{_PKG}.{mod.name}")
        except Exception as exc:  # 单个插件炸掉不能拖垮整个加载
            log.error("插件 %s 加载失败: %s", mod.name, exc)


def parse_enabled(raw: str | list[str] | None) -> list[str]:
    """解析白名单。空 = 全关，和 pansou 一致。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [s.strip() for s in raw if s and s.strip()]


def load_all(
    enabled: str | list[str] | None = None,
    *,
    cache: SearchCache | None = None,
    pool: WorkerPool | None = None,
    proxy: str | None = None,
) -> list[BasePlugin]:
    """扫目录 → 过滤白名单 → 实例化 → 注入共享资源。"""
    global _LOADED
    _discover()

    wanted = parse_enabled(enabled)
    if not wanted:
        log.warning("未配置任何启用源（ENABLED_SOURCES 为空），插件层为空")
        _LOADED = []
        return []

    unknown = [n for n in wanted if n not in _REGISTRY]
    if unknown:
        log.error("白名单里有未注册的源，已忽略: %s（可用：%s）", unknown, registered_names())

    instances: list[BasePlugin] = []
    for name in wanted:
        cls = _REGISTRY.get(name)
        if cls is None:
            continue
        instances.append(cls())

    if instances and (cache is None or pool is None):
        raise ValueError("加载插件必须提供 cache 与 pool")

    # 所有实例共享同一组客户端和缓存：每个插件各建一份连接池是纯浪费
    if instances:
        assert cache is not None and pool is not None
        fast, long = build_clients(proxy=proxy)
        for inst in instances:
            inst.cache = cache
            inst.pool = pool
            inst.client_fast = fast
            inst.client_long = long

    _LOADED = instances
    log.info("已启用 %d 个源：%s", len(instances), [p.name for p in instances])
    return instances


def loaded() -> list[BasePlugin]:
    return list(_LOADED)


async def close_clients() -> None:
    """进程退出时关连接池。"""
    seen: set[int] = set()
    for inst in _LOADED:
        for c in (inst.client_fast, inst.client_long):
            if c is not None and id(c) not in seen and not c.is_closed:
                seen.add(id(c))
                await c.aclose()
