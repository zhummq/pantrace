"""腾讯文档 → 资源库的同步作业。CLI 和看板共用这一份逻辑。

**为什么不是各写一套。** `scripts/docs_watch.py` 是命令行的入口，看板是另一个
入口，两边都要做：取数 → 切批 → 解析 → 校验列号 → 落库 → 记进度。任何一处
实现分叉，就会出现"命令行跑得好好的、面板里少了一半数据"这种没法查的问题。
所以流程只写在这里，两个入口都只负责"把参数递进来、把结果打印/渲染出去"。

三层职责：
- `ingest_rows()` 纯函数式：二维表 → 库，返回计数。可干跑。
- `sync_one()` 一份订阅的一轮增量：算起止行、取数、落库、写进度。
- `start()` / `status()` 后台作业：抓一份 38 万行的表要几分钟，HTTP 不能等，
  也不能让浏览器刷新一下就中断。作业状态放进程内存（uvicorn 单 worker），
  重启归零——它是观测不是账本。

**进度为什么存在内存而不是库。** 写 `doc_sources.last_row` 是"已经稳定读到哪"，
必须等一轮成功才更新；抓取途中的进度是过程量，几秒一个，写库会把 SQLite 的
写锁长时间占住，前台搜索的命中记录就被挤掉了。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import docs_qq, intake, store

# 每次往回多看这么多行。表格中间插入新行时，纯按 last_row 往前读会漏，
# 重叠一段靠 dedup_key 合并就能补上；512 行的代价约两次请求，很值。
LOOKBACK = 512
# 一批落库多少行。38 万行一次性 ingest 会把内存顶到几百兆，而且中途失败
# 就全丢；分批的话前面已入库的不受影响。
BATCH = 5000

# 一次最多同时跑几份表。腾讯文档侧对并发有限制，而且真正的并发在块级
# （workers），这里排队反而更快也更稳。
MAX_CONCURRENT = 2


@dataclass
class SyncReport:
    """一轮同步的结果。给 CLI 打印，也给看板渲染。"""

    source_id: int = 0
    tab: str = ""
    rows: int = 0            # 这次实际读到多少行
    max_row: int = 0         # 表有多长
    last_row: int = 0        # 进度推进到哪
    added: int = 0           # 新入库
    touched: int = 0         # 命中已有、更新了字段
    skipped: list[tuple[int, str]] = field(default_factory=list)
    ok: bool = True
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "tab": self.tab,
            "rows": self.rows,
            "max_row": self.max_row,
            "last_row": self.last_row,
            "added": self.added,
            "touched": self.touched,
            "skipped": len(self.skipped),
            "skip_reasons": skip_summary(self.skipped),
            "skip_samples": [
                {"line": n, "why": why} for n, why in self.skipped[:5]
            ],
            "ok": self.ok,
            "error": self.error,
        }


def skip_summary(skipped: list[tuple[int, str]]) -> list[dict]:
    """跳过原因按类聚合。逐条给前端没意义，几万条能把页面拖死。"""
    counts: dict[str, int] = {}
    for _, why in skipped:
        key = why.split("：")[0].split("（")[0]
        counts[key] = counts.get(key, 0) + 1
    return [
        {"reason": k, "count": n}
        for k, n in sorted(counts.items(), key=lambda kv: -kv[1])[:6]
    ]


def cols_of(src: dict) -> dict:
    """订阅上的列配置 → parse_rows 的参数。全 0 时等于没配置。"""
    return {
        "title_col": int(src.get("title_col") or 0),
        "link_col": int(src.get("link_col") or 0),
        "code_col": int(src.get("code_col") or 0),
    }


def ingest_rows(
    rows: list[list[str]],
    source_name: str,
    *,
    dry_run: bool = False,
    cols: dict | None = None,
) -> dict:
    """二维表 → 库。分批走，返回 {added, touched, skipped}。"""
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


def sync_one(
    src: dict,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    workers: int = 6,
    on_progress=None,
) -> SyncReport:
    """跑一份订阅的一轮增量。异常不吞，抛给调用方决定怎么记。"""
    label = src.get("title") or src.get("tab_id") or "默认表"
    report = SyncReport(source_id=int(src.get("id") or 0), tab=str(label))

    def progress(offset: int, total: int) -> None:
        report.rows = offset
        report.max_row = total
        if on_progress:
            on_progress(offset, total)

    start = max(0, int(src.get("last_row") or 0) - LOOKBACK) if src.get("last_row") else 0
    end_row = None if limit is None else start + limit
    rows, meta = docs_qq.read_tab(
        src["url"],
        tab_id=src.get("tab_id") or "",
        start_row=start,
        end_row=end_row,
        workers=workers,
        on_progress=progress,
    )
    tab = meta.tab(src.get("tab_id") or "")
    report.rows = len(rows)

    source_name = f"doc:{(tab.title or meta.title or label)[:40]}"
    got = ingest_rows(rows, source_name, dry_run=dry_run, cols=cols_of(src))
    report.added = got["added"]
    report.touched = got["touched"]
    report.skipped = got["skipped"]

    # 进度只写到表长为止。`limit` 是调试参数，不该把进度顶到表外面去。
    # 线上真出现过 last_row=5013 而表只有 525 行，之后每次增量都从 5013 起读，
    # 等于永远读空。
    ceiling = tab.max_row if tab else int(src.get("last_row") or 0)
    new_row = min(end_row, ceiling) if end_row is not None else ceiling
    report.max_row = tab.max_row if tab else report.max_row
    report.last_row = new_row
    if not dry_run:
        store.save_doc_progress(src["id"], last_row=new_row, last_rev=meta.rev)
    return report


# ---------------------------------------------------------------- 列探测


def probe(url: str, tab_id: str = "", *, sample_rows: int = 40) -> dict:
    """读前几十行，报出每个工作表长什么样、每列像什么。

    这是"添加数据"面板第一屏的全部依据：先让人看清表的结构，再决定点哪个
    工作表、要不要锁列号。取数只要一次请求（几百 KB），比抓整表便宜得多。

    `tab_id` 为空时**先从链接里取 `?tab=`**：用户粘的就是那一张表的链接，
    却给他列出全部七张让人挑，等于让他重新找一遍。只有链接里没带 tab 时才
    列全部。
    """
    pad_id, url_tab = docs_qq.pad_id_of(url)
    want = tab_id or url_tab
    meta = docs_qq.fetch_meta(url)
    tabs = meta.tabs if not want else [t for t in meta.tabs if t.tab_id == want]
    out_tabs = []
    for tab in tabs:
        if tab is None:
            continue
        item = {
            "tab_id": tab.tab_id,
            "title": tab.title,
            "max_row": tab.max_row,
            "max_col": tab.max_col,
            "columns": [],
            "error": "",
        }
        try:
            rows, _ = docs_qq.read_tab(url, tab_id=tab.tab_id, end_row=sample_rows)
            item["columns"] = intake.probe_columns(rows)
            item["sample_rows"] = min(len(rows), sample_rows)
        except docs_qq.DocError as exc:
            item["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            item["error"] = f"{exc.__class__.__name__}: {exc}"
        out_tabs.append(item)
    return {
        "pad_id": docs_qq.pad_id_of(url)[0],
        "title": meta.title,
        "tabs": out_tabs,
    }


def check_link_column(url: str, tab_id: str, link_col: int) -> str:
    """登记/改配置前校验链接列。返回警告文案，空串表示没问题。"""
    if link_col < 1:
        return ""
    rows, _ = docs_qq.read_tab(url, tab_id=tab_id, end_row=40)
    return intake.check_link_column(rows, link_col)


# ---------------------------------------------------------------- 后台作业

_lock = threading.Lock()
_jobs: dict[int, dict] = {}       # source_id → 作业状态
_running: set[int] = set()


def _now() -> int:
    return int(time.time())


def status(source_id: int | None = None) -> dict:
    """作业状态。传 None 返回全部（看板一次拿走）。"""
    with _lock:
        if source_id is None:
            return {str(k): dict(v) for k, v in _jobs.items()}
        return dict(_jobs.get(source_id) or {})


def is_running(source_id: int) -> bool:
    return source_id in _running


def start(source_id: int, **kwargs) -> dict:
    """起一个后台作业。已经在跑的直接返回现有状态，不排队第二份。

    同一份表重复触发是常态（用户手快点了两下、页面自动刷新又点了一次）。
    排队执行会让"新增条数"看起来翻倍，其实第二次全是 touched。
    """
    with _lock:
        if source_id in _running:
            return dict(_jobs[source_id])
        if len(_running) >= MAX_CONCURRENT:
            return {"state": "busy", "msg": f"已有 {len(_running)} 份在抓，稍后再试"}
        _running.add(source_id)
        _jobs[source_id] = {
            "state": "running",
            "started_at": _now(),
            "finished_at": 0,
            "rows": 0,
            "total": 0,
            "added": 0,
            "touched": 0,
            "skipped": 0,
            "error": "",
        }

    threading.Thread(
        target=_run, args=(source_id, kwargs), name=f"doc-sync-{source_id}", daemon=True
    ).start()
    return status(source_id)


def _run(source_id: int, kwargs: dict) -> None:
    """作业主体。任何异常都必须落进状态，否则前端会一直转圈。"""
    def on_progress(offset: int, total: int) -> None:
        with _lock:
            job = _jobs.get(source_id)
            if job:
                job["rows"] = offset
                job["total"] = total

    try:
        src = next(
            (s for s in store.list_doc_sources() if int(s["id"]) == source_id), None
        )
        if not src:
            raise RuntimeError(f"没有 id={source_id} 的订阅")
        report = sync_one(src, workers=kwargs.get("workers", 6), on_progress=on_progress)
        with _lock:
            _jobs[source_id] = {
                "state": "done",
                "started_at": _jobs[source_id]["started_at"],
                "finished_at": _now(),
                "rows": report.rows,
                "total": report.max_row,
                "added": report.added,
                "touched": report.touched,
                "skipped": len(report.skipped),
                "skip_reasons": skip_summary(report.skipped),
                "error": "",
            }
    except Exception as exc:  # noqa: BLE001
        # 失败要落库，不然 --list 看不出"这份表连续几天没抓成功"；
        # 内存里也要留一份，前端才能立刻显示原因而不是干等。
        msg = f"{exc.__class__.__name__}: {exc}"[:200]
        try:
            store.save_doc_progress(source_id, error=msg)
        except Exception:  # noqa: BLE001
            pass
        with _lock:
            job = _jobs.get(source_id) or {}
            _jobs[source_id] = {
                "state": "error",
                "started_at": job.get("started_at", _now()),
                "finished_at": _now(),
                "rows": job.get("rows", 0),
                "total": job.get("total", 0),
                "added": 0,
                "touched": 0,
                "skipped": 0,
                "error": msg,
            }
    finally:
        with _lock:
            _running.discard(source_id)
