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

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


def plan(days: int = 30, page: int = 1, page_size: int = 20) -> dict:
    """上架清单：近 N 天被搜得最多的词，已上架的自动剔除。

    排序按**去重人数 u**（多少人搜过），不是动作次数：
    一个人刷 20 次不代表需求大，8 个人各搜一次才是真需求。
    次数 n 只做同分排序；u=0（老数据没带访客号）时退回按次数排。

    返回 total / totalPages，前端按页取；分页在排序之后做，翻页不会串序。
    """
    if not _inited:
        return {
            "days": days, "items": [], "covered": 0,
            "total": 0, "page": 1, "pageSize": page_size, "totalPages": 0,
        }

    days = max(1, min(365, int(days or 30)))
    page_size = max(1, min(100, int(page_size or 20)))
    page = max(1, int(page or 1))
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
        b["u"] = max(b["u"], int(r["u"] or 0))  # 同词不同写法取并集上限
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

    # 人数优先，次数同分；整库都没有访客号时退回按次数排
    has_uv = any(i["u"] for i in items)
    if has_uv:
        items.sort(key=lambda x: (-x["u"], -x["n"], x["q"]))
    else:
        items.sort(key=lambda x: (-x["n"], -x["last"], x["q"]))

    total = len(items)
    total_pages = (total + page_size - 1) // page_size
    start_idx = (page - 1) * page_size
    return {
        "days": days,
        "items": items[start_idx:start_idx + page_size],
        "covered": covered,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
    }


# ---- 补录解析：贴一段分享文案就够 ----
#
# 运营实测的痛点：一条链接要填主关键词、别名、标题、平台、链接、提取码、
# 文件数、大小、备注共 9 个格子，其中 7 个是可以从链接本身推出来的。
# 这里只做两件事：把分享文案拆成条目（纯本地正则，不联网），
# 再对夸克补一次元信息（标题/到期/文件数，公开接口，失败就当没有）。

_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.IGNORECASE)

# 域名 → 平台 key，一律小写匹配；顺序即优先级
_HOST_RULES: tuple[tuple[str, str], ...] = (
    (r"(^|\.)pan\.quark\.cn$", "quark"),
    (r"(^|\.)drive\.uc\.cn$", "uc"),
    (r"(^|\.)pan\.baidu\.com$", "baidu"),
    (r"(^|\.)(alipan|aliyundrive)\.com$", "ali"),
    (r"(^|\.)pan\.xunlei\.com$", "xunlei"),
    (r"(^|\.)(h5\.)?cloud\.189\.cn$", "tianyi"),
    (r"(^|\.)115\.com$", "115"),
    (r"(^|\.)(123pan|123684|123865)\.(com|cn)$", "123"),
    (r"(^|\.)(caiyun|yun)\.139\.com$", "yidong"),
    (r"(^|\.)ilanzou\.com$", "lanzou"),
    (r"(^|\.)lanzou[a-z]*\.com$", "lanzou"),
    (r"(^|\.)share\.weiyun\.com$", "weiyun"),
    (r"(^|\.)guangyapan\.com$", "guangya"),
    (r"(^|\.)ctfile\.com$", "ctfile"),
    (r"(^|\.)pan\.wo\.cn$", "wo"),
)

# 文案里的提取码：提取码/访问码/密码/口令，后面允许缺分隔符（"pwd2hBE" 实测存在）
_PWD_RE = re.compile(
    r"(?:提取码|抽取码|访问码|提取密码|密码|口令|pwd|passcode)\s*[:：=]?\s*([A-Za-z0-9]{3,8})",
    re.IGNORECASE,
)
# 分享标题：书名号、直角引号、半角引号都收（微信里半角 ｢｣ 很常见）
_TITLE_RE = re.compile(r"[「『《“\"｢]([^」』》”\"｢｣]{2,120})[」』》”\"｣]")

# 标题转关键词时要剥掉的噪音，越靠前越先剥
_NOISE_RE = re.compile(
    r"(4K|8K|1080[PI]|720[PI]|2160P|HDR|蓝光|高清|超清|标清|原盘|重制版?|"
    r"国语|粤语|日语|中字|双语|中英字幕|内嵌字幕|字幕版?|"
    r"未删减|无删减|完整版|修正版|修复版|"
    r"全\s*\d+\s*季|\d+\s*-\s*\d+\s*季|第[一二三四五六七八九十百\d]+\s*季|"
    r"全集|合集|完结|连载中|更新至[^\s,，。]*|"
    r"BT|磁力|种子|迅雷下载|网盘资源|资源整理|资源|整理版?|珍藏版?|收藏版?|"
    r"夸克|百度网盘|迅雷|阿里云盘|UC|123云盘|蓝奏云|"
    r"MP4|MKV|AVI|FLAC|APE|PDF|EPUB|MOBI|AZW3|TXT|ZIP|RAR|7Z)",
    re.IGNORECASE,
)
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{2,5}$")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_QUARK_TOKEN = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc"
_QUARK_DETAIL = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail"
# 永久分享的 expired_at 落在 2100 年，用它当天花板判断"永久"
_FOREVER_TS = 4102416000.0


def _platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for pattern, key in _HOST_RULES:
        if re.search(pattern, host):
            return key
    return ""


def _trim_url(url: str) -> str:
    """去掉粘贴时常带的尾巴标点（句号、逗号、右括号）。"""
    return url.rstrip(".,;:!?，。；：！？、)")


def _pwd_from_url(url: str) -> str:
    try:
        q = parse_qs(urlparse(url).query)
    except ValueError:
        return ""
    for key in ("pwd", "passcode", "password", "code", "access_code"):
        vals = q.get(key) or []
        if vals and vals[0]:
            return vals[0].strip()[:20]
    return ""


def _with_pwd(url: str, pwd: str) -> str:
    """把提取码并进 URL（夸克/百度/迅雷/阿里打开即自动填充）。

    顺手修一个实测的脏数据：有些导出把 `?pwd=abcd` 写成了 `?pwdabcd`，
    等号丢了，这个参数是废的，留着只会让卡片上的链接看着像坏的。
    """
    if not pwd:
        return url
    cleaned = re.sub(r"([?&])pwd[^=&\s]{1,8}(?=&|$)", r"\1", url)
    cleaned = cleaned.replace("?&", "?").replace("&&", "&").rstrip("?&")
    if _pwd_from_url(cleaned):
        return cleaned
    key = _platform_of(cleaned)
    if key in {"quark", "baidu", "xunlei", "ali"}:
        return f"{cleaned}{'&' if '?' in cleaned else '?'}pwd={pwd}"
    return cleaned


def suggest_keyword(title: str) -> str:
    """从分享标题推一个能用的主关键词。

    匹配靠「归一化 + 双向包含」，关键词偏长还能靠 `query in term` 接住短词，
    所以这里的目标是"干净"而不是"短"：剥掉季数/清晰度/格式/网盘名这些
    人人会变的装饰，剩下的核心词才是用户真正会搜的东西。
    """
    text = (title or "").strip()
    if not text:
        return ""
    text = _TITLE_RE.sub(lambda m: m.group(1), text)
    text = re.sub(r"[\[【（(][^\[\]【】（）()]{1,30}[\]】）)]", " ", text)
    text = _EXT_RE.sub("", text)
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"[_/\\|~·—\-+]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-:：|")
    if len(text) < 2:
        # 剥完啥都不剩（标题本身就是"全集"这类词）就退回原标题
        fallback = re.sub(r"\s+", " ", (title or "").strip())
        return fallback[:40]
    return text[:40]


def _quark_meta(url: str, passcode: str, timeout: float = 6.0) -> dict:
    """夸克分享元信息：标题、到期时间、顶层文件数与大小。

    公开接口，不需要登录/签名，实测直接可用。任何失败都返回 error 字段，
    调用方照常保存——元信息是加分项，不该拦住补录。
    """
    pwd_id = ""
    path = urlparse(url).path or ""
    m = re.search(r"/s/([A-Za-z0-9]+)", path)
    if m:
        pwd_id = m.group(1)
    if not pwd_id:
        return {"error": "链接里没找到分享 id"}

    try:
        import requests
    except ImportError:  # pragma: no cover - 依赖缺失时静默降级
        return {"error": "requests 未安装"}

    session = requests.Session()
    session.trust_env = False  # 和 providers 一致：不走系统代理
    session.headers.update({"User-Agent": _UA, "Referer": "https://pan.quark.cn/"})
    try:
        resp = session.post(
            _QUARK_TOKEN,
            json={"pwd_id": pwd_id, "passcode": passcode or ""},
            timeout=timeout,
        )
        tok = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"夸克接口不可达：{exc}"}

    if tok.get("code") == 41008:
        return {"needPwd": True, "error": "这条分享需要提取码"}
    if tok.get("code") == 41019:
        return {"expired": True, "error": "分享已过期"}
    if tok.get("code") != 0:
        return {"error": str(tok.get("message") or "夸克接口返回异常")}

    data = tok.get("data") or {}
    out = {
        "title": str(data.get("title") or "")[:200],
        "expiredAt": (float(data.get("expired_at") or 0) / 1000) or None,
    }
    stoken = data.get("stoken")
    if not stoken:
        return out

    try:
        detail = session.get(
            _QUARK_DETAIL,
            params={
                "pr": "ucpro", "fr": "pc", "pwd_id": pwd_id, "stoken": stoken,
                "passcode": passcode or "", "force": 0, "offset": 0, "size": 50,
            },
            timeout=timeout,
        ).json()
    except Exception:  # noqa: BLE001
        return out

    entries = ((detail.get("data") or {}).get("list")) or []
    files = [e for e in entries if not e.get("dir")]
    # 只有顶层全是文件时数字才算得准；含文件夹时留空，别在卡片上写错数
    if files and len(files) == len(entries):
        out["fileCount"] = len(files)
        out["sizeBytes"] = sum(int(e.get("size") or 0) for e in files)
    return out


def _blocks_of(text: str) -> list[tuple[int, int]]:
    """按空行切块，返回字符区间。

    分享文案天然一块一条（标题 / 链接 / 提取码三行），空行是最好的分界线；
    一坨没有空行的多条（论坛里那种连续列表）在块内再按"离哪个链接近"分。
    """
    bounds = [0]
    for m in re.finditer(r"\n\s*\n", text):
        bounds.append(m.start())
        bounds.append(m.end())
    bounds.append(len(text))
    return list(zip(bounds[0::2], bounds[1::2]))


def parse_share(text: str, keyword_hint: str = "", with_meta: bool = True) -> list[dict]:
    """把粘贴的一段分享文案拆成若干条待录链接。

    归属规则：块内只有一个链接，块里的标题和提取码全归它；块内多个链接时，
    提取码归它**前面最近**的那个链接（分享文案一律是"链接 → 提取码"的顺序），
    标题取离它最近的一个。
    """
    raw_text = (text or "").strip()
    if not raw_text:
        return []

    hits = [(m.start(), _trim_url(m.group(0))) for m in _URL_RE.finditer(raw_text)]
    if not hits:
        return []

    def _spans(regex: re.Pattern) -> list[tuple[int, int, str]]:
        return [(m.start(), m.end(), m.group(1).strip()) for m in regex.finditer(raw_text)]

    pwd_spans = _spans(_PWD_RE)
    title_spans = _spans(_TITLE_RE)
    used_pwd: set[int] = set()
    used_title: set[int] = set()
    blocks = _blocks_of(raw_text)

    def _pick(spans, used: set[int], lo: int, hi: int, pos: int, prefer_left: bool) -> str:
        best, best_dist, best_idx = "", None, None
        for i, (s, e, val) in enumerate(spans):
            if i in used or s < lo or e > hi or not val:
                continue
            if prefer_left and e <= pos:  # 提取码只认链接之后的
                continue
            dist = abs((s + e) // 2 - pos)
            if best_dist is None or dist < best_dist:
                best, best_dist, best_idx = val, dist, i
        if best_idx is not None:
            used.add(best_idx)
        return best

    results: list[dict] = []
    seen: set[str] = set()
    for idx, (pos, url) in enumerate(hits):
        if url in seen:
            continue
        seen.add(url)

        lo, hi = next((b for b in blocks if b[0] <= pos <= b[1]), (0, len(raw_text)))
        in_block = [h for h in hits if lo <= h[0] <= hi]
        multi = len(in_block) > 1
        if multi:
            # 块内多条：把区间收窄到"上一条链接之后 / 下一条链接之前"
            prev = max((h[0] for h in in_block if h[0] < pos), default=lo)
            nxt = min((h[0] for h in in_block if h[0] > pos), default=hi)
            lo, hi = prev, nxt

        pwd = _pwd_from_url(url) or _pick(pwd_spans, used_pwd, pos, hi, pos, True)
        title = _pick(title_spans, used_title, lo, hi, pos, False)

        platform = _platform_of(url)
        item = {
            "platform": platform,
            "platformName": _PLATFORMS.get(platform, platform or "未知平台"),
            "url": url,
            "pwd": pwd,
            "title": title,
            "keyword": "",
            "fileCount": 0,
            "sizeBytes": 0,
            "expiredAt": None,
            "error": "",
            "needPwd": False,
            "dupId": 0,
        }

        if platform == "quark" and with_meta:
            meta = _quark_meta(url, pwd)
            if meta.get("title"):
                item["title"] = meta["title"]
            if meta.get("expiredAt"):
                item["expiredAt"] = meta["expiredAt"]
            if meta.get("fileCount"):
                item["fileCount"] = int(meta["fileCount"])
                item["sizeBytes"] = int(meta.get("sizeBytes") or 0)
            if meta.get("needPwd"):
                item["needPwd"] = True
            item["error"] = str(meta.get("error") or "")

        item["url"] = _with_pwd(url, pwd)
        item["keyword"] = suggest_keyword(item["title"]) or (
            keyword_hint.strip()[:60] if keyword_hint else ""
        )
        if not item["platform"]:
            item["error"] = item["error"] or "认不出这是哪个网盘，请手动选平台"
        results.append(item)

    # 单条补录时，清单里那个词比从标题推的准——那是用户真正搜过的词
    if len(results) == 1 and keyword_hint.strip():
        results[0]["keyword"] = keyword_hint.strip()[:60]

    existing = {r["share_url"]: r["id"] for r in list_links() if r.get("share_url")}
    for it in results:
        it["dupId"] = int(existing.get(it["url"]) or 0)
    return results


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


def save_many(items: list[dict]) -> dict:
    """批量补录：逐条独立校验，一条坏不连坐，返回成功项和失败下标。"""
    saved: list[dict] = []
    errors: list[dict] = []
    for idx, item in enumerate(items):
        try:
            saved.append(save(item))
        except ValueError as exc:
            errors.append({"index": idx, "msg": str(exc)})
    return {"saved": saved, "errors": errors}


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
