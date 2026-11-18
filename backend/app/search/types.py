"""搜索结果的核心数据结构。

逐字段对照 pansou 的 `model/response.go` 与 `plugin/plugin.go` 里的 `cachedResponse`。
字段名与 Go 保持一致（Python 侧转 snake_case），目的是**跨语言对照代码时不用翻译**。

不要往这里加"顺手想到的"字段。多一个字段，去重键和完整度打分就要跟着改，
而那两个函数的正确性直接决定同一资源会不会重复出现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


@dataclass
class Link:
    """一条网盘链接。对应 pansou `model.Link`。

    注意 `url` 和 `password` 是**分开存**的：百度链接必须剥掉 `?pwd=`，
    密码单独放，输出时再拼回去。否则同一个链接的两种写法去不掉重。
    """

    type: str = ""          # 网盘类型：quark / baidu / aliyun / uc / xunlei / 123 / 115
    url: str = ""           # 干净的链接（百度已剥 ?pwd=）
    password: str = ""      # 提取码，可为空 —— 无码链接必须保留，不许丢
    datetime: float = 0.0   # 链接更新时间，0 = 未知
    work_title: str = ""    # 同一消息里有多个作品时用于区分


@dataclass
class SearchResult:
    """一条搜索结果 = TG 的一条消息，或插件返回的一个条目。"""

    unique_id: str = ""
    message_id: str = ""
    channel: str = ""
    title: str = ""
    content: str = ""
    datetime: float = 0.0
    links: list[Link] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    # 本项目的扩展位：承载上游原始字段（hsid / fileCount / sizeBytes / shareCode …），
    # 出口要按它们还原成前端那张卡。pansou 没有这个字段（它只面向网页展示）。
    # **不参与去重键、不参与完整度打分** —— 加了就要连着改那两个函数，
    # 而它们的正确性直接决定同一资源会不会重复出现。
    extras: dict = field(default_factory=dict)

    def result_key(self) -> str:
        """去重键。三级降级，对应 pansou 的 `generateResultKey`。

        第三级用 title 而不是 URL：同一条资源在多个频道转发时标题一致、
        unique_id 不同，这一级才兜得住。带上 channel 是为了避免不同频道
        的同名资源被误合并。
        """
        if self.unique_id:
            return self.unique_id
        if self.message_id:
            return self.message_id
        return f"title_{self.title}_{self.channel}"

    def completeness_score(self) -> int:
        """完整度打分。对应 pansou 的 `calculateCompletenessScore`。

        同 key 撞上时**取完整度高的，不是保留先到的** —— 这是它比多数实现
        做得好的地方：先到的往往是信息最少的那个。
        """
        score = 0
        if self.unique_id:
            score += 10
        if self.links:
            score += 5 + len(self.links)
        if self.content:
            score += 3
        # Go 的 len(string) 是字节数：10 个汉字的标题在 pansou 里是 +3 分，不是 +1。
        # 按字符算会让长中文标题在两边的排序权重不一致，这里必须按字节。
        score += len(self.title.encode("utf-8", "surrogatepass")) // 10
        if self.channel:
            score += 2
        score += len(self.tags)
        return score


@dataclass
class CachedResponse:
    """单源缓存条目。对应 pansou 的 `cachedResponse`。

    `complete` 是这个结构存在的唯一理由：它让缓存能表达"这条结果不完整，
    下次该重抓"，而不是把 4 秒超时返回的空结果当权威缓存钉住一小时。
    """

    results: list[SearchResult] = field(default_factory=list)
    timestamp: float = 0.0
    complete: bool = False
    last_access: float = 0.0
    access_count: int = 1

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.timestamp


@dataclass
class PluginOutcome:
    """单源一次搜索的产出。对应 pansou 的 `model.PluginSearchResult`。"""

    source: str = ""
    results: list[SearchResult] = field(default_factory=list)
    is_final: bool = False          # False = 超时返回的部分结果，后台还在跑
    timestamp: float = field(default_factory=time.time)
    message: str = ""
    failure: "FailureClass | None" = None   # 有值 = 这一源本轮失败，且原因可分类

    @property
    def pending(self) -> bool:
        """后台还在补。用于批次统计判断这一源算"超时未完成"。"""
        return not self.is_final and self.failure is None


class ParseStatus(str, Enum):
    """页面解析结果的可信度。对应 pansou 的 `PageParseStatus`。

    存在的意义：区分"这页真的没内容"和"站点改版了解析失效"。
    **不能用 len(results) == 0 判断失效** —— 消息识别成功但整条不含网盘链接
    时同样是 0 条，那是正常页面。
    """

    OK = "ok"
    NO_MESSAGES = "no_messages"                # 页面明确显示"没有消息"
    STRUCTURE_CHANGED = "structure_changed"    # 有消息块但一条都认不出 → 站点改版

    @property
    def is_suspicious(self) -> bool:
        return self is ParseStatus.STRUCTURE_CHANGED


class FailureClass(str, Enum):
    """失败分类。对应 pansou `service/batch_search.go` 的 `failureClass`。

    **非 200 一律算失败，不算"零结果"。** 限流被记成"这个源没内容"，
    会同时污染存活统计和缓存，而且查不出来。
    """

    TIMEOUT = "timeout"              # 超时
    CANCELLED = "cancelled"          # 调用方取消
    NETWORK = "network"              # DNS / 连接 / TLS
    RATE_LIMITED = "rate_limited"    # 429
    FORBIDDEN = "forbidden"          # 403
    NOT_FOUND = "not_found"          # 404
    SERVER_ERROR = "server_error"    # 5xx
    HTTP_STATUS = "http_status"      # 其它状态码
    STRUCTURE_CHANGED = "structure_changed"   # 200 但解析不出任何条目
    OTHER = "other"

    @property
    def retryable(self) -> bool:
        """能不能重试。对应 pansou 的 `Abort` 语义。

        404 是接口已删、403 是永久封 —— 重试等于给死源加负载，
        而且会挤占 4 秒窗口里别的源的时间。
        """
        return self in (
            FailureClass.TIMEOUT,
            FailureClass.NETWORK,
            FailureClass.SERVER_ERROR,
            FailureClass.RATE_LIMITED,
        )
