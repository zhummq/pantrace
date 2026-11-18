"""Telegram 公开频道搜索通道。

三块，对应 pansou 的三层：
- `parse`     t.me 页面 → SearchResult（`util/parser_util.go:146-928`）
- `gate`      可达性门，决定这一轮要不要走 TG（`service/tg_reachability.go`）
- `channel`   单频道抓取 + 结构变化检测（`service/search_service.go` 的 TG 阶段）

`parse` 不依赖网络，可以单测；`gate` / `channel` 依赖 t.me 可达性，只能在部署机验证。
"""

from .channel import build_search_url, search_channel
from .gate import (
    TG,
    TG_PROBE_REQUIRED_FAILURES,
    TGReachability,
    probe_tg_url,
    tg_probe_url,
)
from .parse import (
    PARSE_STATUS_NO_MESSAGES,
    PARSE_STATUS_OK,
    PARSE_STATUS_STRUCTURE_CHANGED,
    TgParseError,
    parse_search_results,
    parse_search_results_with_status,
)
from .service import search_tg, tg_cache_key

__all__ = [
    "build_search_url",
    "search_channel",
    "search_tg",
    "tg_cache_key",
    "TG",
    "TG_PROBE_REQUIRED_FAILURES",
    "TGReachability",
    "probe_tg_url",
    "tg_probe_url",
    "PARSE_STATUS_NO_MESSAGES",
    "PARSE_STATUS_OK",
    "PARSE_STATUS_STRUCTURE_CHANGED",
    "TgParseError",
    "parse_search_results",
    "parse_search_results_with_status",
]
