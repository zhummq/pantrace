"""新源模板：复制这个文件改个名字就是新插件。

加一个源的完整步骤：
1. 复制本文件为 `<源名>.py`（**不要**用下划线开头，那样不会被加载）
2. 改 `name` / `level`，实现 `fetch`
3. 在 `.env` 的 `ENABLED_SOURCES` 里加上这个 `name`

主流程、缓存、超时、工作池、失败回退全部由 BasePlugin 处理，这里不用管。
"""

from __future__ import annotations

import httpx

from ..base import BasePlugin
from ..observe import HttpStatusError
from ..registry import register
from ..types import Link, SearchResult

API = "https://example.com/api/search"


@register
class TemplatePlugin(BasePlugin):
    # 缓存键的第一段，必须全局唯一
    name = "template"
    # 1 = 一手源（TG 频道/自建采集），2 = 开放接口，3 = 网页解析，4 = 二手聚合
    level = 3

    async def fetch(self, client: httpx.AsyncClient, keyword: str, ext: dict) -> list[SearchResult]:
        resp = await client.get(API, params={"q": keyword, "page": ext.get("page", 1)})

        # 非 200 一律抛 HttpStatusError，不要 return []。
        # return [] 会让"限流/接口已删"和"这个源真没内容"变成同一件事，
        # 最终污染存活统计和缓存，而且事后查不出来。
        if resp.status_code != 200:
            raise HttpStatusError(resp.status_code, self.name, resp.text[:200])

        data = resp.json()
        if not isinstance(data.get("list"), list):
            raise ValueError(f"返回结构变了，没有 list 字段：{list(data)[:5]}")

        out: list[SearchResult] = []
        for item in data["list"]:
            link = Link(
                type=item.get("disk_type", ""),
                url=item.get("link", ""),
                password=item.get("password", ""),
            )
            if not link.url:
                continue
            out.append(
                SearchResult(
                    unique_id=f"{self.name}_{item.get('id', '')}",
                    title=item.get("title", ""),
                    content=item.get("desc", ""),
                    datetime=float(item.get("share_time") or 0),
                    # 无提取码的链接也要保留，不许因为没密码就丢掉
                    links=[link],
                )
            )
        return out
