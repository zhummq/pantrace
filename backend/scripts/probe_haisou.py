"""haisou 通道探针：验证官方 API 链路 + 看清一次搜索到底花了什么。

为什么还留这个脚本
==================
插件改走官方 API 之后，"通不通"和"贵不贵"是两件事，而且失败原因常常是
**HTTP 200 + code 1005/1004**（apikey 无效、签名过期），这两种在浏览器里
看起来都是"正常返回"。脚本把 code / 耗时 / 条数直接打出来，一眼能分清：

    http=200 code=0  条数>0      → 链路正常
    http=200 code=1005          → apikey 无效或余额不足（和改造无关，旧链路同样会报）
    http=200 code=1004          → 签名过期，去浏览器重抓 x-idr-sign
    http=403/429/5xx            → 网络层或限流，见 failure 分类

用法
====
    # 最小验证：默认词"三体"，抓 1 页
    python scripts/probe_haisou.py

    # 指定词、页数、平台
    python scripts/probe_haisou.py -k 庆余年 --pages 2 --platforms quark,baidu

    # 走完整编排（多源合并 + 本地分页），验 /api/search 的出口形状
    python scripts/probe_haisou.py --engine

结果判读
========
    final=False                 → 6 秒窗口内没跑完，后台还在抓（不是失败）
    failure=forbidden 且 code 1005 → 凭据问题，换 apikey / 充值
    failure=structure_changed   → 上游字段改名，去看 _search_page 的结构检查
    条数 0 且无 failure          → 真零结果，换词再试，不能判源失效
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.search import registry
from app.search.cache import SearchCache
from app.search.pool import WorkerPool


async def run_plugin(keyword: str, args: argparse.Namespace) -> None:
    plugins = registry.load_all(["haisou"], cache=SearchCache(), pool=WorkerPool())
    if not plugins:
        print("插件没加载起来：检查 app/search/plugins/haisou.py 是否注册成功")
        return

    plugin = plugins[0]
    ext = {"pages_per_type": args.pages, "page_size": args.page_size}
    if args.platforms:
        # 不传 = 插件按 SUPPORTED_PLATFORMS（5 个盘）各查一遍
        ext["platforms"] = [p.strip() for p in args.platforms.split(",") if p.strip()]
    if args.scope:
        ext["scope"] = args.scope

    started = time.time()
    # 直接调 fetch 而不是 search()：search() 会写缓存，第二次跑就测不到真实链路了。
    client = plugin.client_long
    try:
        results = await plugin.fetch(client, keyword, ext)
        elapsed = time.time() - started
        by_platform: dict[str, int] = {}
        for r in results:
            key = r.extras.get("platform", "?")
            by_platform[key] = by_platform.get(key, 0) + 1
        print(f"\n=== 插件（官方 API）{elapsed:.2f}s  条数={len(results)}  按平台 {by_platform}")
        for r in results[: args.show]:
            link = r.links[0] if r.links else None
            print(
                f"- {r.title[:36]:<36} | {link.type if link else '-':<7} | "
                f"{(link.url if link else '')[:58]:<58} | "
                f"pwd={link.password if link else ''!r} | "
                f"files={r.extras.get('fileCount')} | size={r.extras.get('sizeBytes')}"
            )
    except Exception as exc:  # noqa: BLE001  探针就是要有话直说
        elapsed = time.time() - started
        from app.search.observe import classify_failure

        print(f"\n=== 插件失败 {elapsed:.2f}s  {type(exc).__name__}: {exc}")
        print(f"    failureClass = {classify_failure(exc)}")
        if getattr(exc, "code", None):
            print(f"    上游 code   = {exc.code}")
        if getattr(exc, "status", None):
            print(f"    HTTP status = {exc.status}")
    finally:
        await registry.close_clients()


def run_engine(keyword: str, args: argparse.Namespace) -> None:
    """走一遍完整编排（多源 → 合并 → 排序 → 本地分页），验证出口形状。

    和 /api/search 的差别只有鉴权、限流、响应出口装饰；取数这一段完全一致。
    """
    from app.search import engine

    platforms = [p.strip() for p in (args.platforms or "").split(",") if p.strip()]
    started = time.time()
    try:
        data = asyncio.run(
            engine.search(keyword, platforms, args.page, args.page_size, args.scope or "title")
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n=== engine 失败 {time.time() - started:.2f}s  {type(exc).__name__}: {exc}")
        return

    pg = data["pagination"]
    print(
        f"\n=== engine {time.time() - started:.2f}s  本页 {len(data['items'])} 条 / "
        f"total={pg['total']}  {pg['page']}/{pg['totalPages']} 页  "
        f"源={data['_meta']['sources']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="haisou 官方 API 通道探针")
    parser.add_argument("-k", "--keyword", default="三体")
    parser.add_argument("--page", type=int, default=1, help="本地分页页码（配 --engine 用）")
    parser.add_argument(
        "--pages", type=int, default=1,
        help="每种网盘各抓几页（默认 1 省额度；插件默认 2，与 pansou 同值）",
    )
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument(
        "--platforms", default="",
        help="逗号分隔；留空 = 按 SUPPORTED_PLATFORMS 走 5 个盘（ali,baidu,quark,xunlei,tianyi）",
    )
    parser.add_argument("--scope", default="", help="title / all")
    parser.add_argument("--show", type=int, default=5, help="打印前几条")
    parser.add_argument("--engine", action="store_true", help="改走完整编排，验证 items/pagination 出口")
    args = parser.parse_args()

    if args.engine:
        run_engine(args.keyword, args)
    else:
        asyncio.run(run_plugin(args.keyword, args))


if __name__ == "__main__":
    main()
