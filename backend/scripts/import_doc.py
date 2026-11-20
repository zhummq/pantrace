"""把一张收集表导入资源库。

用法
----
    # 先干跑，看能提出多少、哪些行有问题（不写库）
    .venv/Scripts/python.exe scripts/import_doc.py --file 资源表.csv --dry-run

    # 确认没问题再入库
    .venv/Scripts/python.exe scripts/import_doc.py --file 资源表.csv --name "老王的表"

    # 纯文本（一行一条），或管道
    .venv/Scripts/python.exe scripts/import_doc.py --text links.txt --name 微信收藏

    cat links.txt | .venv/Scripts/python.exe scripts/import_doc.py --stdin

支持 CSV / TSV，自动识别编码（Excel 导出的中文 CSV 常是 GBK）。表头自动判断，
认不出就当纯数据行——宁可多算一行，也不要把第一条资源吃掉。

**这个脚本是幂等的。** 同一张表反复导入，已存在的资源只会更新 `last_seen_at`，
不会重复。所以"每天监控文档有没有更新"不需要 diff 逻辑：每天跑一次，
看 `新增 N` 就知道更新了什么。

判读报告
--------
    新增 0、更新 12     表没变
    新增 3、更新 12     表里加了 3 条新的
    可入库 0、跳过 20   列结构变了或链接全被挡住，去看跳过原因
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import intake, store  # noqa: E402
from app.config import get_settings  # noqa: E402


def read_csv(path: Path) -> list[list[str]]:
    """读 CSV/TSV。编码按 utf-8-sig → gbk 依次试，分隔符按扩展名和首行猜。"""
    raw = path.read_bytes()
    text = ""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        raise SystemExit(f"读不出编码：{path}")

    if path.suffix.lower() in (".tsv", ".tab"):
        delim = "\t"
    else:
        first = text.splitlines()[0] if text.splitlines() else ""
        delim = "\t" if first.count("\t") > first.count(",") else ","

    reader = csv.reader(io.StringIO(text), delimiter=delim)
    return [row for row in reader]


def main() -> int:
    ap = argparse.ArgumentParser(description="收集表 → 资源库")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="CSV / TSV 文件")
    src.add_argument("--text", help="纯文本文件，一行一条")
    src.add_argument("--stdin", action="store_true", help="从标准输入读")
    ap.add_argument("--name", default="doc", help="来源名，会写进 resources.source_name")
    ap.add_argument("--dry-run", action="store_true", help="只解析不入库")
    ap.add_argument("--show-skipped", type=int, default=10, help="显示前 N 条跳过原因")
    args = ap.parse_args()

    if args.stdin:
        result = intake.parse_text(sys.stdin.read(), source_name=args.name)
    elif args.text:
        result = intake.parse_text(
            Path(args.text).read_text(encoding="utf-8", errors="replace"),
            source_name=args.name,
        )
    else:
        result = intake.parse_rows(read_csv(Path(args.file)), source_name=args.name)

    print(f"源：{args.name}")
    print(f"  数据行 {result.rows} | 链接 {result.links} | 可入库 {result.resources}")
    if result.untitled:
        print(f"  其中 {result.untitled} 条没有标题，用了占位名（搜不到，建议回表补）")
    if result.skipped:
        print(f"  跳过 {len(result.skipped)} 行/条")
        for line_no, reason in result.skipped[: args.show_skipped]:
            print(f"    第{line_no}行  {reason}")
        if len(result.skipped) > args.show_skipped:
            print(f"    ... 另 {len(result.skipped) - args.show_skipped} 条")

    by_platform: dict[str, int] = {}
    for item in result.items:
        by_platform[item["platform"]] = by_platform.get(item["platform"], 0) + 1
    if by_platform:
        print(f"  平台分布 {by_platform}")

    if args.dry_run:
        print("\n（干跑，未写库）")
        return 0
    if not result.items:
        print("\n没有可入库的资源。")
        return 1

    store.init(Path(get_settings().library_path))
    got = store.ingest(result.items)
    total = store.stats()["total"]
    print(f"\n入库：新增 {got['added']}，更新 {got['touched']}（库内共 {total} 条）")
    if got["added"] == 0:
        print("  新增为 0：这张表的内容已经在库里了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
