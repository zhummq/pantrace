"""资源库覆盖率自检：回答"现在把本地检索接进搜索，会怎样"。

为什么要有这个脚本
------------------
P1（本地检索接管出口）被冻结了，条件是"库里链接达到一定数量"。但这个条件**不能用条数定**。
实测本地埋点数据：

    有效搜索 48 次，涉及 11 个不同词
    第 1 个词（尸兄）      占 33.3%
    前 3 个词             占 64.6%
    前 5 个词             占 85.4%

需求是尖峰结构。库里堆 5000 条长尾货，可能一个热门词都盖不住，接进去用户只会觉得"结果变少了"。
反过来，库只有 200 条但把前 5 个词全盖住，就已经能接。

所以判据是**加权覆盖率**：按搜索次数给每个词权重，看库能接住多少比例的**真实搜索行为**。

用法
----
    # 默认：读 backend/data/analytics.db 的真实 query，跑本地库
    .venv/Scripts/python.exe scripts/library_coverage.py

    # 指定数据库 / 词表大小
    .venv/Scripts/python.exe scripts/library_coverage.py --library data/library.db --top 100

    # 只列缺口词（直接拿去当采集词表）
    .venv/Scripts/python.exe scripts/library_coverage.py --gaps

结果判读
--------
    ≥60% 加权覆盖  可以开 P1，用户体感是"结果变全"
    30~60%        还早，先补缺口词
    <30%          接了等于没接，别动

注意：这个脚本**只读**，不写库、不发网络请求。库里有多少就是多少。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import store  # noqa: E402
from app.config import get_settings  # noqa: E402

# 单个词至少返回这么多条，才算"这个需求接住了"。
# 定 3 而不是 1：只出 1 条的结果页，用户会觉得站上没货，不如老实兜底给上游。
ENOUGH = 3

# 这些词是测试残留，别让它们污染覆盖率
NOISE = {"测试", "test", "abc", "123", "666", "aaa", "。。。", "1", "11"}


def read_queries(analytics_path: Path, limit: int) -> list[tuple[str, int]]:
    """从埋点库里取真实 query，按出现次数降序。

    排除 bot 和测试词：bot 的 query 往往是扫描器的产物，不代表需求。
    """
    if not analytics_path.exists():
        return []
    conn = sqlite3.connect(f"file:{analytics_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT query, COUNT(*) AS n FROM events "
            "WHERE query IS NOT NULL AND query != '' AND (bot IS NULL OR bot = '0') "
            "GROUP BY query ORDER BY n DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for q, n in rows:
        q = (q or "").strip()
        if not q or q.lower() in NOISE:
            continue
        out.append((q, int(n)))
    return out[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description="资源库覆盖率自检")
    ap.add_argument("--library", default="", help="资源库路径，默认取 config")
    ap.add_argument("--analytics", default="data/analytics.db", help="埋点库路径")
    ap.add_argument("--top", type=int, default=100, help="取前 N 个高频词")
    ap.add_argument("--gaps", action="store_true", help="只输出缺口词")
    args = ap.parse_args()

    lib_path = Path(args.library or get_settings().library_path)
    if not lib_path.exists():
        print(f"资源库不存在：{lib_path}")
        return 2
    store.init(lib_path)

    queries = read_queries(Path(args.analytics), args.top)
    if not queries:
        print(f"埋点库里没有可用 query：{args.analytics}")
        return 2

    total_hits = sum(n for _, n in queries)
    covered = 0          # 达线词数
    weighted_ok = 0      # 达线词覆盖的搜索次数
    rows: list[tuple[str, int, int]] = []
    for q, n in queries:
        got = store.search(q, page_size=ENOUGH)["total"]
        rows.append((q, n, got))
        if got >= ENOUGH:
            covered += 1
            weighted_ok += n

    if args.gaps:
        print("# 缺口词表（0~2 条结果），可直接作为采集词表")
        for q, n, got in rows:
            if got < ENOUGH:
                print(f"{q}\t{n}次\t{got}条")
        return 0

    stats = store.stats()
    pct = weighted_ok / total_hits * 100 if total_hits else 0.0

    print(f"资源库：{lib_path}")
    print(f"  总量 {stats['total']} 条 | 状态 {stats['by_state']} | 平台 {stats['by_platform']}")
    print(f"词表：{len(queries)} 个词 / {total_hits} 次搜索")
    print()
    print(f"{'词':<16}{'搜索次数':>8}{'占比':>8}{'库存':>6}  判定")
    print("-" * 52)
    for q, n, got in rows[:20]:
        mark = "OK" if got >= ENOUGH else ("少" if got else "缺")
        print(f"{q[:14]:<16}{n:>8}{n / total_hits * 100:>7.1f}%{got:>6}  {mark}")
    if len(rows) > 20:
        print(f"... 另 {len(rows) - 20} 个词")

    print()
    print(f"加权覆盖率：{pct:.1f}%（接得住 {weighted_ok}/{total_hits} 次真实搜索）")
    print(f"词覆盖：{covered}/{len(queries)} 个词的库存 ≥{ENOUGH} 条")
    if pct >= 60:
        print("判定：可以开 P1。用户体感是「结果变全」，不是「结果变少」。")
    elif pct >= 30:
        print("判定：还早。先按 --gaps 的清单补缺口词。")
    else:
        print("判定：现在接进去等于没接，别动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
