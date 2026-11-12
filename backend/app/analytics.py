"""访问埋点：本地 SQLite 单文件存储 + 聚合查询。

为什么不用第三方统计（GA / 百度统计 / Umami 云）：
1. 搜索词本身就是用户隐私（网盘资源），没必要交出去；
2. 国内服务器访问 GA 不稳定，百度统计要备案域名；
3. 这个站的部署形态是单机单 worker，一个 SQLite 文件就够，零外部依赖。

写入约定：所有写操作都要放在 asyncio.to_thread 里，不能挡住事件循环。
读操作只发生在 /admin 看板，量很小，直接同步读。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- 全局状态：由 init() 设置 ----
_db_path: Path | None = None
_tz = timezone(timedelta(hours=8))
_retain_days = 180
_write_lock = threading.Lock()
_inited = False
_last_cleanup = 0.0

# 允许的事件类型。pv/leave 看流量质量，search 看业务，click/copy_pwd 看转化，
# filter/page/retry 看使用路径。前端传白名单以外的值一律丢弃，
# 避免这张表被垃圾数据灌满。
KINDS = {
    "pv",        # 打开页面
    "leave",     # 一次可见时段结束（visibilitychange hidden），duration_ms = 本次停留
    "search",    # 后端在 /api/search 里记录，比前端上报可靠
    "click",     # 点开某条资源（target=标题）
    "copy_pwd",  # 复制提取码
    "filter",    # 切换平台筛选
    "page",      # 翻页
    "retry",     # 失败后重试
}

_VISITOR_RE = re.compile(r"^[A-Za-z0-9_\-]{6,64}$")

# 已知机器人/脚本 UA。只用于统计打标（bot=1），不拦截——
# 误伤真人比多记几行数据代价大。看板汇总一律排除 bot=1 的行。
_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|curl|wget|python-requests|python-urllib"
    r"|okhttp|scrapy|go-http-client|java/|httpclient|axios|node-fetch|headless",
    re.IGNORECASE,
)


def init(db_path: str | Path, tz_offset: int = 8, retain_days: int = 180) -> None:
    """初始化数据库。服务启动时调用一次。"""
    global _db_path, _tz, _retain_days, _inited
    _db_path = Path(db_path).expanduser()
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _tz = timezone(timedelta(hours=tz_offset))
    _retain_days = max(1, retain_days)
    _ensure_schema()
    _inited = True


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("analytics 未初始化，先调用 init()")
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _session():
    """sqlite3 的连接上下文只提交事务、不关连接，必须自己关，否则连接会越攒越多。"""
    conn = _connect()
    try:
        with conn:  # 事务：正常提交，异常回滚
            yield conn
    finally:
        conn.close()


def _ensure_schema() -> None:
    # WAL 是持久化设置，设一次就够，不必每次连接都执行
    with _session() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL    NOT NULL,
                day          TEXT    NOT NULL,
                kind         TEXT    NOT NULL,
                visitor      TEXT    NOT NULL DEFAULT '',
                session      TEXT    NOT NULL DEFAULT '',
                ip           TEXT    NOT NULL DEFAULT '',
                ua           TEXT    NOT NULL DEFAULT '',
                referer      TEXT    NOT NULL DEFAULT '',
                path         TEXT    NOT NULL DEFAULT '',
                query        TEXT    NOT NULL DEFAULT '',
                platform     TEXT    NOT NULL DEFAULT '',
                target       TEXT    NOT NULL DEFAULT '',
                ok           INTEGER NOT NULL DEFAULT 1,
                duration_ms  INTEGER NOT NULL DEFAULT 0,
                result_count INTEGER NOT NULL DEFAULT 0,
                cached       INTEGER NOT NULL DEFAULT 0,
                device       TEXT    NOT NULL DEFAULT '',
                is_new       INTEGER NOT NULL DEFAULT 0,
                bot          INTEGER NOT NULL DEFAULT 0,
                meta         TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
            CREATE INDEX IF NOT EXISTS idx_events_day     ON events(day, kind);
            CREATE INDEX IF NOT EXISTS idx_events_visitor ON events(visitor, ts);
            CREATE INDEX IF NOT EXISTS idx_events_kind    ON events(kind, ts);
            """
        )
        # 旧库升级：bot 列是后加的，已有表不会走 CREATE TABLE，单独补一列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
        if "bot" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN bot INTEGER NOT NULL DEFAULT 0")


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, _tz).strftime("%Y-%m-%d")


def _clip(value, limit: int) -> str:
    """截断字符串并去掉控制字符。埋点是外部输入，不能原样落库。"""
    if value is None:
        return ""
    text = str(value)
    text = "".join(ch for ch in text if ch >= " " or ch in "\t")
    return text[:limit]


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def record(
    kind: str,
    *,
    visitor: str = "",
    session: str = "",
    ip: str = "",
    ua: str = "",
    referer: str = "",
    path: str = "",
    query: str = "",
    platform: str = "",
    target: str = "",
    ok: bool = True,
    duration_ms: int = 0,
    result_count: int = -1,
    cached: bool = False,
    is_new: bool = False,
    meta: dict | None = None,
) -> None:
    """写一条事件。任何字段问题都吞掉——埋点绝不能影响主业务。"""
    global _last_cleanup
    if not _inited or kind not in KINDS:
        return

    ts = time.time()
    row = (
        ts,
        _day_of(ts),
        kind,
        _clip(visitor, 64) if _VISITOR_RE.match(visitor or "") else "",
        _clip(session, 64),
        _clip(ip, 64),
        _clip(ua, 300),
        _clip(referer, 300),
        _clip(path, 200),
        _clip(query, 100),
        _clip(platform, 100),
        _clip(target, 200),
        1 if ok else 0,
        max(0, _to_int(duration_ms)),
        _to_int(result_count, -1),
        1 if cached else 0,
        "mobile" if re.search(r"Mobile|Android|iPhone|iPad", ua or "") else "desktop",
        1 if is_new else 0,
        1 if (ua and _BOT_RE.search(ua)) else 0,
        json.dumps(meta, ensure_ascii=False)[:500] if meta else "",
    )

    try:
        with _write_lock:
            with _session() as conn:
                conn.execute(
                    """INSERT INTO events
                       (ts, day, kind, visitor, session, ip, ua, referer, path,
                        query, platform, target, ok, duration_ms, result_count,
                        cached, device, is_new, bot, meta)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                # 每天最多清理一次，顺手把过期数据删掉，避免库无限长大
                if ts - _last_cleanup > 86400:
                    _last_cleanup = ts
                    cutoff = ts - _retain_days * 86400
                    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    except Exception:  # noqa: BLE001
        # 落库失败只影响统计，不影响搜索。不抛、不刷日志。
        pass


def record_search(
    *,
    query: str,
    platform: str,
    page: int,
    ok: bool,
    duration_ms: int,
    result_count: int,
    cached: bool,
    visitor: str = "",
    session: str = "",
    ip: str = "",
    ua: str = "",
    referer: str = "",
    is_new: bool = False,
    error: str = "",
) -> None:
    """搜索事件。由 /api/search 调用，服务端记录，比前端上报可靠（不怕广告拦截）。"""
    record(
        "search",
        visitor=visitor,
        session=session,
        ip=ip,
        ua=ua,
        referer=referer,
        path="/",
        query=query,
        platform=platform,
        ok=ok,
        duration_ms=duration_ms,
        result_count=result_count,
        cached=cached,
        is_new=is_new,
        meta={"page": page, "error": error} if error else {"page": page},
    )


# ---------------------------------------------------------------- 查询

def _mask_ip(ip: str) -> str:
    if not ip:
        return "-"
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:4]) + ":···"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".x"
    return ip


def _range_start(days: int, now: float) -> float:
    """按本地零点对齐的区间起点，含今天往前 days 天。"""
    today = datetime.fromtimestamp(now, _tz).date()
    first = today - timedelta(days=max(0, days - 1))
    return datetime.combine(first, datetime.min.time(), tzinfo=_tz).timestamp()


def summary(days: int = 7) -> dict:
    """看板数据，分三层。

    首层（每天必看）：搜索量、点击转化率、零结果率；
    健康层（出问题才看）：失败率、耗时、缓存命中、机器人、24h 分布；
    决策层（要拍板才看）：关键词榜、搜不到词榜、热门资源、来源、平台、新客回头客。
    所有汇总一律排除 bot=1 的行，机器人流量单独给一个数。
    """
    days = max(1, min(365, _to_int(days, 7)))
    start = _range_start(days, time.time())

    with _session() as conn:
        overview = conn.execute(
            """SELECT
                 SUM(CASE WHEN kind='pv' THEN 1 ELSE 0 END) AS pv,
                 COUNT(DISTINCT CASE WHEN visitor<>'' THEN visitor END) AS uv,
                 SUM(CASE WHEN kind='search' THEN 1 ELSE 0 END) AS searches,
                 SUM(CASE WHEN kind='click' THEN 1 ELSE 0 END) AS clicks,
                 SUM(CASE WHEN kind='search' AND ok=1 AND result_count=0
                          THEN 1 ELSE 0 END) AS zero,
                 SUM(CASE WHEN kind='search' AND ok=0 THEN 1 ELSE 0 END) AS fails,
                 SUM(CASE WHEN kind='search' AND cached=1 THEN 1 ELSE 0 END) AS cached_hits,
                 AVG(CASE WHEN kind='search' AND ok=1 AND cached=0 AND duration_ms>0
                          THEN duration_ms END) AS avg_ms,
                 COUNT(DISTINCT CASE WHEN visitor<>'' AND is_new=1 THEN visitor END)
                          AS new_visitors
               FROM events
               WHERE kind IN ('pv', 'search', 'click') AND ts >= ? AND bot=0""",
            (start,),
        ).fetchone()
        bot_searches = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='search' AND ts >= ? AND bot=1",
            (start,),
        ).fetchone()["n"]
        dwell = conn.execute(
            """SELECT AVG(duration_ms) AS ms FROM events
               WHERE kind='leave' AND ts >= ? AND bot=0 AND duration_ms > 0""",
            (start,),
        ).fetchone()["ms"]

        keywords = conn.execute(
            """SELECT query AS q, COUNT(*) AS n
               FROM events
               WHERE kind='search' AND ts >= ? AND query<>'' AND bot=0
               GROUP BY query ORDER BY n DESC, MAX(ts) DESC LIMIT 20""",
            (start,),
        ).fetchall()
        # 搜不到的词：成功返回但 0 条。这是词库运营最有用的一个数。
        zero_keywords = conn.execute(
            """SELECT query AS q, COUNT(*) AS n
               FROM events
               WHERE kind='search' AND ts >= ? AND query<>''
                     AND ok=1 AND result_count=0 AND bot=0
               GROUP BY query ORDER BY n DESC, MAX(ts) DESC LIMIT 20""",
            (start,),
        ).fetchall()
        platforms = conn.execute(
            """SELECT platform AS key, COUNT(*) AS n
               FROM events
               WHERE kind='search' AND ts >= ? AND platform<>'' AND bot=0
               GROUP BY platform ORDER BY n DESC LIMIT 12""",
            (start,),
        ).fetchall()
        targets = conn.execute(
            """SELECT target AS t, COUNT(*) AS n
               FROM events
               WHERE kind='click' AND ts >= ? AND target<>'' AND bot=0
               GROUP BY target ORDER BY n DESC LIMIT 10""",
            (start,),
        ).fetchall()
        referers = conn.execute(
            """SELECT referer AS r, COUNT(*) AS n
               FROM events
               WHERE kind='pv' AND ts >= ? AND referer<>'' AND bot=0
               GROUP BY referer ORDER BY n DESC LIMIT 10""",
            (start,),
        ).fetchall()
        # 24 小时分布（按配置时区），搜索口径
        hourly_rows = conn.execute(
            """SELECT ((CAST(ts AS INTEGER) + ?) / 3600) % 24 AS hour, COUNT(*) AS n
               FROM events
               WHERE kind='search' AND ts >= ? AND bot=0
               GROUP BY hour""",
            (int(_tz.utcoffset(None).total_seconds()), start),
        ).fetchall()
        recent = conn.execute(
            """SELECT ts, kind, query, platform, target, ok, device, ip
               FROM events
               WHERE bot=0 AND ts >= ?
               ORDER BY ts DESC LIMIT 30""",
            (start,),
        ).fetchall()

    searches = overview["searches"] or 0
    clicks = overview["clicks"] or 0
    zero = overview["zero"] or 0
    fails = overview["fails"] or 0
    cached_hits = overview["cached_hits"] or 0
    successful = searches - fails  # 零结果率、缓存命中率都按成功搜索算

    hourly = [0] * 24
    for r in hourly_rows:
        h = _to_int(r["hour"], -1)
        if 0 <= h <= 23:
            hourly[h] = r["n"]

    return {
        "days": days,
        "serverTime": int(time.time()),
        "overview": {
            "pv": overview["pv"] or 0,
            "uv": overview["uv"] or 0,
            "searches": searches,
            "clicks": clicks,
            "ctr": round(clicks / searches, 4) if searches else 0,
            "zero_rate": round(zero / successful, 4) if successful else 0,
            "fail_rate": round(fails / searches, 4) if searches else 0,
            "cache_rate": round(cached_hits / successful, 4) if successful else 0,
            "avg_ms": int(overview["avg_ms"] or 0),
            "dwell_ms": int(dwell or 0),
            "bot_searches": bot_searches,
            "new_visitors": overview["new_visitors"] or 0,
            "returning_visitors": max(
                0, (overview["uv"] or 0) - (overview["new_visitors"] or 0)
            ),
        },
        "keywords": [{"q": r["q"], "n": r["n"]} for r in keywords],
        "zero_keywords": [{"q": r["q"], "n": r["n"]} for r in zero_keywords],
        "platforms": [{"key": r["key"], "n": r["n"]} for r in platforms],
        "targets": [{"t": r["t"], "n": r["n"]} for r in targets],
        "referers": [{"r": r["r"], "n": r["n"]} for r in referers],
        "hourly": hourly,
        "recent": [
            {
                "ts": int(r["ts"]),
                "kind": r["kind"],
                "query": r["query"],
                "platform": r["platform"],
                "target": r["target"],
                "ok": bool(r["ok"]),
                "device": r["device"],
                "ip": _mask_ip(r["ip"]),
            }
            for r in recent
        ],
    }
