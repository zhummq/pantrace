"""插件产出 → 本项目 items：搜索链路上唯一的格式转换点。

pansou 的 `SearchResult` 是给网页展示用的（title / content / links），本项目前端要的
是另一套字段（hsid / shareUrl / shareCode / fileCount / sizeBytes）。两边必须有一个
明确的转换层，不能在插件里各写一份、也不能在响应出口临时拼。

## 为什么不在这里做清洗

`SearchResult.links` 可能来自两类源：

- 结构化源（haisou 官方 API）：链接本身是干净的，`extras` 里就是上游原值；
- 网页/TG 源：链接是从文本里扒出来的，已有的 `clean.py` 在入库前就洗过了。

所以这里**只做映射，不改链接**。再洗一遍只会把 `?pwd=` 之类合法参数改坏，
或者把上游给了短链的平台（天翼 / 微云）误判成无效链接。

## 分页

pansou 的分页是**本地切页**（它把多个源的结果合并后再分页），这里保持一致：
插件抓一批，出口按 page/page_size 切。好处是同一批数据翻页稳定、不会为了第 2 页
再打一次上游；代价是 page 不能超过本次抓取量（`engine.SEARCH_PAGES` 决定）。
"""

from __future__ import annotations

import math

from ..providers import PLATFORM_NAMES
from .types import SearchResult

# Link.type → 本项目平台 key。只有 aliyun 不一致（pansou 叫 aliyun，前端叫 ali）。
# 移动/联通在 pansou 里都归成 mobile，这里优先用 extras 里的原始值还原。
_LINK_TYPE_TO_KEY = {
    "aliyun": "ali",
    "mobile": "yidong",
}


def item_key(item: dict) -> str:
    """去重键。优先分享链接，其次 hsid / 标题。

    不能用 title：同一个资源在不同源里标题一致但链接不同是常态，
    按标题合并会把两条不同的分享吞掉。
    """
    return item.get("shareUrl") or item.get("hsid") or item.get("title") or ""


def platform_of(result: SearchResult) -> str:
    """这条结果属于哪个网盘。结构化源直接读 extras，网页/TG 源从链接类型反推。

    单独抽出来是因为**降级判定要用它**：TG 有结果、但没有用户要的那个盘，
    仍然得走兜底源，否则用户选了百度却拿到一堆夸克。判定发生在转换之前，
    不能为了看一眼平台就先 to_item 一遍。
    """
    extras = result.extras or {}
    platform = (extras.get("platform") or "").lower()
    if platform:
        return platform
    link = result.links[0] if result.links else None
    if link is None:
        return ""
    return _LINK_TYPE_TO_KEY.get(link.type, link.type)


def to_item(result: SearchResult) -> dict | None:
    """单条转换。没有链接的条目返回 None —— 前端点不开的东西不该占位置。"""
    link = result.links[0] if result.links else None
    extras = result.extras or {}

    url = extras.get("shareUrl") or (link.url if link else "")
    if not url:
        return None

    platform = platform_of(result)

    password = extras.get("sharePwd")
    if password is None:
        password = link.password if link is not None else ""

    return {
        "hsid": extras.get("hsid") or "",
        "platform": platform,
        "platformName": (
            extras.get("platformName")
            or PLATFORM_NAMES.get(platform)
            or "其他"
        ),
        "title": result.title or "未知资源",
        "shareUrl": url,
        # 与上游同口径：清掉就不会把分享 id 换个字段发出去（出口还会再清一次）
        "shareCode": extras.get("shareCode") or "",
        "sharePwd": password,
        "fileCount": int(extras.get("fileCount") or 0),
        "sizeBytes": int(extras.get("sizeBytes") or 0),
        "indexedTime": "",
    }


def to_items(results: list[SearchResult]) -> list[dict]:
    """批量转换 + 按链接去重（同 key 保留先到的，顺序已由 engine 排好）。"""
    seen: set[str] = set()
    items: list[dict] = []
    for result in results:
        item = to_item(result)
        if item is None:
            continue
        key = item_key(item)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def paginate(items: list[dict], page: int, page_size: int) -> tuple[list[dict], dict]:
    """本地切页。返回 (当前页 items, pagination)。

    `total` 是**本次抓到的总量**，不是上游的命中总数：插件层不保留上游 pagination
    （pansou 的 `fetch()` 只返回结果列表）。所以翻到最后一页就结束，不会出现
    "有 total 但拿不到内容"的空页。
    """
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    total = len(items)
    total_pages = max(1, math.ceil(total / page_size)) if total else 0
    start = (page - 1) * page_size
    return items[start:start + page_size], {
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
    }
