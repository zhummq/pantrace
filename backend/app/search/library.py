"""本地资源库检索层：把 80 万条索引当成第三个可查的地方。

## 为什么不做成插件

整个搜索架构是插件化的，第一反应肯定是再加一个 `library` 插件。不行，四个理由：

1. **健康统计会被污染**。`liveness` 会把本地库记成永远健康的源，拉高整体健康率，
   真实上游出问题时反而不显眼。
2. **`_pick_plugins` 的通道分流会坏**。库要么伪装成 `plugin`（`src=plugin` 时被
   强行带上，等于偷偷合并），要么新增 `source_kind`，把两档通道改成三档。
3. **BasePlugin 的开销全是白付的**。4~6 秒窗口、`complete` 语义缓存、失败分类、
   工作池——对一次几十毫秒的本地 SQLite 查询毫无意义。
4. **同步阻塞**。`store.search()` 是同步 SQLite，塞进 async 插件里也要
   `asyncio.to_thread` 绕一圈，最后还是回到"单独一层"。

所以本模块不进注册表、不进 `liveness`、不出现在 `/api/health` 的 `sources` 里。

## 探活不在这里做

**这一层不探活，也绝不阻塞等探活。** 库里的链接是否还活着，走的是既有的那条
现成链路：`_export_search` 统一 `check.annotate()` 贴结论 + `check.schedule()`
后台探，前端 `scheduleCheckRefresh` 在 6s / 16s 后补拉一次把角标刷新。

这样安排的三个理由：
- 一页 20 条、每条 1~3 秒、3 线程，同步探要等 7~20 秒。搜索不能为验链接付这个时间。
- `check.py` 模块头的三条硬约束第一条就是"不在搜索链路里同步做"，别破。
- 探活结论是**共享的**：同一个链接无论从 tg、haisou 还是本地库出来，结论都在
  `link_check` 表里按 `platform + url` 存一份。库不需要自己那套。

本层唯一多做的事：把 `checkState` 带出去，让出口能判"这条该不该显示"。

## 分页

`store.search()` 自己做分页（FTS 硬筛 → 候选集算分 → 切片），这里只把
pagination 补齐成和 `engine.search()` 一样的形状，前端不用为库单独写一套。
"""

from __future__ import annotations

import logging
import math

from .. import store

log = logging.getLogger("pantrace.search.library")

TIER = "library"


def is_available() -> bool:
    """库是否可查。关闭状态由调用方（main）先判，这里只兜底。"""
    try:
        return store.is_ready()
    except Exception:  # noqa: BLE001
        return False


def search(
    query: str,
    platforms: list[str] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """查库。返回结构对齐 `engine.search()`：items + pagination + _meta。

    同步函数。调用方在 async 上下文里必须用 `asyncio.to_thread` —— SQLite 查询
    在事件循环线程里跑，库一大就会把整个进程卡住。
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 50))

    got = store.search(query, platforms or [], page, page_size)
    items = got["items"]
    total = int(got["total"])
    total_pages = max(1, math.ceil(total / page_size)) if total else 0

    return {
        "items": items,
        "pagination": {
            "page": page,
            "pageSize": page_size,
            "total": total,
            "totalPages": total_pages,
        },
        "_meta": {
            # 库不烧额度，但字段要占位：main 的响应组装和前端都按同一个形状读。
            "credits": 0,
            "tier": TIER,
            # 库这一档没有"标题不含关键词被丢掉"这回事：FTS 命中就是命中。
            "filtered": 0,
            # 候选集被 bm25 截断过，total 是下界。出口需要如实标注，
            # 否则前端会把"1000+"显示成正好 1000。
            "truncated": bool(got.get("truncated")),
            "sources": {
                "library": {
                    "count": len(items),
                    "failure": None,
                    **({"truncated": True} if got.get("truncated") else {}),
                }
            },
        },
    }
