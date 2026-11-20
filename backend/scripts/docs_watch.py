"""腾讯文档收集表 → 资源库，带增量监控。

用法
----
    # 登记一份文档（登记后会记住读到第几行）
    .venv/Scripts/python.exe scripts/docs_watch.py --add "https://docs.qq.com/sheet/XXX?tab=BB08J2"

    # 登记整个文档的所有工作表
    .venv/Scripts/python.exe scripts/docs_watch.py --add "https://docs.qq.com/sheet/XXX" --all-tabs

    # 看登记了什么、上次跑到哪、有没有出错
    .venv/Scripts/python.exe scripts/docs_watch.py --list

    # 跑一轮增量（cron 每天调这个）
    .venv/Scripts/python.exe scripts/docs_watch.py --run

    # 只导一次，不登记（临时表用）
    .venv/Scripts/python.exe scripts/docs_watch.py --once "https://docs.qq.com/sheet/XXX" --dry-run

每天自动跑（服务器上配 cron）
------------------------------
    # crontab -e
    20 4 * * * cd /srv/pantrace/backend && .venv/bin/python scripts/docs_watch.py --run >> /var/log/pantrace-docs.log 2>&1

不想配 cron 就常驻：
    .venv/Scripts/python.exe scripts/docs_watch.py --run --loop 24

判读报告
--------
`新增 0 / 更新 2570` 说明表没变，只是又扫了一遍重叠区；
`新增 312 / 更新 2570` 说明表里加了 312 条新的。
更新数远大于新增数是正常的：每次会回看一段重叠区，靠去重键合并，
作用是把"表中间被插了一行"这种情况也捞回来。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import docs_qq, intake, store  # noqa: E402
from app.config import get_settings  # noqa: E402

# 每次往回多看这么多行。表格中间插入新行时，纯按 last_row 往前读会漏，
# 重叠一段靠 dedup_key 合并就能补上；512 行的代价约两次请求，很值。
LOOKBACK = 512
# 一批落库多少行。38 万行一次性 ingest 会把内存顶到几百兆，
# 而且中途失败就全丢，分批的话前面已入库的不受影响。
BATCH = 5000


def _open_library() -> None:
    store.init(Path(get_settings().library_path))


def _ingest_rows(rows: list[list[str]], source_name: str, *, dry_run: bool) -> dict:
    """二维表 → 库。分批走，返回累计的 {added, touched}。"""
    added = touched = 0
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        result = intake.parse_rows(chunk, source_name=source_name)
        if not result.items:
            continue
        if dry_run:
            added += len(result.items)
            continue
        got = store.ingest(result.items)
        added += got["added"]
        touched += got["touched"]
    return {"added": added, "touched": touched}


def _sync_one(src: dict, *, dry_run: bool, limit: int | None, workers: int) -> dict:
    """跑一份订阅的增量。返回一行报告用数据。"""
    label = f"{src['title'] or src['tab_id'] or '默认表'}"

    def progress(offset: int, total: int) -> None:
        # 小表刷进度只是噪声，大表（几万行起步）不刷的话像卡死了
        if total > 20000:
            print(f"    {label} {offset}/{total}", end="\r", flush=True)

    start = max(0, int(src["last_row"]) - LOOKBACK) if src["last_row"] else 0
    end_row = None if limit is None else start + limit
    rows, meta = docs_qq.read_tab(
        src["url"],
        tab_id=src["tab_id"],
        start_row=start,
        end_row=end_row,
        workers=workers,
        on_progress=progress,
    )
    tab = meta.tab(src["tab_id"])
    if tab and tab.max_row > 20000:
        print()  # 进度行是 \r 覆盖的，收尾补个换行

    source_name = f"doc:{(tab.title or meta.title or label)[:40]}"
    got = _ingest_rows(rows, source_name, dry_run=dry_run)

    # 进度只写到表长为止。`--limit` 是调试参数，不该把进度顶到表外面去
    ceiling = tab.max_row if tab else src["last_row"]
    new_row = min(end_row, ceiling) if end_row is not None else ceiling
    if not dry_run:
        store.save_doc_progress(
            src["id"], last_row=new_row, last_rev=meta.rev
        )
    return {
        "tab": label,
        "rows": len(rows),
        "max_row": tab.max_row if tab else 0,
        "last_row": new_row,
        **got,
    }


def cmd_add(args) -> int:
    pad_id, tab_id = docs_qq.pad_id_of(args.add)
    meta = docs_qq.fetch_meta(args.add)
    print(f"文档：{meta.title}（{len(meta.tabs)} 个工作表）")

    targets = meta.tabs if (args.all_tabs or not tab_id) else [meta.tab(tab_id)]
    if not targets:
        print("没有可登记的工作表。")
        return 1

    _open_library()
    for tab in targets:
        if tab is None:
            continue
        got = store.add_doc_source(
            pad_id=pad_id,
            tab_id=tab.tab_id,
            title=tab.title,
            url=args.add.split("?")[0],
        )
        flag = "新增" if got["created"] else "已存在"
        print(f"  [{flag}] {tab.title or tab.tab_id}  {tab.max_row} 行  起点 {got['last_row']}")
    print("\n登记完成。跑 --run 开始第一次抓取。")
    return 0


def cmd_list(args) -> int:
    _open_library()
    items = store.list_doc_sources()
    if not items:
        print("还没有登记任何文档。用 --add <链接> 加一个。")
        return 0
    now = int(time.time())
    print(f"{'ID':>3}  {'工作表':<14} {'进度':>9}  {'上次跑':<12} 状态")
    for src in items:
        ran = src["last_run_at"]
        ago = f"{int((now - ran) / 3600)}小时前" if ran else "从未"
        state = "正常" if src["enabled"] else "已停用"
        if src["last_error"]:
            state = f"失败：{src['last_error'][:40]}"
        print(
            f"{src['id']:>3}  {(src['title'] or src['tab_id'] or '默认'):<14}"
            f" {src['last_row']:>9}  {ago:<12} {state}"
        )
    return 0


def cmd_drop(args) -> int:
    _open_library()
    ok = store.drop_doc_source(args.drop)
    print("已删除" if ok else f"没有 id={args.drop} 的订阅")
    return 0 if ok else 1


def cmd_once(args) -> int:
    """不登记，直接导一次。临时表、别人发来的表用这个。"""
    pad_id, tab_id = docs_qq.pad_id_of(args.once)
    meta = docs_qq.fetch_meta(args.once)
    tabs = meta.tabs if (args.all_tabs or not tab_id) else [meta.tab(tab_id)]
    tabs = [t for t in tabs if t]
    if not tabs:
        print("没有可读的工作表。")
        return 1

    if not args.dry_run:
        _open_library()
    total = {"added": 0, "touched": 0}
    for tab in tabs:
        end = None if args.limit is None else args.limit
        rows, _ = docs_qq.read_tab(
            args.once, tab_id=tab.tab_id, end_row=end, workers=args.workers
        )
        got = _ingest_rows(rows, f"doc:{(tab.title or meta.title)[:40]}", dry_run=args.dry_run)
        print(f"  {tab.title or tab.tab_id}: {len(rows)} 行 → 新增 {got['added']}，更新 {got['touched']}")
        for k in total:
            total[k] += got[k]
    if args.dry_run:
        print(f"\n（干跑，未写库）可入库 {total['added']} 条")
    else:
        print(f"\n合计：新增 {total['added']}，更新 {total['touched']}")
    return 0


def cmd_run(args) -> int:
    _open_library()
    sources = store.list_doc_sources(enabled_only=True)
    if not sources:
        print("没有启用的订阅。先 --add 一个文档。")
        return 0

    failures = 0
    while True:
        print(f"\n=== {time.strftime('%Y-%m-%d %H:%M')} 增量抓取 {len(sources)} 份 ===")
        for src in sources:
            print(f"  [{src['id']}] {src['title'] or src['tab_id']}")
            try:
                rep = _sync_one(src, dry_run=args.dry_run, limit=args.limit, workers=args.workers)
            except docs_qq.DocError as exc:
                # 失败要落库，不然 --list 看不出"这份表连续几天没抓成功"
                if not args.dry_run:
                    store.save_doc_progress(src["id"], error=str(exc)[:200])
                print(f"    失败：{exc}")
                failures += 1
                continue
            except Exception as exc:  # noqa: BLE001
                # 任何意外都不能让整轮停在第一份表上
                if not args.dry_run:
                    store.save_doc_progress(src["id"], error=f"{exc.__class__.__name__}: {exc}"[:200])
                print(f"    异常：{exc.__class__.__name__}: {exc}")
                failures += 1
                continue
            print(
                f"    读到 {rep['rows']} 行（表长 {rep['max_row']}）"
                f" → 新增 {rep['added']}，更新 {rep['touched']}，进度 {rep['last_row']}"
            )

        if not args.loop:
            break
        hours = float(args.loop)
        print(f"\n{hours} 小时后再跑。Ctrl+C 退出。")
        try:
            time.sleep(hours * 3600)
        except KeyboardInterrupt:
            break
        sources = store.list_doc_sources(enabled_only=True)

    return 1 if failures and failures == len(sources) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="腾讯文档收集表 → 资源库（带增量）")
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument("--add", metavar="URL", help="登记文档，之后每天增量抓")
    act.add_argument("--list", action="store_true", help="看订阅和进度")
    act.add_argument("--drop", type=int, metavar="ID", help="删除订阅（不动已入库的资源）")
    act.add_argument("--run", action="store_true", help="跑一轮增量")
    act.add_argument("--once", metavar="URL", help="只导一次，不登记")

    ap.add_argument("--tab", default="", help="指定工作表 id（--add / --once 用）")
    ap.add_argument("--all-tabs", action="store_true", help="所有工作表都处理")
    ap.add_argument("--dry-run", action="store_true", help="只解析，不写库、不更新进度")
    ap.add_argument("--limit", type=int, default=None, help="最多读 N 行（调试用）")
    ap.add_argument("--workers", type=int, default=6, help="并发块数，默认 6")
    ap.add_argument("--loop", metavar="小时", default=None, help="常驻模式，每 N 小时跑一轮")
    args = ap.parse_args()

    if args.add:
        return cmd_add(args)
    if args.list:
        return cmd_list(args)
    if args.drop is not None:
        return cmd_drop(args)
    if args.once:
        return cmd_once(args)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
