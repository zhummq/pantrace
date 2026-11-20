"""TG 频道活跃度探针：加频道之前先跑一遍，别拿"页面能打开"当"频道有货"。

为什么要有这个脚本
==================
t.me/s/<channel> 返回 200 只能证明频道存在，证明不了它是资源频道：

    tgsearchers7     消息多、几乎每条带网盘链接、昨天还在发   → 值得接
    某个资讯频道      消息多、一条网盘链接都没有              → 接了也是白跑
    某个停更频道      有链接、但最新一条是半年前              → 链接基本全死
    某个被封频道      404 / 429 / 页面结构变了                → 解析层直接失效

这里的"活跃"是三层 AND，不是单一指标：
    可达（HTTP 200 + 结构能认）+ 有货（最近一屏有网盘链接）+ 新鲜（最新带链接消息距今够近）

判据全部复用线上同一套代码（`search_channel` / `parse_search_results_with_status`），
所以探针说"有货"的频道，线上搜索一定能解析出东西 —— 不会出现"探针明明说好，线上 0 条"。

用法
====
    # 在服务器上跑（开发机直连 t.me 超时，见 .env 注释）
    python scripts/probe_tg_channels.py tgsearchers7 some_channel another

    # 开发机挂代理跑
    python scripts/probe_tg_channels.py --proxy http://127.0.0.1:16796 tgsearchers7

    # 调时间阈值（默认 7 天内算新鲜）和并发
    python scripts/probe_tg_channels.py --fresh-days 14 --concurrency 8 ch1 ch2 ch3

    # 不指定频道时探测 .env 里的 TG_CHANNELS
    python scripts/probe_tg_channels.py

输出
====
    channel           status    msgs  links  newest       verdict
    tgsearchers7      ok          20     18  0.4d         ACTIVE   → 加
    news_channel      ok          20      0  0.2d         NO_LINK  → 别加，不是资源频道
    dead_channel      ok          20     12  214d         STALE    → 别加，链接基本全死
    gone_channel      404        --     --  --            DEAD     → 频道没了

    ACTIVE   = 可达 + 有货 + 新鲜（默认 ≤7 天）
    NO_LINK  = 可达但最近一屏没有网盘链接
    STALE    = 有链接但最新带链接消息超过 --fresh-days
    DEAD     = 404/403/429/超时/页面结构认不出
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import get_settings
from app.search.base import build_clients
from app.search.observe import HttpStatusError
from app.search.tg.channel import search_channel
from app.search.tg.parse import (
    PARSE_STATUS_STRUCTURE_CHANGED,
    TgParseError,
    parse_search_results_with_status,
)

# 不自己设 UA：线上 build_clients 用的是 httpx 默认 UA（见 base.py:58）。
# 探针必须和线上同构，否则"探针说有货、线上 0 条"这个承诺就不成立。


def _fmt_age(ts: float) -> str:
    if ts <= 0:
        return "--"
    days = (time.time() - ts) / 86400
    if days < 1:
        return f"{max(0, days * 24):.1f}h"
    return f"{days:.1f}d"


async def probe_one(
    channel: str,
    client: httpx.AsyncClient,
    fresh_days: float,
    request_timeout: float,
) -> dict:
    """探测单个频道。异常一律转成 verdict，不让一个坏频道拖垮整批。"""
    out = {
        "channel": channel,
        "status": "--",
        "msgs": 0,
        "links": 0,
        "newest": 0.0,
        "verdict": "DEAD",
        "note": "",
    }

    # 直接抓频道预览页（keyword 为空），拿最近一屏消息。
    # 走 parse 的带状态版本，这样"结构变化"和"没有链接"能分开判。
    url = f"https://t.me/s/{channel}"
    try:
        resp = await client.get(url)
        out["status"] = str(resp.status_code)
        if resp.status_code != 200:
            out["note"] = f"HTTP {resp.status_code}"
            return out

        results, parse_status = parse_search_results_with_status(resp.text, channel)
        if parse_status == PARSE_STATUS_STRUCTURE_CHANGED:
            out["note"] = "页面结构变化，解析失效"
            return out

        # parse 只返回"含网盘链接"的消息；消息总数另从页面数 message_wrap。
        # 用 lxml 数，和 parse.py 同一个选择器。
        from lxml import html

        doc = html.fromstring(resp.text)
        out["msgs"] = len(doc.cssselect(".tgme_widget_message_wrap"))
        out["links"] = sum(len(r.links) for r in results)
        out["newest"] = max((r.datetime for r in results), default=0.0)

        if not results:
            out["verdict"] = "NO_LINK"
            out["note"] = "最近一屏没有网盘链接"
        elif (time.time() - out["newest"]) / 86400 > fresh_days:
            out["verdict"] = "STALE"
            out["note"] = f"最新带链接消息 {_fmt_age(out['newest'])} 前"
        else:
            out["verdict"] = "ACTIVE"
        return out

    except httpx.TimeoutException:
        out["status"] = "timeout"
        out["note"] = "超时（开发机直连 t.me 常见，挂代理或在服务器跑）"
    except HttpStatusError as exc:
        out["status"] = str(exc.status_code)
        out["note"] = str(exc)
    except TgParseError as exc:
        out["note"] = f"解析失败: {exc}"
    except Exception as exc:  # 单频道失败不影响整批
        out["note"] = f"{type(exc).__name__}: {exc}"
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="探测 TG 频道是否值得接入（活跃度三层判据）")
    ap.add_argument("channels", nargs="*", help="频道名，@ 可省略；不填则用 .env 的 TG_CHANNELS")
    ap.add_argument("--proxy", default="", help="代理地址，开发机跑必须给")
    ap.add_argument("--fresh-days", type=float, default=7.0, help="最新带链接消息距今多少天内算新鲜")
    ap.add_argument("--concurrency", type=int, default=6, help="并发探测数，别打太猛")
    ap.add_argument("--timeout", type=float, default=8.0, help="单频道请求超时秒数")
    args = ap.parse_args()

    settings = get_settings()
    channels = [c.strip().lstrip("@") for c in args.channels if c.strip()]
    if not channels:
        channels = settings.tg_channel_list
    if not channels:
        print("没有频道可探测：传频道名，或配置 TG_CHANNELS")
        return 2

    proxy = args.proxy or (settings.proxy_url if settings.use_env_proxy else "")
    # 复用线上同一个 client 构造函数，只把超时换成探针可调的。
    client, _ = build_clients(timeout_fast=args.timeout, proxy=proxy or None)
    async with client:
        sem = asyncio.Semaphore(max(1, args.concurrency))

        async def guarded(ch: str) -> dict:
            async with sem:
                return await probe_one(ch, client, args.fresh_days, args.timeout)

        results = await asyncio.gather(*(guarded(c) for c in channels))

    hdr = f"{'channel':<20} {'status':<8} {'msgs':>5} {'links':>6} {'newest':>8}  verdict  note"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(
            f"{r['channel']:<20} {r['status']:<8} {r['msgs']:>5} {r['links']:>6} "
            f"{_fmt_age(r['newest']):>8}  {r['verdict']:<8} {r['note']}"
        )

    active = [r["channel"] for r in results if r["verdict"] == "ACTIVE"]
    print()
    print(f"ACTIVE {len(active)}/{len(results)}：{', '.join(active) if active else '（无）'}")
    print("只把 ACTIVE 的频道加进后台；NO_LINK / STALE / DEAD 加了也是白跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
