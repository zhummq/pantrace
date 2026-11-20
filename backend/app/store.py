"""资源库：本地索引 + 检索 + 影子回填。

## 为什么要这个模块

现在的 `/api/search` 是纯聚合：每次搜索都去打上游，结果用完就扔。上游一变慢、
一改签名、一挂，全站立刻空手。库里攒下来的东西，才是我们能自己掌控的部分。

这里只做三件事，别的都不做：

1. **存**：把一次搜索已经拿到的结果顺手落库（影子回填）。零额外请求、零反爬风险。
2. **找**：本地 FTS5 检索，毫秒级，不依赖任何外部请求。
3. **排**：按「可用性 + 热度 + 我方观测时间」排序。

巡检、采集、上架策略都在别的模块，这里不掺。

## 身份是解析出来的分享 ID，不是 URL

去重键 `dedup_key = platform:pwd_id`，来自 `sdk/urls.py` 的 `parse()`。
同一条夸克资源有五六种写法（带 `?pwd=`、末尾斜杠、大小写域名、`#/list/share`、
微信里复制的整段文案），URL 字符串比对会变成五六条；解析成分享 ID 就只剩一条。

解析不出来的（详情页、站内页、不支持的一堆站）**根本不入库**——这道门在
`_prepare()` 里，`ParsedShare.ok` 为假直接丢。库里只允许出现"能直接打开/转存"的链接。

## 中文检索：1-gram + 2-gram，不用 trigram

`tokenize='trigram'` 看着最省事，但要求查询串 ≥3 字符，**「三体」「慈欣」这类
两字词全部搜不到**——中文最常见的搜索形态直接被废。所以自己切 gram 存进 `tk` 列。

切法有个坑：**不能先按空格分段再切 n-gram**。标题「三体 全集」会切成
`三体` / `全集` 两段，跨空格的双字 `体全` 永远进不了索引，用户搜「三体全集」
就召不回来。所以这里先剥掉所有空白和标点，把文本压成连续串再切。

代价是索引膨胀约 2 倍，换「两字词能搜到」，值。

## 排序：先筛后排，相关性是门槛不是加分

FTS 的 `MATCH` 先把"每个 gram 都出现过"的资源筛出来，再在候选集内算分。
**相关性不参与打分**——如果它是个加分项，一条无关但源等级高 1000 分的资源
会排在精准命中的前面。这跟 `search/merge.py` 里那套聚合器打分不是一回事：
那套是给"一次抓取的一批结果"排序，衡量的是"标题里有没有热词"；这里衡量的是
"跟这次查询有多相关 + 这条资源我们观测到有多可信"。

时间项用的是 `last_seen_at`（我们最后一次在上游见到它的时间），不是源站那个
发布日期——盗版站的日期基本是编的，拿它排序等于加噪声。

## 写操作全部加锁

单 worker 也挡不住多线程：`BackgroundTasks` 跑在线程池里，将来的采集器和巡检
是独立进程。WAL + `busy_timeout` 兜住跨进程，进程内用 `_write_lock` 串行化，
避免「边写边查同一连接」这种最容易把库写坏的模式。
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from .providers import PLATFORM_NAMES
from .sdk.urls import parse as parse_share

log = logging.getLogger("pantrace.store")

# ---- 全局状态：由 init() 设置 ----
_db_path: Path | None = None
_inited = False
_write_lock = threading.Lock()

# ---------------------------------------------------------------- 状态
#
# 直接复用 check.py 那五态，不另造一套。区别只在"结论保鲜策略"：
# check.py 是 TTL 6 小时的查询期缓存（给搜索结果标注用），这里是长期结论
# （给库里的资源做生命周期管理）。判断标准同一个，存储期限不同。
STATE_UNKNOWN = "unknown"
STATE_OK = "ok"
STATE_BAD = "bad"
STATE_LOCKED = "locked"
STATE_UNCERTAIN = "uncertain"
STATE_UNSUPPORTED = "unsupported"

# `bad` 不进候选集。不是扣分排最后——用户不该看到死链。
HIDDEN_STATES = (STATE_BAD,)

# ---------------------------------------------------------------- 打分权重
#
# 全部提成常量，调排序时只动这里。分数的绝对值不重要，相对关系才是：
# 可用性(300) 压过一切，其次是热度，再次是新鲜度，源等级和完整度做微调。
W_STATE_OK = 300
W_STATE_UNKNOWN = 50
W_HIT = 120          # log(1+hit_count) 的系数
W_CLICK = 200        # 点击比命中更有信息量，权重更高
W_PWD = 100          # 有提取码，少一次跳转
W_FILE_COUNT = 30
W_SIZE = 20
SOURCE_LEVEL_SCORE = {1: 300, 2: 150, 3: 0, 4: -100}

# 新鲜度阶梯：(天数上限, 分数)。按 last_seen_at 算，不看源站时间。
FRESH_STEPS = ((7, 150), (30, 80), (90, 40), (365, 15))

# 平台 key 的别名。`sdk/urls.py` 解析 139 云盘返回 `c139`，但项目对外统一叫
# `yidong`（PLATFORM_NAMES 里就是这个名字），入库前对齐，否则按平台筛选会漏。
_PLATFORM_ALIAS = {"c139": "yidong", "aliyun": "ali", "mobile": "yidong"}

# 候选集上限。FTS 命中几千条时不能全取回来算分，先按 bm25 截断。
# 注意 bm25 在 SQLite 里是"越负越相关"，排序方向别写反。
CANDIDATE_LIMIT = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id            INTEGER PRIMARY KEY,
    dedup_key     TEXT    NOT NULL UNIQUE,
    platform      TEXT    NOT NULL,
    pwd_id        TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    aliases       TEXT    NOT NULL DEFAULT '',
    url           TEXT    NOT NULL,
    pwd           TEXT    NOT NULL DEFAULT '',
    file_count    INTEGER NOT NULL DEFAULT 0,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    source_name   TEXT    NOT NULL DEFAULT '',
    source_level  INTEGER NOT NULL DEFAULT 3,
    check_state   TEXT    NOT NULL DEFAULT 'unknown',
    check_at      INTEGER NOT NULL DEFAULT 0,
    first_seen_at INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL,
    hit_count     INTEGER NOT NULL DEFAULT 0,
    click_count   INTEGER NOT NULL DEFAULT 0,
    revive_count  INTEGER NOT NULL DEFAULT 0,
    archived_at   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_res_platform ON resources(platform, check_state);
CREATE INDEX IF NOT EXISTS idx_res_check    ON resources(check_state, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_res_seen     ON resources(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_res_hot      ON resources(hit_count DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5(
    title, tk, tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS resource_hits (
    resource_id INTEGER NOT NULL,
    query       TEXT    NOT NULL,
    day         INTEGER NOT NULL,
    channel     TEXT    NOT NULL DEFAULT 'upstream',
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (resource_id, query, day, channel)
);
CREATE INDEX IF NOT EXISTS idx_hits_query ON resource_hits(query, day DESC);
"""


# ---------------------------------------------------------------- 初始化


# 命中来源。不是自由字段：写错一个值就等于凭空多一条统计口径，所以
# record_hits 里做白名单归一，只认这两个。
CHANNEL_UPSTREAM = "upstream"  # 上游结果带出来的（现在唯一在用的）
CHANNEL_LOCAL = "local"        # 本地库自己检索出来的（P1 起使用）


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _migrate(conn: sqlite3.Connection) -> None:
    """把老库升到当前结构。每次启动都跑，幂等，代价是一次 PRAGMA 查表。

    必须在 executescript(_SCHEMA) **之前**调用：建表语句全是 IF NOT EXISTS，
    先执行建表会让老结构的 resource_hits 蒙混过关，迁移从此再也不会发生。
    """
    if not _has_table(conn, "resource_hits"):
        return
    if _has_column(conn, "resource_hits", "channel"):
        return

    # 老库里的命中全部来自上游回填——那时本地检索还不存在，这是事实不是猜测。
    conn.execute("ALTER TABLE resource_hits RENAME TO resource_hits_old")
    conn.execute(
        """
        CREATE TABLE resource_hits (
            resource_id INTEGER NOT NULL,
            query       TEXT    NOT NULL,
            day         INTEGER NOT NULL,
            channel     TEXT    NOT NULL DEFAULT 'upstream',
            count       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (resource_id, query, day, channel)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO resource_hits(resource_id, query, day, channel, count)
        SELECT resource_id, query, day, 'upstream', count FROM resource_hits_old
        """
    )
    moved = int(conn.execute("SELECT COUNT(*) FROM resource_hits").fetchone()[0])
    # 索引跟着旧表走，DROP 之后索引才消失。所以 CREATE INDEX 必须排在 DROP 后面：
    # 反过来写，同名索引还在，IF NOT EXISTS 会静默跳过，新表就永远没有索引。
    conn.execute("DROP TABLE resource_hits_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hits_query ON resource_hits(query, day DESC)"
    )
    log.info("资源库迁移：resource_hits 加 channel 列，%d 行历史命中归为 upstream", moved)


def init(db_path: str | Path) -> None:
    """建库建表。服务启动时调一次；重复调用是幂等的。"""
    global _db_path, _inited
    _db_path = Path(db_path).expanduser()
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with _session() as conn:
        # WAL 是持久化设置，设一次就够
        conn.execute("PRAGMA journal_mode=WAL")
        _migrate(conn)
        conn.executescript(_SCHEMA)
    _inited = True


def is_ready() -> bool:
    return _inited


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("资源库未初始化，先调用 store.init()")
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    # NORMAL 在 WAL 下不会损坏库，最坏丢最后几个事务——回填丢几条无所谓，
    # 下次搜索补回来。别用 FULL，每条 commit 都 fsync 会把回填拖成瓶颈。
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # 默认页缓存只有 2MB，几万条资源就开始频繁读盘
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    return conn


@contextmanager
def _session():
    """sqlite3 的连接上下文只提交事务、不关连接，必须自己关。"""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- 切词


def _squash(text: str) -> str:
    """剥掉空白和标点，压成连续串。

    用 Unicode 类别判断而不是正则白名单：正则白名单会把日文假名、俄文一起
    删掉（番剧资源标题里真有），而标点剥离是通用需求。
    """
    out = []
    for ch in (text or "").casefold():
        if ch.isspace():
            continue
        if unicodedata.category(ch)[0] == "P":  # P* = 标点
            continue
        out.append(ch)
    return "".join(out)


def grams(text: str) -> str:
    """1-gram + 2-gram，空格分隔。不依赖分词词典，召回优先。

    先压成连续串再切：`三体 全集` 和 `三体全集` 必须产生同一批 2-gram
    （含 `体全`），否则两者互相搜不到。这是本文件最容易写错的一处。
    """
    s = _squash(text)
    if not s:
        return ""
    tokens = list(s)                                  # 全部单字
    tokens.extend(s[i : i + 2] for i in range(len(s) - 1))  # 全部双字
    return " ".join(dict.fromkeys(tokens))            # 保序去重


def match_expr(query: str) -> str:
    """查询串 → FTS5 MATCH 表达式。

    AND 连接：所有 gram 都出现才算命中，语义上就是"用户搜的每个字都在标题里"。
    每个 term 用双引号包住，避免 gram 里的特殊字符被当成 FTS 语法。
    """
    parts = grams(query).split()
    if not parts:
        return ""
    return " AND ".join(f'"{p}"' for p in parts)


def platform_key(raw: str) -> str:
    key = (raw or "").strip().lower()
    return _PLATFORM_ALIAS.get(key, key)


# ---------------------------------------------------------------- 写入


def _prepare(rows: list[dict], now: int) -> list[dict]:
    """把一批候选归一成可入库的行。同批内同 key 的合并成一条。"""
    merged: dict[str, dict] = {}

    for row in rows:
        url = (row.get("url") or row.get("shareUrl") or "").strip()
        title = (row.get("title") or "").strip()
        if not url or not title:
            continue

        parsed = parse_share(url)
        if not parsed.ok:
            # 详情页 / 不支持的站 / 识别不出分享 ID：不入库。
            # 这条门挡住的是"看着像资源、点开是介绍页"的那一类。
            continue

        platform = platform_key(row.get("platform") or parsed.platform)
        key = f"{platform}:{parsed.pwd_id}"
        pwd = (row.get("pwd") or row.get("sharePwd") or parsed.passcode or "").strip()
        file_count = _int(row.get("file_count") or row.get("fileCount"))
        size_bytes = _int(row.get("size_bytes") or row.get("sizeBytes"))
        level = _int(row.get("source_level") or row.get("sourceLevel"), default=3) or 3
        level = min(max(level, 1), 4)
        source_name = (row.get("source_name") or row.get("sourceName") or "").strip()

        cur = merged.get(key)
        if cur is None:
            merged[key] = {
                "dedup_key": key,
                "platform": platform,
                "pwd_id": parsed.pwd_id,
                "title": title,
                "aliases": [],
                "url": parsed.url or url,
                "pwd": pwd,
                "file_count": file_count,
                "size_bytes": size_bytes,
                "source_name": source_name,
                "source_level": level,
                "now": now,
            }
            continue

        # 同批撞车：标题留更长的（信息量更大），其余字段取非零，
        # 别名把另一个叫法收进去——同一资源的不同叫法都要能被搜到。
        if title != cur["title"] and title not in cur["aliases"]:
            if len(title) > len(cur["title"]):
                cur["aliases"].append(cur["title"])
                cur["title"] = title
            else:
                cur["aliases"].append(title)
        cur["pwd"] = cur["pwd"] or pwd
        cur["file_count"] = cur["file_count"] or file_count
        cur["size_bytes"] = cur["size_bytes"] or size_bytes
        cur["source_level"] = min(cur["source_level"], level)
        cur["source_name"] = cur["source_name"] or source_name

    return list(merged.values())


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def upsert_many(rows: list[dict], *, now: int | None = None) -> dict:
    """批量入库。返回 `{"added": n, "touched": n, "ids": {dedup_key: id}}`。

    已存在的行只更新"新鲜度"和"更全的字段"，**不覆盖标题**——标题被后来的
    帖子改写，越改越标题党，是资源库腐化最快的一条路径。别的叫法进 `aliases`。
    """
    if not rows:
        return {"added": 0, "touched": 0, "ids": {}}

    stamp = int(now if now is not None else time.time())
    prepared = _prepare(rows, stamp)
    if not prepared:
        return {"added": 0, "touched": 0, "ids": {}}

    added = 0
    touched = 0
    ids: dict[str, int] = {}

    with _write_lock, _session() as conn:
        # 先一次性查出已存在的，避免逐条 SELECT（回填一次几十条，逐条查很亏）
        keys = [p["dedup_key"] for p in prepared]
        existing: dict[str, sqlite3.Row] = {}
        for i in range(0, len(keys), 400):
            chunk = keys[i : i + 400]
            marks = ",".join("?" * len(chunk))
            sql = (
                "SELECT id, dedup_key, title, aliases FROM resources "
                f"WHERE dedup_key IN ({marks})"
            )
            for row in conn.execute(sql, chunk):
                existing[row["dedup_key"]] = row

        for p in prepared:
            hit = existing.get(p["dedup_key"])
            if hit is None:
                cur = conn.execute(
                    """
                    INSERT INTO resources
                      (dedup_key, platform, pwd_id, title, aliases, url, pwd,
                       file_count, size_bytes, source_name, source_level,
                       first_seen_at, last_seen_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        p["dedup_key"], p["platform"], p["pwd_id"], p["title"],
                        json.dumps(p["aliases"], ensure_ascii=False) if p["aliases"] else "",
                        p["url"], p["pwd"], p["file_count"], p["size_bytes"],
                        p["source_name"], p["source_level"], stamp, stamp,
                    ),
                )
                rid = int(cur.lastrowid)
                _fts_put(conn, rid, p["title"], p["aliases"])
                added += 1
                ids[p["dedup_key"]] = rid
                continue

            rid = int(hit["id"])
            ids[p["dedup_key"]] = rid
            touched += 1

            aliases = _load_aliases(hit["aliases"])
            dirty_title = False
            if p["title"] != hit["title"] and p["title"] not in aliases:
                if len(aliases) < 5:
                    aliases.append(p["title"])
                    dirty_title = True

            conn.execute(
                """
                UPDATE resources SET
                  last_seen_at = MAX(last_seen_at, ?),
                  pwd          = COALESCE(NULLIF(pwd, ''), ?),
                  file_count   = COALESCE(NULLIF(?, 0), file_count),
                  size_bytes   = COALESCE(NULLIF(?, 0), size_bytes),
                  source_level = MIN(source_level, ?),
                  source_name  = CASE WHEN source_name = '' THEN ? ELSE source_name END,
                  aliases      = ?
                WHERE id = ?
                """,
                (
                    p["now"], p["pwd"], p["file_count"], p["size_bytes"],
                    p["source_level"], p["source_name"],
                    json.dumps(aliases, ensure_ascii=False) if aliases else "",
                    rid,
                ),
            )
            if dirty_title:
                # FTS5 不支持部分列 UPDATE，只能删了重插。只在标题/别名真变了时做，
                # 否则每次回填都重建一遍索引，纯浪费。
                _fts_put(conn, rid, hit["title"], aliases)

    if added or touched:
        log.info("资源库入库：新增 %d，更新 %d（候选 %d，可入库 %d）",
                 added, touched, len(rows), len(prepared))
    return {"added": added, "touched": touched, "ids": ids}


def _load_aliases(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value if str(v).strip()] if isinstance(value, list) else []


def _fts_put(conn: sqlite3.Connection, rid: int, title: str, aliases: list[str]) -> None:
    conn.execute("DELETE FROM resources_fts WHERE rowid = ?", (rid,))
    # tk 里带上别名：用户搜任何一个叫法都得能命中同一条资源。
    # 命中率靠这个，不靠标题写得多准。
    conn.execute(
        "INSERT INTO resources_fts(rowid, title, tk) VALUES (?,?,?)",
        (rid, title, grams(" ".join([title, *aliases]))),
    )


def record_hits(
    resource_ids: list[int],
    query: str,
    *,
    channel: str = CHANNEL_UPSTREAM,
    now: int | None = None,
) -> None:
    """记"这次查询带出了这些资源"。按天聚合，不是每出一条写一行流水。

    一天 5000 次搜索 × 20 条 = 10 万行/天，逐条记一年就是 3600 万行垃圾。
    这里 (resource_id, query, day, channel) 做主键，count 累加，一行代表
    "这个词今天从这条通道带出过这条资源 N 次"。

    channel 必须分开算：一条资源被上游带来 50 次、被本地库带来 0 次，和
    反过来，是两种完全不同的状态——前者说明库还没用上，后者说明库在兜住
    上游给不了的东西。混在一个 count 里就再也分不出来了。
    """
    if not resource_ids or not query:
        return
    stamp = int(now if now is not None else time.time())
    q = _squash(query)[:100]
    if not q:
        return
    day = int(time.strftime("%Y%m%d", time.localtime(stamp)))
    # 白名单归一。传了别的值一律按上游算，宁可少一个口径，不要多一条脏数据。
    ch = CHANNEL_LOCAL if channel == CHANNEL_LOCAL else CHANNEL_UPSTREAM

    try:
        with _write_lock, _session() as conn:
            conn.executemany(
                "INSERT INTO resource_hits(resource_id, query, day, channel, count) "
                "VALUES (?,?,?,?,1) "
                "ON CONFLICT(resource_id, query, day, channel) DO UPDATE SET count = count + 1",
                [(rid, q, day, ch) for rid in resource_ids],
            )
            conn.executemany(
                "UPDATE resources SET hit_count = hit_count + 1 WHERE id = ?",
                [(rid,) for rid in resource_ids],
            )
    except sqlite3.Error as exc:
        # 热度记不上不影响搜索，但必须留痕：静默失败会让"库没热度"变成一个
        # 查不出原因的谜。
        log.warning("记录命中失败：%s", exc)


def mark_check(resource_id: int, state: str, *, now: int | None = None) -> None:
    """巡检回写单条结论。复活要单独计数——它比任何评分都准。"""
    stamp = int(now if now is not None else time.time())
    with _write_lock, _session() as conn:
        row = conn.execute(
            "SELECT check_state FROM resources WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            return
        revived = 1 if row["check_state"] == STATE_BAD and state == STATE_OK else 0
        conn.execute(
            "UPDATE resources SET check_state = ?, check_at = ?, "
            "revive_count = revive_count + ? WHERE id = ?",
            (state, stamp, revived, resource_id),
        )


# ---------------------------------------------------------------- 检索


def search(
    query: str,
    platforms: list[str] | None = None,
    page: int = 1,
    page_size: int = 20,
    *,
    now: int | None = None,
) -> dict:
    """本地检索。返回 `{"items", "total", "truncated"}`。

    两段：FTS 硬筛（相关性是门槛）→ 候选集内算分排序。顺序不能反。
    """
    expr = match_expr(query)
    if not expr:
        return {"items": [], "total": 0, "truncated": False}

    stamp = int(now if now is not None else time.time())
    page = max(1, _int(page, 1))
    page_size = min(max(1, _int(page_size, 20)), 50)
    limit = max(CANDIDATE_LIMIT, page * page_size * 3)

    sql = [
        "SELECT r.* FROM resources r JOIN (",
        "  SELECT rowid FROM resources_fts WHERE resources_fts MATCH ?",
        "  ORDER BY bm25(resources_fts) LIMIT ?",
        ") m ON m.rowid = r.id",
        "WHERE r.archived_at = 0 AND r.check_state NOT IN (%s)" % ",".join("?" * len(HIDDEN_STATES)),
    ]
    params: list = [expr, limit, *HIDDEN_STATES]

    wanted = [platform_key(p) for p in (platforms or []) if p]
    if wanted:
        sql.append("AND r.platform IN (%s)" % ",".join("?" * len(wanted)))
        params.extend(wanted)

    with _session() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()

    if not rows:
        return {"items": [], "total": 0, "truncated": False}

    scored = sorted(
        ((_score(row, stamp), row) for row in rows),
        key=lambda pair: (-pair[0], -int(pair[1]["id"])),  # id 兜底，保证翻页稳定
    )
    total = len(scored)
    start = (page - 1) * page_size
    page_rows = scored[start : start + page_size]

    return {
        "items": [_to_item(row) for _, row in page_rows],
        "total": total,
        # 候选被 bm25 截断过，total 是下界不是真实总数。出口要如实标注，
        # 别让前端把"1000+"当成"正好 1000"。
        "truncated": total >= limit,
    }


def _score(row: sqlite3.Row, now: int) -> float:
    """可解释的加法模型。每一项都能单独说清为什么加这些分。"""
    state = row["check_state"]
    if state == STATE_OK:
        score = float(W_STATE_OK)
    elif state == STATE_UNKNOWN:
        score = float(W_STATE_UNKNOWN)
    else:
        score = 0.0

    score += math.log1p(max(0, row["hit_count"])) * W_HIT
    score += math.log1p(max(0, row["click_count"])) * W_CLICK
    score += SOURCE_LEVEL_SCORE.get(_int(row["source_level"], 3), 0)
    score += _fresh_score(row["last_seen_at"], now)

    if row["pwd"]:
        score += W_PWD
    if _int(row["file_count"]):
        score += W_FILE_COUNT
    if _int(row["size_bytes"]):
        score += W_SIZE
    if _int(row["revive_count"]):
        score += 40  # 死而复生过，说明有人在持续分享它
    return score


def _fresh_score(last_seen: int, now: int) -> float:
    if not last_seen:
        return 0.0
    days = max(0.0, (now - last_seen) / 86400)
    for limit, score in FRESH_STEPS:
        if days <= limit:
            return float(score)
    return 0.0


def _to_item(row: sqlite3.Row) -> dict:
    """库内行 → 前端卡片字段。形状对齐 `search/adapter.py:to_item`，
    多两个只有库能提供的标记。"""
    platform = row["platform"]
    return {
        "hsid": "",
        "sourceKey": "lib:" + (row["source_name"] or "library"),
        "sourceKind": "library",
        "sourceName": row["source_name"] or "library",
        "platform": platform,
        "platformName": PLATFORM_NAMES.get(platform, "其他"),
        "title": row["title"],
        "shareUrl": row["url"],
        "shareCode": "",
        "sharePwd": row["pwd"],
        "fileCount": _int(row["file_count"]),
        "sizeBytes": _int(row["size_bytes"]),
        "indexedTime": "",
        "fromLibrary": True,
        "checkState": row["check_state"],
        "resourceId": int(row["id"]),
    }


# ---------------------------------------------------------------- 回填


def ingest(
    items: list[dict],
    query: str = "",
    *,
    channel: str = CHANNEL_UPSTREAM,
    now: int | None = None,
) -> dict:
    """影子回填：把一次搜索已经拿到的结果顺手落库。

    **绝不允许抛异常给调用方。** 这是纯旁路：回填失败最多是库不涨，
    搜索必须照常返回。所有错误在这里吞掉并留日志。
    """
    if not items:
        return {"added": 0, "touched": 0}

    try:
        rows = []
        for item in items:
            rows.append({
                "url": item.get("shareUrl") or "",
                "title": item.get("title") or "",
                "platform": item.get("platform") or "",
                "pwd": item.get("sharePwd") or "",
                "file_count": item.get("fileCount") or 0,
                "size_bytes": item.get("sizeBytes") or 0,
                "source_name": item.get("sourceName") or "",
                "source_level": 1 if item.get("sourceKind") == "tg" else 2,
            })

        result = upsert_many(rows, now=now)
        if query and result["ids"]:
            record_hits(
                list(result["ids"].values()), query, channel=channel, now=now
            )
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("影子回填失败（不影响搜索）：%s", exc, exc_info=True)
        return {"added": 0, "touched": 0}


# ---------------------------------------------------------------- 观测


def stats() -> dict:
    """给 /api/health 和管理端用。库不涨、某状态暴涨，都该在这里被发现。"""
    out: dict = {
        "total": 0,
        "by_state": {},
        "by_platform": {},
        "new_7d": 0,
        "hits_7d": 0,
        "hits_7d_by_channel": {},
    }
    if not _inited:
        return out
    now = int(time.time())
    with _session() as conn:
        out["total"] = int(conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0])
        for row in conn.execute(
            "SELECT check_state, COUNT(*) c FROM resources GROUP BY check_state"
        ):
            out["by_state"][row["check_state"]] = int(row["c"])
        for row in conn.execute(
            "SELECT platform, COUNT(*) c FROM resources GROUP BY platform ORDER BY c DESC LIMIT 20"
        ):
            out["by_platform"][row["platform"]] = int(row["c"])
        out["new_7d"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM resources WHERE first_seen_at >= ?", (now - 7 * 86400,)
            ).fetchone()[0]
        )
        # 一条 SQL 同时拿总和与分解，不查两遍。hits_7d 保留为总和是为了让
        # 现有消费方（health 看板）不改就能继续读；分解看 by_channel。
        since = int(time.strftime("%Y%m%d", time.localtime(now - 7 * 86400)))
        for row in conn.execute(
            "SELECT channel, COALESCE(SUM(count), 0) c FROM resource_hits "
            "WHERE day >= ? GROUP BY channel",
            (since,),
        ):
            out["hits_7d_by_channel"][row["channel"]] = int(row["c"])
        out["hits_7d"] = sum(out["hits_7d_by_channel"].values())
    return out


def get(resource_id: int) -> dict | None:
    """单条详情，给 P2 的资源详情页用。"""
    with _session() as conn:
        row = conn.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
    if row is None:
        return None
    item = _to_item(row)
    item["aliases"] = _load_aliases(row["aliases"])
    item["firstSeenAt"] = int(row["first_seen_at"])
    item["lastSeenAt"] = int(row["last_seen_at"])
    item["hitCount"] = int(row["hit_count"])
    item["clickCount"] = int(row["click_count"])
    item["reviveCount"] = int(row["revive_count"])
    return item
