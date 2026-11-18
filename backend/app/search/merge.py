"""合并去重与排序。逐行翻译 pansou 的 `service/search_service.go`。

对照关系：

| 本文件 | pansou 源码 |
|---|---|
| `merge_search_results` | `search_service.go:106-140` |
| `generate_result_key` | `:143-152`（已在 `types.SearchResult.result_key`） |
| `select_better_result` | `:155-164` |
| `calculate_completeness_score` | `:167-199`（已在 `types.SearchResult.completeness_score`） |
| `ResultScore` | `:1875-1881` |
| `get_result_source` | `:1889-1901` |
| `get_plugin_level_by_source` | `:1904-1932` |
| `get_plugin_priority_by_name` | `:1935-1940` |
| `get_plugin_level_score` | `:1941-1956` |
| `calculate_time_score` | `:1959-1984` |
| `sort_results_by_time_and_keywords` | `:657-689` |
| `get_keyword_priority` | `:691-701` |

两处语言差异：

1. **排序稳定性**。Go 用 `sort.Slice`（不稳定），并列项顺序本来就随机；
   Python 的 `sorted` 稳定，并列项会保持输入顺序。结果集相同，只是并列时的先后不同，
   而并列项本来就没有确定顺序，所以这里不额外打散。
2. **`sync.Map` → dict**。等级缓存原本是并发安全的，Python 单线程事件循环下，
   `dict` 的读写在无 await 的同步代码里是原子的，行为等价。

其余数字全部照抄，包括与注释不一致的那个：`getKeywordPriority` 注释写"最高400分"，
代码是 `(len(priorityKeywords)-i)*70`，7 个关键词时最高 490。**以代码为准**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .types import SearchResult

# search_service.go:54。顺序即优先级，索引越小分越高。
PRIORITY_KEYWORDS: tuple[str, ...] = ("合集", "系列", "全", "完", "最新", "附", "complete")

# 等级缓存。对应 `pluginLevelCache sync.Map`。
_PLUGIN_LEVEL_CACHE: dict[str, int] = {}


@dataclass
class ResultScore:
    """search_service.go:1875。三项分数分开存，出问题能看出是哪一项在起作用。"""

    result: SearchResult
    time_score: float = 0.0
    keyword_score: int = 0
    plugin_score: int = 0
    total_score: float = 0.0


# ---------------------------------------------------------------- 合并


def merge_search_results(
    existing: list[SearchResult],
    new_results: list[SearchResult],
) -> list[SearchResult]:
    """search_service.go:106。同键取**信息更完整**的那个，不是保留先到的。"""
    result_map: dict[str, SearchResult] = {}

    for result in existing:
        result_map[result.result_key()] = result

    for new_result in new_results:
        key = new_result.result_key()
        if key in result_map:
            result_map[key] = select_better_result(result_map[key], new_result)
        else:
            result_map[key] = new_result

    merged = list(result_map.values())
    merged.sort(key=lambda r: r.datetime, reverse=True)
    return merged


def select_better_result(existing: SearchResult, new: SearchResult) -> SearchResult:
    """search_service.go:155。分数相同保留旧的那个。"""
    if new.completeness_score() > existing.completeness_score():
        return new
    return existing


# ---------------------------------------------------------------- 来源与等级


def get_result_source(result: SearchResult) -> str:
    """search_service.go:1889。`tg:<频道>` 或 `plugin:<插件名>`。"""
    if result.channel:
        return "tg:" + result.channel
    if result.unique_id and "-" in result.unique_id:
        parts = result.unique_id.split("-", 1)
        return "plugin:" + parts[0]
    return "unknown"


def get_plugin_priority_by_name(plugin_name: str) -> int:
    """search_service.go:1935。查不到就按等级 3。"""
    from . import registry

    cls = registry.get_registered_class(plugin_name)
    if cls is None:
        return 3
    return getattr(cls, "level", 3)


def get_plugin_level_by_source(source: str) -> int:
    """search_service.go:1904。TG 等同于等级 3。"""
    cached = _PLUGIN_LEVEL_CACHE.get(source)
    if cached is not None:
        return cached

    parts = source.split(":")
    if len(parts) != 2:
        _PLUGIN_LEVEL_CACHE[source] = 3
        return 3

    if parts[0] == "tg":
        _PLUGIN_LEVEL_CACHE[source] = 3
        return 3

    if parts[0] == "plugin":
        level = get_plugin_priority_by_name(parts[1])
        _PLUGIN_LEVEL_CACHE[source] = level
        return level

    _PLUGIN_LEVEL_CACHE[source] = 3
    return 3


def get_plugin_level_score(source: str) -> int:
    """search_service.go:1941。等级差异直接是三位数分差，压过时间分。"""
    level = get_plugin_level_by_source(source)

    if level == 1:
        return 1000
    if level == 2:
        return 500
    if level == 3:
        return 0
    if level == 4:
        return -200
    return 0


# ---------------------------------------------------------------- 打分


def calculate_time_score(datetime: float) -> float:
    """search_service.go:1959。`datetime` 是 epoch 秒，0 表示无时间信息。"""
    if not datetime:
        return 0

    days_diff = (time.time() - datetime) / 3600 / 24

    if days_diff <= 1:
        return 500
    if days_diff <= 3:
        return 400
    if days_diff <= 7:
        return 300
    if days_diff <= 30:
        return 200
    if days_diff <= 90:
        return 100
    if days_diff <= 365:
        return 50
    return 20


def get_keyword_priority(title: str) -> int:
    """search_service.go:691。命中第一个就返回，不再往下看。"""
    title = title.lower()
    total = len(PRIORITY_KEYWORDS)
    for i, keyword in enumerate(PRIORITY_KEYWORDS):
        if keyword in title:
            return (total - i) * 70
    return 0


def sort_results_by_time_and_keywords(results: list[SearchResult]) -> list[SearchResult]:
    """search_service.go:657。原地排序并返回同一个列表。

    总分 = 时间分 + 关键词分 + 插件等级分。三项都可解释，不是黑盒排序。
    """
    scores: list[ResultScore] = []

    for result in results:
        source = get_result_source(result)
        item = ResultScore(
            result=result,
            time_score=calculate_time_score(result.datetime),
            keyword_score=get_keyword_priority(result.title),
            plugin_score=get_plugin_level_score(source),
        )
        item.total_score = item.time_score + item.keyword_score + item.plugin_score
        scores.append(item)

    scores.sort(key=lambda s: s.total_score, reverse=True)

    for i, item in enumerate(scores):
        results[i] = item.result

    return results
