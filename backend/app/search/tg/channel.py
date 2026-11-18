"""单频道抓取。

逐行翻译 pansou `service/search_service.go:709-765`（`searchChannelWithContext`）
与 `util/http_util.go:306`（`BuildSearchURL`）。

三条判定不能省：
- **非 200 直接判失败**，不把错误页当成"频道没有匹配内容"（`observe.py` 的 HttpStatusError）。
- **响应体有上限**，避免异常响应把内存吃满。
- **结构变化要显式告警**，而不是静默返回空。本地无法访问 t.me，线上出问题唯一的信号就是这行告警。
"""

from __future__ import annotations

import logging
from urllib.parse import quote, quote_plus

import httpx

from ..observe import HttpStatusError
from .parse import PARSE_STATUS_STRUCTURE_CHANGED, TgParseError, parse_search_results_with_status
from ..types import SearchResult

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL_REQUEST_TIMEOUT = 4.0
DEFAULT_RESPONSE_MAX_BYTES = 2 * 1024 * 1024


def build_search_url(channel: str, keyword: str = "", next_page_param: str = "") -> str:
    """http_util.go:306 `BuildSearchURL`。

    channel 直接来自配置，仍必须转义：否则可注入 `?` `/` `#` 与空格。
    合法频道名只有字母数字下划线，转义对它们是无操作。
    """
    base_url = "https://t.me/s/" + quote(channel, safe="")
    if keyword:
        base_url += "?q=" + quote_plus(keyword, safe="")
        if next_page_param:
            base_url += "&" + next_page_param
    return base_url


async def search_channel(
    keyword: str,
    channel: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_CHANNEL_REQUEST_TIMEOUT,
    max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
) -> list[SearchResult]:
    """search_service.go:709 `searchChannelWithContext`。

    频道请求自身的超时保留，非 200 判失败，响应体有上限。
    """
    search_url = build_search_url(channel, keyword)

    own_client = False
    if client is None:
        client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,  # 和 use_env_proxy=False 对齐
            follow_redirects=True,
        )
        own_client = True

    try:
        async with client.stream("GET", search_url) as resp:
            # 429/403/5xx 都不是可用页面，按失败上报，
            # 这样"频道被限流"与"频道没有匹配内容"不会混为一谈
            if resp.status_code != 200:
                raise HttpStatusError(resp.status_code, source=channel)

            # 读响应体（带上限）。Go 用 io.LimitReader 截断后照常解析，
            # 这里同样截断后交给 lxml —— 它对截断的 HTML 是容错的。
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) >= max_bytes:
                    del buf[max_bytes:]
                    break
            body = bytes(buf)
    finally:
        if own_client:
            await client.aclose()

    try:
        results, parse_status = parse_search_results_with_status(
            body.decode("utf-8", "replace"), channel
        )
    except TgParseError as exc:
        raise TgParseError(f"频道 {channel} 页面解析失败: {exc}") from exc

    # 页面含消息块却解析不出条目，通常是 t.me 改版；显式告警而不是静默返回空
    if parse_status == PARSE_STATUS_STRUCTURE_CHANGED:
        logger.warning(
            "[searchTG] 频道 %s 的结果页含消息块但未解析出任何条目，页面结构可能已变", channel
        )

    return results
