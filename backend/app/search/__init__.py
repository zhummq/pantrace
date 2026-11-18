"""搜索核心层：插件机制 / 异步执行 / 缓存 / 清洗 / 合并。

移植自 pansou，但**不接它的源**。机制清单与逐项对照见
`docs/pansou-机制移植方案.md`。

模块分工：
- `types`     数据结构（Link / SearchResult / CachedResponse / PluginOutcome）
- `observe`   失败分类 + 源存活分级
- `pool`      非阻塞工作池 + 全局计数
- `cache`     L1 内存 + L2 SQLite，`complete` 语义，合并写
- `base`      插件基类：4 秒窗口 / shield / 双客户端 / 失败回退
- `registry`  自注册 + 白名单（默认全关）
- `clean`     链接类型判定 / 七类清洗 / 提取码提取 / 就近取码 / 六类归并
- `merge`     合并去重 + 完整度 + 三段打分排序
"""

from .base import BasePlugin, build_clients, ext_digest
from .cache import SearchCache, merge_results
from .clean import (
    LinkCandidates,
    extract_net_disk_links,
    extract_password,
    get_link_type,
    is_supported_link,
    password_for,
)
from .merge import (
    merge_search_results,
    sort_results_by_time_and_keywords,
)
from .observe import HttpStatusError, Liveness, classify_failure, liveness
from .outcome import BatchOutcome
from .pool import WorkerPool, stats
from .types import CachedResponse, FailureClass, Link, ParseStatus, PluginOutcome, SearchResult

__all__ = [
    "BasePlugin",
    "build_clients",
    "ext_digest",
    "SearchCache",
    "merge_results",
    "LinkCandidates",
    "extract_net_disk_links",
    "extract_password",
    "get_link_type",
    "is_supported_link",
    "password_for",
    "merge_search_results",
    "sort_results_by_time_and_keywords",
    "HttpStatusError",
    "Liveness",
    "classify_failure",
    "liveness",
    "BatchOutcome",
    "WorkerPool",
    "stats",
    "CachedResponse",
    "FailureClass",
    "Link",
    "ParseStatus",
    "PluginOutcome",
    "SearchResult",
]
