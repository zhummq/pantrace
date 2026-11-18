"""TG 公开频道源插件（pansou 的 `searchTG` 通道）。

pansou 里 TG 不是插件，而是一条**独立阶段**：先跑插件聚合，再把 TG 频道结果并进来
（`search_service.go:1363 searchTG`）。本项目所有源统一走插件层，所以这里只做一层
薄包装，判定逻辑一条都不重复实现：

| 职责 | 归属 |
|---|---|
| t.me 可达性门（连续 2 次失败才关门） | `tg/gate.py` |
| 单频道抓取 + 结构变化检测 | `tg/channel.py` |
| 多频道并发 / 全失败不写缓存 / 后台补齐 | `tg/service.py` |
| 缓存、超时窗口、失败分类、工作池 | `BasePlugin`（本文件不碰） |

## 一处必须偏离 pansou 的地方

pansou 的可达性门是**在 searchTG 外面**判的：门关 → 整条通道直接跳过，不写缓存、
不计频道失败。插件化之后，"直接跳过"在语义上等于"返回空列表"，而空列表会被
`Liveness` 记成 `zero_yield`——把"根本没连上"误判成"这个源改版了，什么都抓不到"。

所以门关时这里抛 `TGUnreachable`（继承 `httpx.ConnectError`，由 `classify_failure`
归到 NETWORK）：不写缓存这一点保住了，健康页也能一眼看出是网络不通，而不是源坏了。

## 缓存键对齐

覆盖 `cache_key()`，直接返回 `tg_cache_key(keyword, channels)`，让**外层插件缓存和
`search_tg` 内部缓存用同一个键**。否则后台补齐写进 `tg:` 键的结果，外层
`tg_searchers:` 键读不到——补齐等于白干（pansou 里这两层本来就是一份缓存）。
"""

from __future__ import annotations

import httpx

from ...config import get_settings
from ..base import BasePlugin
from ..tg import config_store as tg_config
from ..registry import register
from ..tg import TG, search_tg, tg_cache_key
from ..types import SearchResult

# 单频道一次抓取实测 1~3 秒（122K 页面）；4 秒的默认窗口会把慢一点的整批挤成
# partial。多频道是并发的，所以窗口不随频道数增长。
DEFAULT_RESPONSE_TIMEOUT = 8.0


class TGUnreachable(httpx.ConnectError):
    """t.me 当前不可达（门关）。

    继承 `httpx.TransportError` 分支，`classify_failure` 会归到 NETWORK——
    这和"频道改版了抓不到东西"（ZERO_YIELD）必须在健康页上分得开。
    """


def _channels(value) -> list[str]:
    """优先级：ext 指定 > 库中配置（后台改的）> .env 的 TG_CHANNELS。

    `@name` 和 `name` 等价。ext 用于单通道强制查询，不能被后台配置盖掉。
    """
    if isinstance(value, str):
        value = value.split(",")
    if isinstance(value, (list, tuple)):
        channels = [str(c).strip().lstrip("@") for c in value if str(c).strip()]
        if channels:
            return channels
    return tg_config.effective(get_settings().tg_channel_list)


@register
class TGSeachersPlugin(BasePlugin):
    name = "tg_searchers"
    # pansou 里 TG 是独立通道（`sourceType == "tg"`），src=tg 时只跑它。
    # 这里是插件、但类型是 tg，引擎靠这个属性分流。
    source_kind = "tg"
    # 频道里是别人转存再公开的分享链接，属于一手来源（与 haisou 同级）
    level = 1
    response_timeout = DEFAULT_RESPONSE_TIMEOUT

    def cache_key(self, keyword: str, ext: dict | None = None) -> str:
        return tg_cache_key(keyword, _channels((ext or {}).get("channels")))

    async def fetch(
        self, client: httpx.AsyncClient, keyword: str, ext: dict
    ) -> list[SearchResult]:
        channels = _channels(ext.get("channels"))
        if not channels:
            # 没配频道 = 这个源没有任何采集目标。这不是"零结果"，是配置缺失。
            raise ValueError("TG 通道未配置任何频道（TG_CHANNELS 为空）")

        results = await search_tg(
            keyword,
            channels,
            # 复用插件缓存实例：键已经对齐，补齐写回能被外层读到
            cache=self.cache,
            client=client,
            force_refresh=bool(ext.get("refresh")),
        )

        # 门关时 search_tg 按 pansou 的语义返回空列表。这里要把它翻译成"失败"，
        # 否则会被当成"可达但零产出"写进存活统计。
        if not results and not TG.is_reachable():
            raise TGUnreachable(f"t.me 不可达：{TG.reason}")

        return results
