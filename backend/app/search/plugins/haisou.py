"""haisou 源插件（本项目官方 API 通道）。

**这个文件的骨架是 pansou 的插件机制，取数换成了本项目一直用的 haisou 官方 API
（idatariver MAPI，apikey + x-idr-sign）。** pansou 原版的网页两阶段实现已经删除，
不并存——同一个源挂两条通道会产生两份缓存、两份健康统计，日志里分不清是哪条在报错。

## 为什么必须换成官方 API

pansou 原版（`plugin/haisou/haisou.go`）是匿名爬 `haisou.cc`：

    搜索阶段  5 种网盘类型 × 页数          → 只拿到 hsid
    取链阶段  min(候选, 30) 个 hsid 各一次   → 才拿到 share_code
    小计     ≤ 35 个请求 / 次搜索

匿名额度是 100 点/天，等于**一天只够 2~3 次搜索**，只能当补充源。

官方 API 是另一个量级：**一次请求就返回 `shareUrl / shareCode / sharePwd /
fileCount / sizeBytes`**，不需要第二阶段取链。所以：

| | pansou 网页版 | 本项目官方 API |
|---|---|---|
| 请求数 / 次搜索 | 5 类型 × 2 页 + ≤30 取链 = ≤ 35 | **1 平台 × 2 页 = 2**（无取链阶段） |
| 链接 | 要二次取链，失败率高 | 一次给全 |
| 额度 | 匿名 100 点/天 | apikey 配额（实测 0.0001/次） |
| 分页 | 无（每个类型各自页码） | 有（page/pageSize/total） |
| 清洗 | 需要 HTML 标签 + 关键词兜底 | 结构化，无需清洗 |

## 并发结构照抄 pansou，但**默认只查一个盘**

上游那个 `platforms` 数组**实际不生效**：传 `["quark","baidu","ali"]`、传 11 个平台、
换数组顺序，返回的 50 条**永远是夸克**（实测 2026-11-18）。上游内部按固定优先级
填充页面，而夸克数据量最大（`total` 封顶 1000），所以第一页永远被夸克占满。
**一次请求只能拿到一个平台的数据**——这既是不传平台的坑，也正好是"省额度"的抓手。

pansou 的答案是拆成 N 个请求全并发（`haisou.go:142`）：

```go
for _, cloudType := range SupportedCloudTypes {
    for pageNo := 1; pageNo <= pagesPerType; pageNo++ {
        go func(cType string, page int) { ... }(cloudType, pageNo)
    }
}
```

这里保留同一套 `平台 × 页码` 矩阵与"全失败才报错"的语义，但 **`platforms` 由用户
在前端选**：选一个盘 = 1 平台 × 2 页 = **2 个计费请求**；空值时只兜
`settings.default_platform`（quark），**不再展开成 5 平台**。

为什么改这一处：原样照抄是 5 平台 × 2 页 = 10 个请求/次搜索。实测 10 并发会被上游
打回 2 个 `code 1003 i18n:server_too_frequency`，且额度消耗是单平台的 5 倍。
压到 5 并发后单平台搜索 2.7~4.8 秒、5 个盘全满，说明限流阈值就在 10 个并发附近。

`response_timeout = 10.0`：并发 5 的一整批实测最慢 6.8 秒，窗口要容得下它。

## 保留的 pansou 机制

自注册（`@register`）、4 秒级响应窗口（`response_timeout`）、双超时客户端、
`complete` 语义缓存 + 合并写、失败分类、`zero_yield` 判定、等级分（level）。
这些都是 `base.py` / `registry.py` 提供的，插件只负责"把这一源的数据取回来"。

## 失败语义（与 pansou 一脉相承的三条）

1. **非 200 不是"零结果"**，抛 `HttpStatusError`，让 `classify_failure` 分出
   429 / 5xx / 403。
2. **200 + code != 0 不是"零结果"**，抛 `UpstreamError`（带原始 code）。
   签名过期 1004、apikey 失效 1005 一律**不重试** —— 重试只会白烧额度。
3. **200 + 有条目但一条链接都没有** = 结构变了，抛 `ValueError`，不能返回空列表，
   否则会被 liveness 记成 `zero_yield`，源改版后所有健康检查还是绿的。

## 计费

官方要求请求超时 > 60 秒，否则提前中断仍会计费。所以 `_post()` 显式传
`settings.request_timeout`（默认 75 秒），**不要**用共享客户端的 30 秒默认值。
前台 6 秒窗口到点就返回旧缓存/空，后台任务仍会跑完并写缓存（`asyncio.shield`）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import httpx

from ...config import get_settings
from ...providers import UA, UpstreamError, build_envelope, normalize_payload, upstream_headers
from ..base import BasePlugin
from ..observe import HttpStatusError, classify_failure
from ..registry import register
from ..types import FailureClass, Link, SearchResult

log = logging.getLogger("pantrace.search.plugins.haisou")

# 单页条数默认 50：上游 max_page_size 默认也是 50，再大上游会截断。
DEFAULT_PAGE_SIZE = 50

# ---- 以下三个常量与 pansou `haisou.go:23-29` 同值，改前先对照组 ----
# pansou 的 SupportedCloudTypes。这里是"这个源支持哪些盘"的清单，
# **不再是默认抓取集合** —— 一次全平台 = 10 个计费请求，默认只查
# `settings.default_platform` 一个盘，其余等用户自己选。
SUPPORTED_PLATFORMS = ["ali", "baidu", "quark", "xunlei", "tianyi"]
# 每种平台抓几页（pansou 的 DefaultPagesPerType）。单平台时 = 2 个计费请求。
DEFAULT_PAGES_PER_PLATFORM = 2
# 每种平台的页数上限（pansou 的 MaxAllowedPagesPerType）
MAX_PAGES_PER_PLATFORM = 3

# pansou 是"任务有多少就发多少"，Go 的 http.Client 默认 MaxIdleConns=100 兜着。
# 这条上游有 QPS 限制：实测同时发 10 个会混进 `code 1003 server_too_frequency`
# （10 个里 2 个被打回，直接少两个平台的一页数据）。
# 默认单平台只发 2 个请求，碰不到这个上限；它是给"用户一次勾多个盘"兜底的。
MAX_CONCURRENCY = 5

# 重试次数（含首次）。官方按请求计费，重试就是双重计费，所以只给 2 次机会，
# 且只对"超时 / 网络 / 5xx / 429 / 1003 限流"这类可恢复错误重试。
MAX_RETRIES = 2
# 退避基数。限流（1003）实测要等 1 秒以上才恢复，0.3 秒会立刻再撞一次。
RETRY_BACKOFF = 0.8

# 上游用 200 + code 表达"太快了"。语义等同 429，必须当限流对待，
# 否则会被归到 OTHER（不可重试），把"自己打太快"记成"源故障"。
CODE_TOO_FREQUENT = 1003

# 上游 platform → pansou 的 Link.type。只有 ali 不一致（上游叫 ali，内部叫 aliyun）。
_LINK_TYPE = {
    "ali": "aliyun",
    "baidu": "baidu",
    "quark": "quark",
    "uc": "uc",
    "xunlei": "xunlei",
    "123": "123",
    "115": "115",
    "tianyi": "tianyi",
    "yidong": "mobile",
    "wo": "mobile",
    "weiyun": "weiyun",
    "ctfile": "ctfile",
    "lanzou": "lanzou",
    "pikpak": "pikpak",
    "guangya": "guangya",
    "magnet": "magnet",
}


def link_type(platform: str) -> str:
    """上游平台标识 → pansou 的 Link.type。不认识的保留原值，不丢信息。"""
    platform = (platform or "").lower()
    return _LINK_TYPE.get(platform, platform)


def format_size(size: int) -> str:
    """与 pansou `haisou.go:533 formatSize` 同口径，两边 content 长得一样。"""
    for unit, div in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= div:
            return f"{size / div:.2f} {unit}"
    return f"{size} B"


def _as_int(raw, default: int, minimum: int, maximum: int) -> int:
    """ext 里的数字参数。bool 是 int 的子类，必须先排掉（`{"page": true}` 不该变成 1）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    return max(minimum, min(int(raw), maximum))


def _as_platforms(raw) -> list[str]:
    """ext 里的平台筛选。接受 list 或逗号串，非法值直接丢掉（上游收到错 key 会整单失败）。"""
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(s).strip() for s in raw]
    else:
        return []
    return [s for s in items if s]


def _upstream_code(data) -> int:
    """取上游 body 里的业务码。返回 -1 表示没有/认不出（交给后面的结构校验兜底）。"""
    if not isinstance(data, dict):
        return -1
    try:
        return int(float(data.get("code")))
    except (TypeError, ValueError):
        return -1


@register
class HaisouPlugin(BasePlugin):
    name = "haisou"
    # pansou 的 NewBaseAsyncPlugin("haisou", 3) 是等级 3。升到 1 是因为这是
    # 唯一一条有 apikey、有分页、一次请求给全链接的通道，排序分 +1000，
    # 有它的时候应该压过匿名爬来的同名资源。
    level = 1

    # 一次真实请求实测 1~7 秒；**10 个并发任务整批实测 6.8 秒**（2026-11-18 线上量）。
    # 沿用单请求时代的 6 秒会把整批挤成 partial：用户拿到空结果，后台还在跑。
    # **必须是类属性**：`search()` 里的 `wait_for(timeout=self.response_timeout)`
    # 在 `fetch()` 开始执行前就求值了，写在 fetch 里改无效。
    response_timeout = 10.0

    async def fetch(self, client: httpx.AsyncClient, keyword: str, ext: dict) -> list[SearchResult]:
        settings = get_settings()
        pages_per_platform = _as_int(
            ext.get("pages_per_type"), DEFAULT_PAGES_PER_PLATFORM, 1, MAX_PAGES_PER_PLATFORM
        )
        page_size = _as_int(
            ext.get("page_size"), DEFAULT_PAGE_SIZE, 1, max(1, settings.max_page_size)
        )
        # **空平台只兜一个默认盘，绝不展开成 SUPPORTED_PLATFORMS 全量。**
        # 一个平台 = 1 页 1 个计费请求，5 个平台 = 5 个，差 5 倍。
        # 前端已经强制"先选网盘再搜"，这里的兜底只服务于直调插件的场景
        # （探针、以后的定时任务），不能让它们悄悄打出 10 个请求。
        platforms = _as_platforms(ext.get("platforms")) or [settings.default_platform]
        scope = ext.get("scope") if ext.get("scope") in ("title", "all") else "title"
        now = time.time()

        # 任务矩阵 = 平台 × 页码，照抄 `haisou.go:142` 的双层循环。全部并发：
        # 上游对同一 query 的 10 个并发请求实测全部 200，没有它以前那套"连续请求
        # 必超时"的问题（那是网页匿名接口的脾气，不是这条 API 的）。
        jobs = [
            (platform, page_no)
            for platform in platforms
            for page_no in range(1, pages_per_platform + 1)
        ]
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def run(platform: str, page_no: int) -> list[SearchResult]:
            async with sem:
                return await self._search_page(
                    client, keyword, page_no, page_size, [platform], scope, now
                )

        # return_exceptions=True：单平台失败不拖垮整批（pansou 的 errorTasks++）。
        outcomes = await asyncio.gather(
            *(run(p, n) for p, n in jobs), return_exceptions=True
        )

        results: list[SearchResult] = []
        failed: list[BaseException] = []
        by_platform: dict[str, int] = {}

        for (platform, page_no), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                failed.append(outcome)
                continue
            results.extend(outcome)
            by_platform[platform] = by_platform.get(platform, 0) + len(outcome)

        # pansou `haisou.go:197`：successTasks == 0 才报错。部分失败只告警，
        # 因为"3 个平台成功 2 个失败"仍是一批可用结果，不该整单报错。
        if len(failed) == len(jobs):
            # 抛**原始异常**，不能包 RuntimeError：包装会让 429 / 超时 / 网络
            # 全退化成 OTHER，`/api/health` 里就看不出死因。
            raise failed[0]
        if failed:
            log.warning(
                "[haisou] %d/%d 个平台页失败，本次结果不完整：%s",
                len(failed), len(jobs), "; ".join(str(e) for e in failed[:3]),
            )
        log.info(
            "[haisou] %s 共 %d 任务（%d 平台 × %d 页）→ %d 条，按平台 %s",
            keyword, len(jobs), len(platforms), pages_per_platform, len(results), by_platform,
        )
        return results

    # ---------- 取一页 ----------

    async def _search_page(
        self,
        client: httpx.AsyncClient,
        keyword: str,
        page: int,
        page_size: int,
        platforms: list[str],
        scope: str,
        now: float,
    ) -> list[SearchResult]:
        envelope = build_envelope(keyword, platforms, page, page_size, scope)
        data = await self._post(client, envelope, where=f"{keyword} 第 {page} 页")

        # 先看业务码，再看结构。顺序不能反：1004/1005 这类响应**根本没有 result**，
        # 先判结构会把"签名过期"报成"上游改版"，把人往错的方向带。
        if data.get("code") not in (0, "0"):
            try:
                normalize_payload(data, page, page_size)   # 必定抛 UpstreamError
            except UpstreamError as exc:
                # 1004 签名过期 / 1005 apikey 无效：不可恢复，也不该重试。
                # 转成 403 是为了让 classify_failure 归到 FORBIDDEN 而不是 OTHER ——
                # 这两件事在 /api/health 里必须一眼看得出来。
                if exc.code in (1004, 1005):
                    raise HttpStatusError(403, self.name, str(exc)) from exc
                raise
            raise ValueError(f"上游返回未知 code：{data.get('code')!r}")

        # 再判结构。`_normalize()` 用 `.get("items") or []` 兜底，
        # 会把"items 变成字符串"这种改版静默吞成零结果。
        result = data.get("result")
        if not isinstance(result, dict):
            raise ValueError(f"返回结构变了，result 不是对象：{list(data)[:6]}")
        raw_items = result.get("items")
        if raw_items is None:
            # 有的实现空结果直接不返回 items，只有 pagination.total = 0。
            # 这不是结构变化，按零结果处理。
            total = (result.get("pagination") or {}).get("total") or 0
            if int(total or 0) > 0:
                raise ValueError("返回结构变了，有 total 但没有 result.items")
        elif not isinstance(raw_items, list):
            raise ValueError(f"返回结构变了，result.items 不是数组：{type(raw_items).__name__}")

        payload = normalize_payload(data, page, page_size)

        items = payload["items"]
        mapped: list[SearchResult] = []
        for raw in items:
            result_item = self._to_result(raw, now)
            if result_item is not None:
                mapped.append(result_item)

        if items and not mapped:
            # 有 N 条却一条链接都没有 = 上游字段改了名（shareUrl → url 之类）。
            # 返回空列表会被记成 zero_yield，把"解析失效"误判成"这个源没内容"。
            raise ValueError(f"返回 {len(items)} 条但全部缺少 shareUrl，字段可能已改名")
        return mapped

    def _to_result(self, raw: dict, now: float) -> SearchResult | None:
        """把上游一条归一化记录转成 pansou 的 SearchResult。

        上游已经给全了字段，这里**不做任何链接清洗**：pansou 的 clean.py 是给
        HTML 里扒出来的脏文本用的，对结构化数据套上去只会把合法链接改坏。
        """
        url = raw.get("shareUrl") or ""
        if not url:
            return None

        platform = (raw.get("platform") or "").lower()
        share_pwd = raw.get("sharePwd") or ""
        file_count = int(raw.get("fileCount") or 0)
        size_bytes = int(raw.get("sizeBytes") or 0)

        stable = raw.get("hsid") or raw.get("shareCode") or hashlib.md5(url.encode()).hexdigest()[:12]
        title = raw.get("title") or raw.get("shareCode") or "未知资源"

        return SearchResult(
            unique_id=f"{self.name}-{stable}",
            title=title,
            content=(
                f"文件数量: {file_count} | 网盘类型: {raw.get('platformName') or platform} "
                f"| 大小: {format_size(size_bytes)}"
            ),
            links=[Link(type=link_type(platform), url=url, password=share_pwd)],
            tags=[platform] if platform else [],
            # 插件结果必须留空，pansou 同约定（channel 只给 TG 用）
            channel="",
            # 上游不返回发布时间，用抓取时刻。排序里时间分只影响同等级内的先后。
            datetime=now,
            extras={
                "hsid": raw.get("hsid") or "",
                "platform": platform,
                "platformName": raw.get("platformName") or "",
                "shareCode": raw.get("shareCode") or "",
                "shareUrl": url,
                "sharePwd": share_pwd,
                "fileCount": file_count,
                "sizeBytes": size_bytes,
            },
        )

    # ---------- HTTP ----------

    async def _post(self, client: httpx.AsyncClient, envelope: dict, where: str = "") -> dict:
        settings = get_settings()
        headers = dict(upstream_headers())
        headers["User-Agent"] = UA

        last: BaseException | None = None
        for attempt in range(MAX_RETRIES):
            if attempt:
                await asyncio.sleep(RETRY_BACKOFF * attempt)

            try:
                resp = await client.post(
                    settings.idr_url,
                    json=envelope,
                    headers=headers,
                    # 官方要求 > 60 秒，否则提前中断照样计费。
                    # 共享客户端默认 30 秒，这里必须显式覆盖。
                    timeout=settings.request_timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failure = classify_failure(exc) or FailureClass.OTHER
                if not failure.retryable:
                    raise
                last = exc
                continue

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    # 非 JSON 不重试：上游返回了 HTML 错误页，重试只会再来一遍
                    raise ValueError(f"上游返回非 JSON（{where or 'haisou'}）") from exc

                # 1003 = i18n:server_too_frequency，上游拿 200 + 业务码表达"打太快了"。
                # 语义等同 429，必须重试 —— 归到 OTHER（不可重试）会把"自己并发太高"
                # 记成"源故障"，`/api/health` 里就分不出来了。
                if _upstream_code(data) == CODE_TOO_FREQUENT:
                    last = HttpStatusError(429, self.name, "上游频率限制（1003）")
                    log.warning("[haisou] %s 触发限流 1003，退避 %.1fs 后重试", where or "请求", RETRY_BACKOFF * (attempt + 1))
                    continue
                return data

            err = HttpStatusError(resp.status_code, self.name, resp.text[:200])
            if not classify_failure(err).retryable:
                raise err
            last = err

        # 重试耗尽抛原始异常，不包 RuntimeError：包装会丢掉 429/5xx 的分类能力。
        log.warning("[haisou] %s 重试 %d 次后仍失败: %s", where or "请求", MAX_RETRIES, last)
        assert last is not None
        raise last
