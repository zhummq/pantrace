"""来源贡献分：用户点开一条资源，给它背后的 TG 频道 / 插件加一分。

和 `analytics` 分开一张表，这是"之前老的不算，从新的开始算"的实现方式：
历史点击全在 `events` 表里，没有 sourceKey，迁不过来；新表从启用那一刻起
只收新点击。启用时刻写进 `source_score_meta`，重启不会重置，排行榜按它过滤，
所以部署当天之前的任何数据都不可能混进榜。

为什么不用 events.meta 现成的 JSON 字段统计：
- 埋点表保留 180 天就清理，排行榜是运营长期资产，不该跟着一起消失；
- `events.source` 已经被转存链路占用（cached/fresh/fallback），语义不能混；
- 按 source_key 聚合走索引，比每次扫 JSON 快，而且口径一眼能查。

写库失败一律吞掉：这是排行榜，不是主业务，不能因为记分失败影响点击跳转。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

_db_path: Path | None = None
_tz = timezone(timedelta(hours=8))
_write_lock = threading.Lock()
_inited = False

# 计分Key：`tg:<频道>` 或 `plugin:<插件名>`，和 adapter 打进 item 的 sourceKey 同形。
# 频道名只允许字母数字下划线和 - @ .，插件名同理。挡住伪造的超长/乱字符Key。
_KEY_RE = re.compile(r"^(?:tg|plugin):[A-Za-z0-9_.@-]{1,80}$")


def init(db_path: str | Path, tz_offset: int = 8) -> None:
    """建表并记下启用时刻。重复调用幂等，启用时刻只写一次。"""
    global _db_path, _tz, _inited
    _db_path = Path(db_path).expanduser()
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _tz = timezone(timedelta(hours=tz_offset))
    with _session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_scores (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL    NOT NULL,
                day         TEXT    NOT NULL,
                source_key  TEXT    NOT NULL,
                source_kind TEXT    NOT NULL,
                source_name TEXT    NOT NULL,
                visitor     TEXT    NOT NULL DEFAULT '',
                ip          TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_source_scores_key ON source_scores(source_key, ts);
            CREATE TABLE IF NOT EXISTS source_score_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # 计时起点：INSERT OR IGNORE 保证重启/重复 init 不会把起点往后推。
        conn.execute(
            "INSERT OR IGNORE INTO source_score_meta(key, value) VALUES ('enabled_at', ?)",
            (str(time.time()),),
        )
    _inited = True


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("source_score 未初始化，先调用 init()")
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _session():
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def normalize(source_key: str) -> tuple[str, str, str] | None:
    """校验并拆成 (key, kind, name)。非法 Key 返回 None，不记分。"""
    key = (source_key or "").strip()
    if not _KEY_RE.match(key):
        return None
    kind, _, name = key.partition(":")
    return key, kind, name


def record(source_key: str, *, visitor: str = "", ip: str = "", bot: bool = False) -> bool:
    """给来源加一分。返回是否真的记了。"""
    if not _inited or bot:
        return False
    parsed = normalize(source_key)
    if parsed is None:
        return False
    key, kind, name = parsed
    ts = time.time()
    try:
        with _write_lock:
            with _session() as conn:
                conn.execute(
                    """INSERT INTO source_scores
                       (ts, day, source_key, source_kind, source_name, visitor, ip)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        ts,
                        datetime.fromtimestamp(ts, _tz).strftime("%Y-%m-%d"),
                        key, kind, name,
                        (visitor or "")[:64],
                        (ip or "")[:64],
                    ),
                )
        return True
    except Exception:  # noqa: BLE001 - 记分失败不影响任何用户动作
        return False


def rank(limit: int = 20, *, since_enabled: bool = True) -> dict:
    """来源排行榜。默认只算启用之后的分——历史点击不参与。"""
    if not _inited:
        return {"since": 0, "total": 0, "rows": []}
    limit = max(1, min(200, int(limit or 20)))
    try:
        with _session() as conn:
            row = conn.execute(
                "SELECT value FROM source_score_meta WHERE key='enabled_at'"
            ).fetchone()
            since = float(row["value"]) if row else 0.0
            if not since_enabled:
                since = 0.0
            rows = conn.execute(
                """SELECT source_key, source_kind, source_name,
                          COUNT(*) AS score, MAX(ts) AS last_ts
                   FROM source_scores
                   WHERE ts >= ?
                   GROUP BY source_key, source_kind, source_name
                   ORDER BY score DESC, last_ts DESC
                   LIMIT ?""",
                (since, limit),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM source_scores WHERE ts >= ?", (since,)
            ).fetchone()["n"]
        return {
            "since": int(since),
            "total": int(total or 0),
            "rows": [
                {
                    "key": r["source_key"],
                    "kind": r["source_kind"],
                    "name": r["source_name"],
                    "score": int(r["score"]),
                    "lastTs": int(r["last_ts"] or 0),
                }
                for r in rows
            ],
        }
    except Exception:  # noqa: BLE001 - 看板查询失败不该把整页带崩
        return {"since": 0, "total": 0, "rows": []}
