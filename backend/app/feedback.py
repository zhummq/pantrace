"""用户反馈与求资源：本地 SQLite 工单表。

为什么和 events（埋点）分表，虽然放同一个库：
1. events 是流水，180 天自动清；反馈是待办，没处理完不能消失。
2. 反馈要能标 new/done，流水表不该有这个字段。
3. 反馈量小但价值高，要能按链接聚合出"被反复投诉失效"的结果。

两种东西共用一张表，因为落地动作是同一个：
- 反馈问题：dead_link / pwd_wrong / wrong_content / bug
- 提交需求：missing（搜不到 / 求资源）

kind 是固定枚举，前端只能传这几个值。库里存中文自由文本没有意义——
十条"打不开"你没法聚合，十个 dead_link 你能直接去隐藏那条链接。
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
_lock = threading.Lock()
_inited = False

# 展示名放后端，看板和接口共用一份，避免前后端各写一套对不上
KINDS = {
    "dead_link": "链接失效",
    "pwd_wrong": "提取码不对",
    "wrong_content": "内容不符",
    "missing": "求资源",
    "bug": "页面报错",
    "other": "其他",
}

# 每种类型的上下文必填项。没有上下文的反馈处理不了，直接拒掉。
REQUIRED = {
    "dead_link": ("target",),
    "pwd_wrong": ("target",),
    "wrong_content": ("target",),
    "missing": ("query",),
}

MAX_LEN = {
    "message": 500,
    "query": 100,
    "platform": 40,
    "target": 200,
    "url": 500,
    "contact": 120,
    "page": 200,
}

_VISITOR_RE = re.compile(r"^[A-Za-z0-9_\-]{6,64}$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class RateLimited(Exception):
    """提交太频繁。和 ValueError 分开，接口层要回 429 而不是 400。"""

# 同一访客对同一条结果重复点"失效"，24 小时内只留一条。
# 不报错、不提示"你已提交过"——用户不关心，我们也不需要那些重复行。
DEDUPE_WINDOW = 24 * 3600

# 链接被几个不同访客报失效后从搜索结果里隐藏。
# 阈值取 2 而不是 3：这个站流量小，3 个独立访客可能永远等不到。
HIDE_THRESHOLD = 2
HIDE_WINDOW_DAYS = 30

# 隐藏列表缓存 60 秒。每次搜索都查一次库没必要，晚一分钟生效没人在意。
_hidden_cache = {"at": 0.0, "urls": frozenset(), "titles": frozenset()}

# 提交限流：每 IP 10 分钟 5 条。埋点接口是 60 条/分钟，这里必须更严，
# 否则一条脚本就能把工单表写成垃圾场。
_rate: dict[str, tuple[float, int]] = {}
RATE_WINDOW = 600
RATE_LIMIT = 5


def init(db_path: str | Path, tz_offset: int = 8, rate_limit: int = 5) -> None:
    global _db_path, _tz, _inited, RATE_LIMIT
    _db_path = Path(db_path).expanduser()
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _tz = timezone(timedelta(hours=tz_offset))
    RATE_LIMIT = max(1, int(rate_limit or 5))
    _ensure_schema()
    _inited = True


def is_ready() -> bool:
    return _inited


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("feedback 未初始化，先调用 init()")
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
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


def _ensure_schema() -> None:
    with _session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       REAL    NOT NULL,
                day      TEXT    NOT NULL,
                kind     TEXT    NOT NULL,
                message  TEXT    NOT NULL DEFAULT '',
                query    TEXT    NOT NULL DEFAULT '',
                platform TEXT    NOT NULL DEFAULT '',
                target   TEXT    NOT NULL DEFAULT '',
                url      TEXT    NOT NULL DEFAULT '',
                contact  TEXT    NOT NULL DEFAULT '',
                visitor  TEXT    NOT NULL DEFAULT '',
                ip       TEXT    NOT NULL DEFAULT '',
                ua       TEXT    NOT NULL DEFAULT '',
                page     TEXT    NOT NULL DEFAULT '',
                status   TEXT    NOT NULL DEFAULT 'new',
                note     TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_ts      ON feedback(ts);
            CREATE INDEX IF NOT EXISTS idx_feedback_status  ON feedback(status, ts);
            CREATE INDEX IF NOT EXISTS idx_feedback_target  ON feedback(kind, url, ts);
            """
        )


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, _tz).strftime("%Y-%m-%d")


def _clip(value: object, field: str) -> str:
    """截断 + 去控制字符。反馈是外部输入，原样落库就是给自己埋雷。

    换行保留：用户可能分点写，压成一行反而难读。
    """
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch >= " " or ch in "\t\n")
    return text.strip()[: MAX_LEN.get(field, 200)]


def _clean_url(url: object) -> str:
    """只留 http/https。javascript: 之类的伪协议一旦进库，
    看板里点一下就是一次 XSS，必须在入口掐掉。"""
    text = _clip(url, "url")
    return text if _URL_RE.match(text) else ""


def rate_allowed(ip: str) -> bool:
    now = time.time()
    started, count = _rate.get(ip, (now, 0))
    if now - started > RATE_WINDOW:
        started, count = now, 0
    count += 1
    _rate[ip] = (started, count)
    if len(_rate) > 4096:  # 防止字典被海量 IP 撑爆
        for key, (at, _) in list(_rate.items()):
            if now - at > RATE_WINDOW:
                _rate.pop(key, None)
    return count <= RATE_LIMIT


def submit(
    kind: str,
    *,
    message: str = "",
    query: str = "",
    platform: str = "",
    target: str = "",
    url: str = "",
    contact: str = "",
    visitor: str = "",
    ip: str = "",
    ua: str = "",
    page: str = "",
) -> dict:
    """写一条工单。返回 {"id": ..., "duplicate": bool}。

    校验失败抛 ValueError，由接口层转成 400，错误文案直接给用户看。
    """
    if not _inited:
        raise ValueError("反馈功能未启用")

    kind = (kind or "").strip()
    if kind not in KINDS:
        raise ValueError("反馈类型不正确")

    row = {
        "kind": kind,
        "message": _clip(message, "message"),
        "query": _clip(query, "query"),
        "platform": _clip(platform, "platform"),
        "target": _clip(target, "target"),
        "url": _clean_url(url),
        "contact": _clip(contact, "contact"),
        "visitor": _clip(visitor, "visitor") if _VISITOR_RE.match(visitor or "") else "",
        "ip": _clip(ip, "target"),
        "ua": _clip(ua, "target"),
        "page": _clip(page, "page"),
    }

    for field in REQUIRED.get(kind, ()):
        if not row[field]:
            raise ValueError("缺少必要信息，请刷新页面后重试")
    if kind in ("bug", "other") and not row["message"]:
        raise ValueError("写一句具体情况吧，不然没法查")
    if kind == "dead_link" and not row["url"]:
        # 没有链接的"失效"无从处理，但可能是上游没给链接，仍然收下
        row["url"] = ""

    now = time.time()
    with _lock:
        with _session() as conn:
            # 去重键：有链接按链接，没有就按标题 + 关键词。只压同一个人，
            # 不同访客都报同一条，那正是我们要的信号，不能合并。
            if row["visitor"]:
                dup = conn.execute(
                    """SELECT id FROM feedback
                       WHERE kind=? AND visitor=? AND target=? AND url=? AND query=? AND ts>?
                       ORDER BY ts DESC LIMIT 1""",
                    (
                        kind,
                        row["visitor"],
                        row["target"],
                        row["url"],
                        row["query"],
                        now - DEDUPE_WINDOW,
                    ),
                ).fetchone()
                if dup:
                    return {"id": dup["id"], "duplicate": True}

            # 限流放在校验和去重之后：用户忘填关键词不该消耗额度，
            # 重复点同一条也不该。真正要挡住的是"往库里灌垃圾"。
            if not rate_allowed(ip):
                raise RateLimited()

            cur = conn.execute(
                """INSERT INTO feedback
                   (ts, day, kind, message, query, platform, target, url, contact,
                    visitor, ip, ua, page, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'new', '')""",
                (
                    now,
                    _day_of(now),
                    row["kind"],
                    row["message"],
                    row["query"],
                    row["platform"],
                    row["target"],
                    row["url"],
                    row["contact"],
                    row["visitor"],
                    row["ip"],
                    row["ua"],
                    row["page"],
                ),
            )
            return {"id": int(cur.lastrowid or 0), "duplicate": False}


def hidden_keys() -> dict[str, frozenset]:
    """被多个独立访客报失效的链接 / 标题，60 秒缓存。

    返回两个集合：url 用于精确命中，title 兜底（上游有时 shareUrl 带参数）。
    这是反馈唯一的自动闭环——没有它，反馈就只是个越攒越多的收件箱。
    """
    global _hidden_cache
    now = time.time()
    if now - _hidden_cache["at"] < 60:
        return {"urls": _hidden_cache["urls"], "titles": _hidden_cache["titles"]}
    if not _inited:
        return {"urls": frozenset(), "titles": frozenset()}

    cutoff = now - HIDE_WINDOW_DAYS * 86400
    try:
        with _session() as conn:
            rows = conn.execute(
                """SELECT url, target, COUNT(DISTINCT visitor) AS n
                   FROM feedback
                   WHERE kind='dead_link' AND ts > ? AND visitor<>''
                   GROUP BY url, target
                   HAVING n >= ?""",
                (cutoff, HIDE_THRESHOLD),
            ).fetchall()
    except Exception:  # noqa: BLE001
        # 查不到就不隐藏。反馈把搜索搞挂是最坏的结果。
        return {"urls": frozenset(), "titles": frozenset()}

    urls = frozenset(r["url"] for r in rows if r["url"])
    titles = frozenset(r["target"] for r in rows if r["target"])
    _hidden_cache = {"at": now, "urls": urls, "titles": titles}
    return {"urls": urls, "titles": titles}


def _mask_ip(ip: str) -> str:
    if not ip:
        return "-"
    if ":" in ip:
        return ":".join(ip.split(":")[:4]) + ":···"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".x"
    return ip


def summary(days: int = 30, status: str = "", limit: int = 100) -> dict:
    """看板用的反馈汇总。

    status=new 的排最前（待办优先），同状态按时间倒序。
    """
    days = max(1, min(365, int(days or 30)))
    limit = max(1, min(300, int(limit or 100)))
    now = time.time()
    cutoff = now - days * 86400

    with _session() as conn:
        counts = conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) AS pending,
                 SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done
               FROM feedback WHERE ts >= ?""",
            (cutoff,),
        ).fetchone()
        by_kind = conn.execute(
            """SELECT kind, COUNT(*) AS n FROM feedback
               WHERE ts >= ? GROUP BY kind ORDER BY n DESC""",
            (cutoff,),
        ).fetchall()
        # 求资源清单：按词聚合，直接就是"该补什么源"的待办列表。
        # 只看未处理的——已经找到的不该继续挂在需求榜上。
        requests = conn.execute(
            """SELECT query AS q, COUNT(*) AS n, MAX(ts) AS last
               FROM feedback
               WHERE kind='missing' AND ts >= ? AND query<>'' AND status='new'
               GROUP BY query ORDER BY n DESC, last DESC LIMIT 20""",
            (cutoff,),
        ).fetchall()
        # 被多个访客报失效的链接：这正是会被自动隐藏的那批，
        # 列出来是为了让人能复核——自动隐藏误伤了得看得见。
        flagged = conn.execute(
            """SELECT url, MAX(target) AS target, COUNT(DISTINCT visitor) AS n
               FROM feedback
               WHERE kind='dead_link' AND ts >= ? AND url<>'' AND visitor<>''
               GROUP BY url HAVING n >= ? ORDER BY n DESC LIMIT 10""",
            (cutoff, HIDE_THRESHOLD),
        ).fetchall()
        if status in ("new", "done"):
            rows = conn.execute(
                """SELECT * FROM feedback WHERE ts >= ? AND status=?
                   ORDER BY ts DESC LIMIT ?""",
                (cutoff, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM feedback WHERE ts >= ?
                   ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, ts DESC
                   LIMIT ?""",
                (cutoff, limit),
            ).fetchall()

    return {
        "days": days,
        "counts": {
            "total": counts["total"] or 0,
            "pending": counts["pending"] or 0,
            "done": counts["done"] or 0,
        },
        "by_kind": [
            {"kind": r["kind"], "name": KINDS.get(r["kind"], r["kind"]), "n": r["n"]}
            for r in by_kind
        ],
        "requests": [{"q": r["q"], "n": r["n"], "last": int(r["last"])} for r in requests],
        "flagged": [
            {"url": r["url"], "target": r["target"] or "", "n": r["n"]} for r in flagged
        ],
        "items": [
            {
                "id": r["id"],
                "ts": int(r["ts"]),
                "kind": r["kind"],
                "kindName": KINDS.get(r["kind"], r["kind"]),
                "message": r["message"],
                "query": r["query"],
                "platform": r["platform"],
                "target": r["target"],
                "url": r["url"],
                "contact": r["contact"],
                "page": r["page"],
                "status": r["status"],
                "note": r["note"],
                "ip": _mask_ip(r["ip"]),
                "device": "mobile" if re.search(
                    r"Mobile|Android|iPhone|iPad", r["ua"] or ""
                ) else "desktop",
            }
            for r in rows
        ],
    }


def set_status(fid: int, status: str, note: str = "") -> bool:
    """标记处理状态。返回是否真的更新了一行。"""
    if status not in ("new", "done"):
        raise ValueError("状态只能是 new 或 done")
    if not _inited:
        return False
    with _lock:
        with _session() as conn:
            cur = conn.execute(
                "UPDATE feedback SET status=?, note=? WHERE id=?",
                (status, _clip(note, "message"), int(fid)),
            )
            return cur.rowcount > 0
