"""搜索编排：把启用的源并发跑一轮 → 合并去重 → 排序 → 本地分页。

这是 `/api/search` 的唯一取数入口，替代原来的 `providers.search()` 同步单通道：
以前是一条上游、一次调用、结果直接透传；现在是多个插件、各自有 4~6 秒窗口和
`complete` 语义缓存，出口再统一成前端要的 items 形状。
## 分页为什么在本地

pansou 的插件 `fetch()` 只返回结果列表，没有 `total` 这类元信息（它整个搜索就是
"多源抓一批 + 本地切片"）。所以这里沿用同一套：一次抓 `PAGES_PER_PLATFORM` 页，
出口按 page/page_size 切。好处是同一批数据翻页稳定、不会为第 2 页重复打上游；
代价是能翻的页数受抓取量限制（haisou 默认 5 平台 × 2 页 × 50 = 500 条，
去重后按 20/页通常还有十几页）。

## 失败怎么处理

单个源失败**不影响**其他源：结果照常合并返回，failure 进日志和 `_meta.sources`。
只有"所有源都失败且一条结果都没有"才抛错 —— 这时返回空列表会让用户以为
"网上没这个资源"，而真实原因是源全挂了。
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import get_settings
from . import adapter, registry
from .merge import merge_search_results, sort_results_by_time_and_keywords
from .outcome import BatchOutcome
from .types import PluginOutcome

log = logging.getLogger("pantrace.search.engine")

# 每个源**每种网盘类型**抓几页。与 pansou `haisou.go:23 DefaultPagesPerType` 同值。
# 实际抓取量 = 平台数 × 页数 × 50 条（haisou 默认 5 平台 × 2 页 = 500 条），
# 按 20 条/页够翻 25 页，远超真实使用深度。别调大：每多一页是所有平台各多一次请求。
PAGES_PER_PLATFORM = 2


class SearchEngineError(RuntimeError):
    """整轮搜索不可用（没有启用的源 / 所有源都失败）。main.py 据此返回 504。"""


# 最近一轮各源的结果摘要。`liveness` 要攒够 5 轮才下结论，线上刚重启时
# `/api/health` 里什么都看不到——运维最想知道的"刚才那次谁失败了"就在这个变量里。
_LAST_ROUND: dict[str, dict] = {}


def last_round() -> dict[str, dict]:
    """给 /api/health 用。进程级，跟着实例走，重启即清空。"""
    return dict(_LAST_ROUND)


def _ext(platforms: list[str], scope: str, force_refresh: bool = False) -> dict:
    settings = get_settings()
    ext = {
        # 参数名对齐 pansou（`ext["pages_per_type"]`），插件直接读这个键。
        "pages_per_type": PAGES_PER_PLATFORM,
        "page_size": max(1, min(settings.max_page_size, 50)),
        # 空平台**不退化成全平台**：一次全平台 = 5 平台 × 2 页 = 10 个计费请求，
        # 而用户什么都没选就烧掉 10 份额度是不可接受的。只兜一个默认盘。
        "platforms": platforms or [settings.default_platform],
        "scope": scope,
    }
    if force_refresh:
        # 键名对齐 pansou 的 `ext["refresh"]`，BasePlugin 靠它跳过插件级缓存。
        # `ext_digest` 的 `_NO_DIGEST_KEYS` 已排除 refresh — 刷新和普通查询共用
        # 同一份插件缓存，否则刷新出来的结果会写进另一个键，普通查询永远读不到。
        ext["refresh"] = True
    return ext


def _as_error(outcome: PluginOutcome) -> BaseException | None:
    """PluginOutcome.failure 是枚举，BatchOutcome 要的是异常。"""
    if outcome.failure is None:
        return None
    return RuntimeError(outcome.message or outcome.failure.value)


def _filter_platforms(items: list[dict], platforms: list[str]) -> list[dict]:
    """按用户选的网盘过滤出口。

    haisou 是"只查选中的平台"，本来就不会串；但 TG 频道里的分享横跨所有网盘，
    请求里的 platforms 对它没有意义。不过滤的话，用户点"百度网盘"会看到一堆
    夸克链接——筛选形同虚设。

    只在用户明确选了平台时过滤：`platforms` 为空是"不限平台"，不是"只要空平台的"。
    """
    if not platforms:
        return items
    wanted = set(platforms)
    return [item for item in items if item.get("platform") in wanted]


# ---- 通道分层：TG 是基础通道，计费接口只做兜底 ----
#
# 这一点和 pansou 的 `sourceType` 语义不同，是**故意的**：
# pansou 的 all 是 "TG ∥ 插件" 两条分支同时跑；本项目 TG 是主力，haisou 是按量计费
# 的兜底。所以走串行降级：TG 拿到可用结果就结束，一条额度都不花；TG 空/失败/没覆盖
# 用户选的网盘，才去调接口。
TIER_TG = "tg"
TIER_PLUGIN = "plugin"


class SourceNotEnabledError(SearchEngineError):
    """请求指定的通道没启用（例如 src=tg 但 ENABLED_SOURCES 里没有 TG 源）。

    单独一个类是为了让出口返回 400 而不是 504：上游没坏，是你要的通道没开，
    这两件事在运维眼里完全不同 —— 504 会被当成"服务挂了"。
    """


def _split_tiers(plugins: list) -> tuple[list, list]:
    """按 source_kind 拆成 (TG 层, 兜底层)。保持配置里的顺序。"""
    tg: list = []
    rest: list = []
    for plugin in plugins:
        if getattr(plugin, "source_kind", "plugin") == TIER_TG:
            tg.append(plugin)
        else:
            rest.append(plugin)
    return tg, rest


def _covers_platforms(results: list, platforms: list[str]) -> bool:
    """这一层的结果里有没有用户要的网盘。

    TG 频道的分享横跨所有网盘，用户选"百度"时它可能只返回夸克——这时候
    虽然"拿到了结果"，但这个结果对用户没用，仍然得往下兜底。
    `platforms` 为空是"不限平台"，有任何结果就算覆盖。
    """
    if not results:
        return False
    if not platforms:
        return True
    wanted = set(platforms)
    return any(adapter.platform_of(r) in wanted for r in results)


async def search(
    query: str,
    platforms: list[str],
    page: int,
    page_size: int,
    scope: str,
    source: str = "auto",
    force_refresh: bool = False,
) -> dict:
    """返回结构与 `providers.search()` 一致：items + pagination + _meta。

    `source` 对齐 pansou 的 `src`，但 **all/auto 的含义是分层降级而非并行**：

    - `auto` / `all`：TG 先跑，拿不到可用结果再跑计费接口（默认）
    - `tg`：只跑 TG，绝不碰接口（调试用，也保证零成本）
    - `plugin`：只跑接口，跳过 TG（TG 挂了时手动切）

    `force_refresh` 只影响**插件级缓存**（`ext["refresh"]`）：跳过读、结果照常写回。
    响应层缓存在 main.py，那边要各自判断，不在这里管。

    缓存在 main.py 里按 source 分了键，`auto` 与 `tg` 不会互相污染。
    """
    plugins = registry.loaded()
    if not plugins:
        raise SearchEngineError(
            "没有启用任何搜索源（ENABLED_SOURCES 为空或全部加载失败）"
        )

    tg_plugins, fallback_plugins = _split_tiers(plugins)

    # 组装本次要依次尝试的层。空层直接跳过 —— 只启用 haisou 时就是"直接走接口"，
    # 和改造前行为一致，不会因为多了 TG 这层而报错。
    if source == TIER_TG:
        tiers = [(TIER_TG, tg_plugins)]
    elif source == TIER_PLUGIN:
        tiers = [(TIER_PLUGIN, fallback_plugins)]
    else:
        tiers = [(TIER_TG, tg_plugins), (TIER_PLUGIN, fallback_plugins)]

    tiers = [(kind, plugs) for kind, plugs in tiers if plugs]
    if not tiers:
        raise SourceNotEnabledError(
            f"{source} 通道没有启用任何源（当前已启用：{', '.join(p.name for p in plugins)}）"
        )

    started = time.perf_counter()
    ext = _ext(platforms, scope, force_refresh)

    batch = BatchOutcome()
    submitted: list[str] = []
    round_info: dict[str, dict] = {}
    results: list = []
    used_tier = ""

    for kind, plugs in tiers:
        # 各源自己就有窗口和超时，这里只负责并发起跑。BasePlugin.search() 从不外抛：
        # 失败会包进 PluginOutcome.failure，所以不用 return_exceptions。
        outcomes = await asyncio.gather(*(p.search(query, ext) for p in plugs))
        collected: list = []
        for outcome in outcomes:
            collected.extend(outcome.results)
            batch.observe(
                outcome.source, len(outcome.results),
                _as_error(outcome), timed_out=outcome.pending,
            )
            submitted.append(outcome.source)
            round_info[outcome.source] = {
                "count": len(outcome.results),
                # pending = 窗口到点还没抓完，不等于失败；前端拿到的可能是旧缓存。
                "pending": outcome.pending,
                "failure": outcome.failure.value if outcome.failure else None,
                "message": outcome.message[:160] if outcome.message else "",
                "tier": kind,
            }

        if _covers_platforms(collected, platforms):
            results = collected
            used_tier = kind
            break
        # 没覆盖 → 留给下一层。TG 有结果但盘不对时，宁可花一次额度也不给用户空手。
        if collected:
            log.info(
                "engine q=%s %s 层有 %d 条但没覆盖平台 %s，继续兜底",
                query, kind, len(collected), platforms,
            )

    batch.finalize(submitted)

    # 先记诊断再判错：全挂是最需要排查的场景，抛出去之后就没机会记了。
    _LAST_ROUND.clear()
    _LAST_ROUND.update(round_info)

    if not results and submitted and batch.failed == len(submitted):
        # 全挂 ≠ 没内容。抛出去让 main 返回 504，别让用户以为网上没有。
        detail = "; ".join(f"{name}: {err}" for name, err in batch.last_errors.items())
        raise SearchEngineError(f"所有搜索源都失败（{detail}）")

    # 先去重（同 unique_id 保完整度高的），再排序（时间 + 关键词 + 源等级）。
    merged = merge_search_results([], results)
    sort_results_by_time_and_keywords(merged)

    items = adapter.to_items(merged)
    items = _filter_platforms(items, platforms)
    page_items, pagination = adapter.paginate(items, page, page_size)

    took_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "engine q=%s page=%s 层=%s 源=%s 合并=%s 返回=%s %sms",
        query, page, used_tier or "-", len(submitted), len(merged), len(page_items), took_ms,
    )
    if batch.failed:
        log.warning("本轮有 %d 个源失败：%s", batch.failed, batch.last_errors)

    return {
        "items": page_items,
        "pagination": pagination,
        "_meta": {
            # 上游计费字段在插件层没保留（pansou 的 fetch 不返回元信息），
            # 保留这个键只是不让 main.py 和前端拿不到字段。
            "credits": 0,
            # 这一轮最后是哪个层出的结果：tg = 没花额度，plugin = 兜底花了。
            # 排查"为什么这次慢了/扣钱了"先看它。
            "tier": used_tier,
            "sources": {
                name: {"count": info["count"], "failure": info["failure"]}
                for name, info in round_info.items()
            },
        },
    }


