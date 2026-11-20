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
from app.search import clean  # noqa: E402
from app.config import get_settings  # noqa: E402

# 每次往回多看这么多行。表格中间插入新行时，纯按 last_row 往前读会漏，
# 重叠一段靠 dedup_key 合并就能补上；512 行的代价约两次请求，很值。
LOOKBACK = 512
# 一批落库多少行。38 万行一次性 ingest 会把内存顶到几百兆，
# 而且中途失败就全丢，分批的话前面已入库的不受影响。
BATCH = 5000


def _open_library() -> None:
    store.init(Path(get_settings().library_path))


def _cols_of(src: dict) -> dict:
    """订阅上的列配置 → parse_rows 的参数。全 0 时等于没配置。"""
    return {
        "title_col": int(src.get("title_col") or 0),
        "link_col": int(src.get("link_col") or 0),
        "code_col": int(src.get("code_col") or 0),
    }


def _report_skips(
    skipped: list[tuple[int, str]], *, cols: dict | None = None, limit: int = 3
) -> None:
    """跳过原因按类聚合。

    一次导 38 万行跳出几万条是正常的（表里本来就有详情页链接），全打出来
    没法看；但一条都不打就是"静默丢数据"——123 和天翼当初被埋掉就是这么来的。
    折中：给每类总数，再给前几条原文，够定位。
    """
    if not skipped:
        return
    counts: dict[str, int] = {}
    for _, why in skipped:
        key = why.split("：")[0].split("（")[0]
        counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    print(f"    跳过 {len(skipped)} 条：" + "，".join(f"{k} {v}" for k, v in top))
    for line_no, why in skipped[:limit]:
        print(f"      第 {line_no} 行 {why}")

    # 指定了链接列，却大面积在那一列找不到链接 —— 作者插了列，列号错位了。
    # 这种错会安静地跑好几天（每次都"读到 N 行、新增 0"），必须喊出来。
    link_col = (cols or {}).get("link_col") or 0
    if link_col:
        miss = counts.get(f"第 {link_col} 列没有网盘链接", 0)
        if miss >= max(20, len(skipped) // 2):
            print(
                f"    ⚠ 有 {miss} 行在「第 {link_col} 列」找不到链接，列号可能变了："
                f"重跑 --add 看列探测，再用 --set-cols 改"
            )


def _ingest_rows(
    rows: list[list[str]], source_name: str, *, dry_run: bool, cols: dict | None = None
) -> dict:
    """二维表 → 库。分批走，返回累计的 {added, touched, skipped}。"""
    added = touched = 0
    skipped: list[tuple[int, str]] = []
    opts = dict(cols or {})
    # 表头只让第一批猜。每批各自猜的话，某批首行标题叫《资源链接大全》
    # 就会被当表头吃掉，38 万行切成 76 批能吃几十条。
    offset = 0
    for first, chunk in intake.iter_row_batches(rows, BATCH):
        result = intake.parse_rows(
            chunk,
            has_header=None if first else False,
            source_name=source_name,
            **opts,
        )
        # 批内行号是相对的，报告里要能对上原表，否则"第 3 行有问题"根本没法找
        skipped.extend((n + offset, why) for n, why in result.skipped)
        offset += len(chunk)
        if not result.items:
            continue
        if dry_run:
            added += len(result.items)
            continue
        got = store.ingest(result.items)
        added += got["added"]
        touched += got["touched"]
    return {"added": added, "touched": touched, "skipped": skipped}


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
    got = _ingest_rows(rows, source_name, dry_run=dry_run, cols=_cols_of(src))
    _report_skips(got["skipped"], cols=_cols_of(src))

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


_KIND_LABEL = {
    "title": "标题",
    "link": "链接",
    "code": "提取码",
    "mixed": "链接混在别的内容里",
    "other": "其他",
}


def _probe_tab(url: str, tab_id: str, sample_rows: int = 40) -> list[dict]:
    """读前几十行，猜每列是干什么的。给人核对列号用，不写库。"""
    rows, _ = docs_qq.read_tab(url, tab_id=tab_id, end_row=sample_rows)
    return intake.probe_columns(rows)


def _print_probe(report: list[dict], cols: dict | None = None) -> None:
    """打印列探测结果。带样本值——人靠样本核对，不靠列号数字。"""
    if not report:
        print("    （前几十行是空的，没法探测列）")
        return
    guess = {item["kind"]: item["col"] for item in report if item["kind"] in _KIND_LABEL}
    picked = cols or {}
    print("    列探测（1 起算，跟表格里看到的列号一致）：")
    for item in report:
        mark = guess.get(item["kind"]) == item["col"]
        flag = "←" if mark else " "
        sample = " / ".join(s[:28] for s in item["samples"][:2])
        print(f"      {flag} 第 {item['col']} 列  {_KIND_LABEL[item['kind']]:<16} {sample}")
    if picked.get("link_col"):
        print(f"    已按人工指定：标题第 {picked.get('title_col') or '-'} 列，"
              f"链接第 {picked['link_col']} 列，"
              f"码第 {picked.get('code_col') or '-'} 列")
    else:
        auto = [f"{_KIND_LABEL[k]}第 {v} 列" for k, v in guess.items() if k in ("title", "link", "code")]
        print("    自动识别：" + ("，".join(auto) if auto else "认不出来"))
        print("    不对就加 --title-col N --link-col N 重登记一次。")


def _cols_from_args(args) -> dict:
    return {
        "title_col": args.title_col or 0,
        "link_col": args.link_col or 0,
        "code_col": args.code_col or 0,
    }


def _check_cols(url: str, tab_id: str, cols: dict) -> str:
    """人工指定的列号是不是指对了地方。返回警告文案，空串表示没问题。

    取数在这里，判断在 `intake.check_link_column`（那是纯逻辑，可以离线测）。
    """
    link_col = cols.get("link_col") or 0
    if not link_col:
        return ""
    rows, _ = docs_qq.read_tab(url, tab_id=tab_id, end_row=40)
    return intake.check_link_column(rows, link_col)


def cmd_add(args) -> int:
    pad_id, tab_id = docs_qq.pad_id_of(args.add)
    meta = docs_qq.fetch_meta(args.add)
    print(f"文档：{meta.title}（{len(meta.tabs)} 个工作表）")

    targets = meta.tabs if (args.all_tabs or not tab_id) else [meta.tab(tab_id)]
    if not targets:
        print("没有可登记的工作表。")
        return 1

    cols = _cols_from_args(args)
    _open_library()
    for tab in targets:
        if tab is None:
            continue
        print(f"\n  [{tab.title or tab.tab_id}] {tab.max_row} 行")
        _print_probe(_probe_tab(args.add.split("?")[0], tab.tab_id), cols)

        if cols["link_col"]:
            warn = _check_cols(args.add.split("?")[0], tab.tab_id, cols)
            if warn:
                # 只警告不拦：有可能这份表前 40 行刚好都是没链接的表头区
                print(f"    ⚠ {warn}（先不登记，确认后再跑一次）")
                continue

        got = store.add_doc_source(
            pad_id=pad_id,
            tab_id=tab.tab_id,
            title=tab.title,
            url=args.add.split("?")[0],
            **cols,
        )
        flag = "新增" if got["created"] else "已存在"
        print(f"    [{flag}] 起点 {got['last_row']}")
    print("\n登记完成。跑 --run 开始第一次抓取。")
    return 0


def cmd_set_cols(args) -> int:
    """改已有订阅的列配置。表格改结构了用这个。"""
    _open_library()
    sources = {src["id"]: src for src in store.list_doc_sources()}
    src = sources.get(args.set_cols)
    if not src:
        print(f"没有 id={args.set_cols} 的订阅。用 --list 看有哪些。")
        return 1

    cols = _cols_from_args(args)
    print(f"[{src['id']}] {src['title'] or src['tab_id']}  当前："
          f"标题 {src['title_col'] or '自动'}，链接 {src['link_col'] or '自动'}，"
          f"码 {src['code_col'] or '自动'}")
    _print_probe(_probe_tab(src["url"], src["tab_id"]), cols)

    if cols["link_col"]:
        warn = _check_cols(src["url"], src["tab_id"], cols)
        if warn:
            print(f"⚠ {warn}")
            return 1

    store.set_doc_columns(
        src["id"],
        title_col=cols["title_col"],
        link_col=cols["link_col"],
        code_col=cols["code_col"],
    )
    print("已更新。下次 --run 生效；进度不受影响，不会重拉整表。")
    return 0


def cmd_list(args) -> int:
    _open_library()
    items = store.list_doc_sources()
    if not items:
        print("还没有登记任何文档。用 --add <链接> 加一个。")
        return 0
    now = int(time.time())
    print(f"{'ID':>3}  {'工作表':<14} {'进度':>9}  {'列配置':<14} {'上次跑':<12} 状态")
    for src in items:
        ran = src["last_run_at"]
        ago = f"{int((now - ran) / 3600)}小时前" if ran else "从未"
        state = "正常" if src["enabled"] else "已停用"
        if src["last_error"]:
            state = f"失败：{src['last_error'][:40]}"
        cols = _cols_desc(src)
        print(
            f"{src['id']:>3}  {(src['title'] or src['tab_id'] or '默认'):<14}"
            f" {src['last_row']:>9}  {cols:<14} {ago:<12} {state}"
        )
    return 0


def _cols_desc(src: dict) -> str:
    """一眼看出这份表是自动识别还是人工锁了列号。"""
    parts = []
    for key, label in (("title_col", "标"), ("link_col", "链"), ("code_col", "码")):
        val = int(src.get(key) or 0)
        if val:
            parts.append(f"{label}{val}")
    return " ".join(parts) if parts else "自动"


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
        got = _ingest_rows(
            rows,
            f"doc:{(tab.title or meta.title)[:40]}",
            dry_run=args.dry_run,
            cols=_cols_from_args(args),
        )
        print(f"  {tab.title or tab.tab_id}: {len(rows)} 行 → 新增 {got['added']}，更新 {got['touched']}")
        _report_skips(got["skipped"], cols=_cols_from_args(args))
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
    act.add_argument("--set-cols", type=int, metavar="ID", help="改已有订阅的列配置")

    ap.add_argument("--tab", default="", help="指定工作表 id（--add / --once 用）")
    ap.add_argument("--all-tabs", action="store_true", help="所有工作表都处理")
    # 列号一律 1 起，跟人在表格里数的一致。不填就走自动识别。
    ap.add_argument("--title-col", type=int, default=0, metavar="N", help="标题在第几列（1 起）")
    ap.add_argument("--link-col", type=int, default=0, metavar="N", help="网盘链接在第几列（1 起）")
    ap.add_argument("--code-col", type=int, default=0, metavar="N", help="提取码在第几列（1 起）")
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
    if args.set_cols is not None:
        return cmd_set_cols(args)
    if args.once:
        return cmd_once(args)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
