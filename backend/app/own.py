"""自有精选链接：站方维护的网盘资源库。

定位（2026-11 运营决策）：站里的搜索流量先导向站方自己的分享链接，
上游结果照常显示。三条设计约定：

1. 匹配用「归一化 + 双向包含」，不搞分词。搜索词几乎都是
   "核心词 + 季数/全集/清晰度" 结构，双向包含一条规则接住全部变体；
   复杂算法在这个量级上只会让问题更难排查。
2. 接不住的词全部落库（own_misses），看板列出"搜过但没接住的词"——
   补别名不靠猜，靠这份日志。命中率永远到不了 100%，闭环才是一切。
3. 和埋点、反馈共用同一个 SQLite 文件，备份一次就够。

失效兜底复用 feedback 的自动隐藏机制：自有卡和上游卡一样带 shareUrl，
被 ≥2 个独立访客报 dead_link 后由 main._apply_hidden 统一摘掉，
own 这边不重复造一套。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_db_path: Path | None = None
_lock = threading.Lock()
_inited = False

# 链接列表 60 秒缓存：每次搜索都查库没必要，改配置晚一分钟生效没人在意
_list_cache: dict = {"at": 0.0, "rows": []}

# 一条搜索最多带出几张自有卡。库里越长越多时，不设上限会把结果页顶满
MAX_MATCH = 3

# 未命中日志上限：超过就删次数最少的词。这份日志是给运营看的，
# 留头部就够，不替它养一个无限长的表。
MISS_MAX_ROWS = 3000

MAX_LEN = {
    "keyword": 60,
    "aliases": 300,
    "title": 200,
    "platform": 40,
    "share_url": 500,
    "share_pwd": 20,
    "note": 200,
}

_PLATFORMS = {}  # 由 main.py 注入合法平台 key，录入时校验用


def init(db_path: str | Path) -> None:
    global _db_path, _inited
    _db_path = Path(db_path).expanduser()
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_schema()
    _inited = True


def set_platforms(keys: dict[str, str]) -> None:
    """注入平台表（key -> 中文名），录入校验与看板展示共用。"""
    global _PLATFORMS
    _PLATFORMS = dict(keys)


def is_ready() -> bool:
    return _inited


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("own 未初始化，先调用 init()")
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
            CREATE TABLE IF NOT EXISTS own_links (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword     TEXT    NOT NULL,
                aliases     TEXT    NOT NULL DEFAULT '',
                title       TEXT    NOT NULL DEFAULT '',
                platform    TEXT    NOT NULL DEFAULT '',
                share_url   TEXT    NOT NULL DEFAULT '',
                share_pwd   TEXT    NOT NULL DEFAULT '',
                file_count  INTEGER NOT NULL DEFAULT 0,
                size_bytes  INTEGER NOT NULL DEFAULT 0,
                note        TEXT    NOT NULL DEFAULT '',
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_ts  REAL    NOT NULL,
                updated_ts  REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_own_enabled ON own_links(enabled);
            CREATE TABLE IF NOT EXISTS own_misses (
                query    TEXT PRIMARY KEY,
                n        INTEGER NOT NULL DEFAULT 1,
                first_ts REAL    NOT NULL,
                last_ts  REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_own_misses_n ON own_misses(n DESC);
            """
        )


# ---- 归一化与匹配 ----

def normalize(text: str) -> str:
    """搜索词 / 关键词统一口径：小写、全角转半角、去全部空白。

    去空白是刻意的：'尸兄 第一季' 和 '尸兄第一季' 必须命中同一条链接，
    双向包含只有两边都不带空格才对得齐。
    """
    s = (text or "").strip().lower()
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            ch = " "
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII 区
            ch = chr(code - 0xFEE0)
        out.append(ch)
    return re.sub(r"\s+", "", "".join(out))


def _clip(value: object, field: str) -> str:
    if value is None:
        return ""
    text = str(value)
    text = "".join(ch for ch in text if ch >= " " or ch in "\t")
    return text.strip()[: MAX_LEN.get(field, 200)]


def _clean_url(url: object) -> str:
    text = _clip(url, "share_url")
    return text if re.match(r"^https?://", text, re.IGNORECASE) else ""


def _terms_of(row: sqlite3.Row | dict) -> list[str]:
    """一条链接的全部命中词：主关键词 + 别名，归一化后去重。"""
    raw = [row["keyword"], *(row["aliases"] or "").split(",")]
    terms = []
    for t in raw:
        n = normalize(t)
        if n and n not in terms:
            terms.append(n)
    return terms


def _link_matches(query_norm: str, row: sqlite3.Row) -> bool:
    """双向包含：搜索词包含关键词（变体搜索），或关键词包含搜索词（短搜长名）。

    两个方向都要求长度 ≥ 2：单字词双向匹配会把结果页搅成粥。
    标题兜底：搜索词出现在分享标题里也算——运营补别名之前先接住一波。
    """
    if len(query_norm) < 2:
        return False
    for term in _terms_of(row):
        if len(term) < 2:
            continue
        if term in query_norm or query_norm in term:
            return True
    title_norm = normalize(row["title"])
    return bool(title_norm) and query_norm in title_norm


def list_links(force: bool = False) -> list[dict]:
    """全部自有链接（含停用的，admin 要看全量）。60 秒缓存。"""
    global _list_cache
    now = time.time()
    if not force and now - _list_cache["at"] < 60:
        return _list_cache["rows"]
    if not _inited:
        return []
    with _session() as conn:
        rows = conn.execute(
            "SELECT * FROM own_links ORDER BY updated_ts DESC"
        ).fetchall()
    out = [dict(r) for r in rows]
    _list_cache = {"at": now, "rows": out}
    return out


def match(query: str, platforms: list[str] | None = None) -> list[dict]:
    """搜索词 → 命中的自有链接（干净字段，直接插结果集顶部）。

    platforms 传当前筛选的平台 key 列表：用户明确筛选了平台还给他
    塞一张别的平台的卡，筛选就名存实亡了。空列表 / None = 不限平台。
    """
    if not _inited:
        return []
    q = normalize(query)
    if len(q) < 2:
        return []
    rows = [r for r in list_links() if r.get("enabled")]
    if platforms:
        rows = [r for r in rows if r.get("platform") in set(platforms)]

    hits: list[tuple[int, sqlite3.Row]] = []
    for r in rows:
        if _link_matches(q, r):
            # 关键词越长越具体，排前面；同长按点击价值不管了，量级太小没意义
            longest = max((len(t) for t in _terms_of(r)), default=0)
            hits.append((longest, r))
    hits.sort(key=lambda pair: -pair[0])
    seen: set[int] = set()
    out: list[dict] = []
    for _, r in hits[:MAX_MATCH]:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(
            {
                "id": r["id"],
                "platform": r["platform"],
                "platformName": _PLATFORMS.get(r["platform"], r["platform"] or "未知平台"),
                "title": r["title"] or r["keyword"],
                "shareUrl": r["share_url"],
                "sharePwd": r["share_pwd"],
                "fileCount": r["file_count"],
                "sizeBytes": r["size_bytes"],
                "note": r["note"],
                "isOwn": True,
                "source": "own",
            }
        )
    return out


def matched_queries_miss(query: str) -> bool:
    """这个搜索词现在接得住吗（供未命中榜过滤已补录的旧词）。"""
    return bool(match(query))


def record_miss(query: str) -> None:
    """记一个没接住的搜索词。写库失败不抛——这行日志不值得影响搜索。"""
    if not _inited:
        return
    raw = (query or "").strip()[:100]
    if len(raw) < 2:
        return
    now = time.time()
    try:
        with _lock:
            with _session() as conn:
                conn.execute(
                    """INSERT INTO own_misses (query, n, first_ts, last_ts)
                       VALUES (?, 1, ?, ?)
                       ON CONFLICT(query) DO UPDATE SET
                         n = n + 1, last_ts = excluded.last_ts""",
                    (raw, now, now),
                )
                # 超上限删最冷的词，顺手做，不单独跑定时任务
                total = conn.execute("SELECT COUNT(*) AS c FROM own_misses").fetchone()["c"]
                if total > MISS_MAX_ROWS:
                    conn.execute(
                        """DELETE FROM own_misses WHERE query IN (
                             SELECT query FROM own_misses ORDER BY n ASC, last_ts ASC LIMIT ?)""",
                        (total - MISS_MAX_ROWS,),
                    )
    except Exception:  # noqa: BLE001
        pass


def misses(days: int = 30, limit: int = 20) -> list[dict]:
    """看板用的"搜过但没接住的词"，按次数降序。

    口径：区间内搜过、且**现在**仍接不住的词——已补录别名的旧词自动消失，
    这份榜单永远是纯待办，不是历史流水。
    """
    if not _inited:
        return []
    days = max(1, min(365, int(days or 30)))
    cutoff = time.time() - days * 86400
    with _session() as conn:
        rows = conn.execute(
            """SELECT query, n, last_ts FROM own_misses
               WHERE last_ts >= ? ORDER BY n DESC, last_ts DESC LIMIT 200""",
            (cutoff,),
        ).fetchall()
    out = []
    for r in rows:
        if matched_queries_miss(r["query"]):
            continue
        out.append({"q": r["query"], "n": r["n"], "last": int(r["last_ts"])})
        if len(out) >= max(1, min(50, limit)):
            break
    return out


def plan(days: int = 30, limit: int = 40) -> dict:
    """上架清单：近 N 天被搜得最多的词，已上架的自动剔除。

    口径只有一条——**搜索次数**。不看点击、不看有没有结果、不分类：
    搜得越多越先上架，就这么简单。

    u（去重访客数）只是附带展示，用来识别"一个人反复搜"，
    不参与排序；排序严格按次数 n。
    """
    if not _inited:
        return {"days": days, "items": [], "covered": 0}

    days = max(1, min(365, int(days or 30)))
    limit = max(1, min(100, int(limit or 40)))
    start = time.time() - days * 86400
    buckets: dict[str, dict] = {}

    try:
        with _session() as conn:
            rows = conn.execute(
                """SELECT query AS q, COUNT(*) AS n,
                          COUNT(DISTINCT CASE WHEN visitor<>'' THEN visitor END) AS u,
                          MAX(ts) AS last
                   FROM events
                   WHERE kind='search' AND bot=0 AND ts >= ? AND query<>''
                   GROUP BY query ORDER BY n DESC LIMIT 500""",
                (start,),
            ).fetchall()
    except sqlite3.Error:
        rows = []

    for r in rows:
        raw = str(r["q"] or "").strip()[:100]
        key = normalize(raw)  # 同一词的不同写法（空格/全角）合并计数
        if len(key) < 2:
            continue
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {"q": raw, "n": 0, "u": 0, "last": 0.0}
        b["n"] += int(r["n"] or 0)
        b["u"] = max(b["u"], int(r["u"] or 0))
        b["last"] = max(b["last"], float(r["last"] or 0))

    items = []
    covered = 0
    for b in buckets.values():
        if match(b["q"]):  # 已经接得住了，不是待办
            covered += 1
            continue
        items.append(
            {
                "q": b["q"],
                "n": b["n"],
                "u": b["u"],
                "last": int(b["last"]),
            }
        )

    # 只看搜索次数；次数相同按最近被搜的时间
    items.sort(key=lambda x: (-x["n"], -x["last"], x["q"]))
    return {"days": days, "items": items[:limit], "covered": covered}


def save(payload: dict) -> dict:
    """新增 / 更新一条链接。校验失败抛 ValueError，接口层转 400。"""
    if not _inited:
        raise ValueError("精选链接未启用")
    keyword = _clip(payload.get("keyword"), "keyword")
    if not keyword:
        raise ValueError("主关键词不能为空")
    platform = _clip(payload.get("platform"), "platform")
    if platform and platform not in _PLATFORMS:
        raise ValueError("平台不在列表里，检查平台 key")
    share_url = _clean_url(payload.get("shareUrl"))
    if not share_url:
        raise ValueError("分享链接必须是 http/https 地址")
    aliases = ",".join(
        t.strip()[:60] for t in _clip(payload.get("aliases"), "aliases").split(",") if t.strip()
    )
    try:
        file_count = max(0, int(float(payload.get("fileCount") or 0)))
        size_bytes = max(0, int(float(payload.get("sizeBytes") or 0)))
    except (TypeError, ValueError):
        file_count = size_bytes = 0
    enabled = 0 if payload.get("enabled") is False else 1
    now = time.time()
    link_id = payload.get("id")
    with _lock:
        with _session() as conn:
            if link_id:
                cur = conn.execute(
                    """UPDATE own_links SET keyword=?, aliases=?, title=?, platform=?,
                         share_url=?, share_pwd=?, file_count=?, size_bytes=?, note=?,
                         enabled=?, updated_ts=? WHERE id=?""",
                    (
                        keyword, aliases, _clip(payload.get("title"), "title"), platform,
                        share_url, _clip(payload.get("sharePwd"), "share_pwd"),
                        file_count, size_bytes, _clip(payload.get("note"), "note"),
                        enabled, now, int(link_id),
                    ),
                )
                if cur.rowcount == 0:
                    raise ValueError("没有这条链接")
            else:
                cur = conn.execute(
                    """INSERT INTO own_links
                       (keyword, aliases, title, platform, share_url, share_pwd,
                        file_count, size_bytes, note, enabled, created_ts, updated_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        keyword, aliases, _clip(payload.get("title"), "title"), platform,
                        share_url, _clip(payload.get("sharePwd"), "share_pwd"),
                        file_count, size_bytes, _clip(payload.get("note"), "note"),
                        enabled, now, now,
                    ),
                )
                link_id = int(cur.lastrowid or 0)
    list_links(force=True)
    return {"id": int(link_id)}


def delete(link_id: int) -> bool:
    if not _inited:
        return False
    with _lock:
        with _session() as conn:
            cur = conn.execute("DELETE FROM own_links WHERE id=?", (int(link_id),))
    list_links(force=True)
    return cur.rowcount > 0


def stats() -> dict:
    """admin 面板小数字：总数 / 启用数 / 未命中词数。"""
    if not _inited:
        return {"total": 0, "enabled": 0, "misses": 0}
    with _session() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM own_links").fetchone()["c"]
        enabled = conn.execute(
            "SELECT COUNT(*) AS c FROM own_links WHERE enabled=1"
        ).fetchone()["c"]
        miss = conn.execute("SELECT COUNT(*) AS c FROM own_misses").fetchone()["c"]
    return {"total": total, "enabled": enabled, "misses": miss}
