"""点击时转存：把上游分享链接换成我们自己的分享链接。

定位（2026-11 决策）：不再人工补录关键词库，改为**用户点开时才动手**——
拿上游那条分享，用我们自己的网盘账号转存一份，再建一个我们名下的分享，
把用户送到那个地址。好处是链接不再外露、失效可控、来源可统计；
代价是三件事，代码里都做了硬约束，改之前先读这一节：

1. **容量**：转存永久占用我们账号的空间，且分享出去后不能删源文件
   （删了分享就断）。所以有 `max_bytes` 阈值，超了直接判失败走回退，
   宁可这一条给原链接，也不要把账号塞爆。
2. **频控**：网盘对连续转存有风控。所有转存走全局串行 + 最小间隔，
   同一源链接并发点击用单飞锁合并成一次。
3. **失效**：源分享过期、提取码错、被举报，都会导致转存失败。
   失败结果短缓存（默认 10 分钟），避免每个点击都去打一次接口。

映射落 `mirrors` 表（和埋点、反馈共用同一个 SQLite）：源链接 → 我们的链接。
一次成功永久复用，第二个人点开是 0 延迟——这是这套机制能扛住流量的关键。

安全：返回给前端的不是真实链接，而是 `make_token()` 生成的签名串。
不做签名就是开放重定向，别人能拿本站当钓鱼跳板。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import pan
from .sdk import registry, urls
from .sdk.errors import NetdiskError, UpstreamError

log = logging.getLogger("pantrace")

_db_path: Path | None = None
_inited = False

# ---- 配置（由 main.py 在启动时注入）----
# 只留这里真正会读的键。体积阈值、并发间隔这些已经搬进 pan（转存是它的事），
# 留在这儿会让人以为改了有用
_cfg: dict = {
    "enabled": True,
    "secret": "",
    "fail_ttl": 600,
}

# 单飞锁：同一个源链接被 N 个人同时点，只转一次，其余等结果
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()

# 只认网盘域名，挡掉任意 URL 被塞进 token（否则 /go 就是开放重定向）
_ALLOWED_HOST_RE = re.compile(
    r"(^|\.)("
    r"pan\.quark\.cn|drive\.uc\.cn|pan\.baidu\.com|"
    r"(alipan|aliyundrive)\.com|pan\.xunlei\.com|cloud\.189\.cn|"
    r"115\.com|(123pan|123684|123865)\.(com|cn)|(caiyun|yun)\.139\.com|"
    r"lanzou[a-z]*\.com|ilanzou\.com|share\.weiyun\.com|guangyapan\.com|"
    r"ctfile\.com|pan\.wo\.cn"
    r")$",
    re.IGNORECASE,
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_QUARK = "https://drive-pc.quark.cn/1/clouddrive"
_QUARK_TOKEN = f"{_QUARK}/share/sharepage/token?pr=ucpro&fr=pc"
_QUARK_DETAIL = f"{_QUARK}/share/sharepage/detail"
_QUARK_SAVE = f"{_QUARK}/share/sharepage/save?pr=ucpro&fr=pc"
_QUARK_TASK = f"{_QUARK}/task"
_QUARK_SHARE = f"{_QUARK}/share?pr=ucpro&fr=pc"
_FOREVER = 4  # expired_type=4 → 永久


def init(db_path: str | Path, **cfg) -> None:
    global _db_path, _inited
    _db_path = Path(db_path).expanduser()
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    for key, value in cfg.items():
        if key in _cfg:
            _cfg[key] = value
    _ensure_schema()
    _inited = True


def is_ready() -> bool:
    return _inited


def configure(**cfg) -> None:
    """运行期改配置（测试或换 cookie 时用，不用重启进程）。"""
    for key, value in cfg.items():
        if key in _cfg:
            _cfg[key] = value


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("resolver 未初始化，先调用 init()")
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


def session():
    """给业务层用的连接上下文。

    mirrors 表归这里管，pan.cleanup 要用同一个连接器把行删掉，
    所以暴露一个口子，而不是让它自己再拼一遍路径。
    """
    return _session()


def _ensure_schema() -> None:
    with _session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mirrors (
                src_key    TEXT PRIMARY KEY,
                src_url    TEXT NOT NULL,
                platform   TEXT NOT NULL DEFAULT '',
                our_url    TEXT NOT NULL DEFAULT '',
                status     TEXT NOT NULL DEFAULT '',   -- ok / fail
                reason     TEXT NOT NULL DEFAULT '',
                bytes      INTEGER NOT NULL DEFAULT 0,
                hits       INTEGER NOT NULL DEFAULT 0,
                account_id INTEGER NOT NULL DEFAULT 0,
                fids       TEXT NOT NULL DEFAULT '[]',
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mirrors_status ON mirrors(status);
            CREATE INDEX IF NOT EXISTS idx_mirrors_ts ON mirrors(updated_ts DESC);
            """
        )
        # 老库补列。SQLite 没有 ADD COLUMN IF NOT EXISTS，只能试一次吞异常
        for column, ddl in (
            ("account_id", "INTEGER NOT NULL DEFAULT 0"),
            ("fids", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            try:
                conn.execute(f"ALTER TABLE mirrors ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                pass


# ---- 源链接归一化 ----

def src_key(url: str) -> str:
    """同一个分享的稳定指纹。

    必须忽略提取码参数：同一份分享在不同帖子里带 ?pwd=abcd 或 ?pwd=2hBE，
    当作两条就会重复转存，白占一份容量。
    """
    try:
        parts = urlparse((url or "").strip())
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    query = parse_qs(parts.query)
    stable = {k: v for k, v in query.items() if k.lower() not in
              ("pwd", "passcode", "password", "code", "access_code")}
    flat = "&".join(f"{k}={sorted(v)[0]}" for k, v in sorted(stable.items()))
    raw = f"{host}{parts.path.rstrip('/')}?{flat}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _host_ok(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and bool(_ALLOWED_HOST_RE.search(host))


def _pwd_of(url: str, pwd: str = "") -> str:
    if pwd:
        return pwd.strip()[:20]
    try:
        for key in ("pwd", "passcode", "password", "code", "access_code"):
            vals = parse_qs(urlparse(url).query).get(key) or []
            if vals and vals[0]:
                return vals[0].strip()[:20]
    except ValueError:
        pass
    return ""


def with_pwd(url: str, pwd: str) -> str:
    """把提取码并进 URL（夸克/百度/迅雷/阿里打开即自动填充）。

    回退到原链接时也走它——否则用户拿到的链接没提取码，等于给了一半。
    """
    if not pwd:
        return url
    if _pwd_of(url):
        return url
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return url
    if not re.search(r"pan\.quark\.cn|pan\.baidu\.com|pan\.xunlei\.com|alipan|aliyundrive", host):
        return url
    return f"{url}{'&' if '?' in url else '?'}pwd={pwd}"


# ---- 签名 token ----

def _secret() -> bytes:
    raw = str(_cfg.get("secret") or "")
    if not raw:
        # 没配密钥时退化到进程内随机值：重启后旧链接失效（会回退），但不会更不安全
        raw = "pantrace-default-secret"
    return hashlib.sha256(raw.encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(url: str, pwd: str, platform: str, title: str = "") -> str:
    """源链接 → /go/ 后面的签名串。只放跳转必需的信息，不放任何凭据。"""
    payload = {
        "u": url[:500],
        "p": (pwd or "")[:20],
        "k": (platform or "")[:20],
        "t": (title or "")[:80],
        "ts": int(time.time()),
    }
    body = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:12])
    return f"{body}.{sig}"


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,900}\.[A-Za-z0-9_\-]{8,32}$")


def read_token(token: str) -> dict | None:
    """验签并解出源链接。任何异常都返回 None——这里不做容错猜测。"""
    token = (token or "").strip()
    if not _TOKEN_RE.match(token):
        return None
    body, sig = token.rsplit(".", 1)
    expect = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:12])
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:  # noqa: BLE001
        return None
    url = str(payload.get("u") or "")
    if not _host_ok(url):
        return None
    return {
        "url": url,
        "pwd": str(payload.get("p") or "")[:20],
        "platform": str(payload.get("k") or "")[:20],
        "title": str(payload.get("t") or "")[:80],
    }


# ---- 映射表读写 ----

def _get_row(key: str) -> dict | None:
    with _session() as conn:
        row = conn.execute("SELECT * FROM mirrors WHERE src_key=?", (key,)).fetchone()
    return dict(row) if row else None


def _bump_hit(key: str) -> None:
    try:
        with _session() as conn:
            conn.execute("UPDATE mirrors SET hits=hits+1 WHERE src_key=?", (key,))
    except sqlite3.Error:
        pass


def _store(key: str, src_url: str, platform: str, our_url: str,
           status: str, reason: str = "", size: int = 0,
           account_id: int = 0, fids: list[str] | None = None) -> None:
    """落映射。fids / account_id 是给清理任务留的：没有它们，回收空间时
    只能靠列目录去猜哪几个文件是我们的，代价大且容易删错。"""
    now = time.time()
    with _session() as conn:
        conn.execute(
            """INSERT INTO mirrors
               (src_key, src_url, platform, our_url, status, reason, bytes,
                hits, account_id, fids, created_ts, updated_ts)
               VALUES (?,?,?,?,?,?,?,0,?,?,?,?)
               ON CONFLICT(src_key) DO UPDATE SET
                 our_url=excluded.our_url, status=excluded.status,
                 reason=excluded.reason, bytes=excluded.bytes,
                 account_id=excluded.account_id, fids=excluded.fids,
                 updated_ts=excluded.updated_ts""",
            (key, src_url[:500], platform[:20], our_url[:500], status,
             reason[:200], int(size), int(account_id),
             json.dumps(fids or [], ensure_ascii=False), now, now),
        )


def _lock_for(key: str) -> threading.Lock:
    with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        # 表不会太大（一个源链接一把锁），但也不能无限长
        if len(_key_locks) > 4096:
            for k, l in list(_key_locks.items()):
                if not l.locked() and k != key:
                    _key_locks.pop(k, None)
        return lock


def _ms_to_ts(value) -> float | None:
    """夸克的时间戳是毫秒，0 / 空值一律当"不知道"。"""
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return None
    return num / 1000 if num > 10_000_000_000 else (num or None)


# ---- 对外主入口 ----

def mirror(url: str, pwd: str = "", platform: str = "", title: str = "", force: bool = False) -> dict:
    """源链接 → 我们的分享链接。

    返回 {"ok": bool, "url": str, "reason": str, "cached": bool}。
    永不抛异常：调用方（/go 跳转）必须能拿到一个可用的去向。

    这里只管三件事：查映射、单飞、落库。真正的转存在 `pan.transfer`——
    挑账号、换账号、各网盘的七步流程都在那边，这块不再认识任何网盘。

    force：跳过**失败**缓存。后台手工验链走这条，否则改完账号配置再验，
    拿到的还是上次那句缓存下来的失败原因，验了个寂寞。成功映射不跳过——
    已经有能用的链接，再转一次只会在临时目录里留一份孤儿文件。
    """
    if not _inited or not _cfg.get("enabled"):
        return {"ok": False, "url": "", "reason": "转存未启用", "cached": False}
    if not _host_ok(url):
        return {"ok": False, "url": "", "reason": "链接不是受支持的网盘", "cached": False}
    key = src_key(url)
    if not key:
        return {"ok": False, "url": "", "reason": "链接无法识别", "cached": False}

    row = _get_row(key)
    if row and row["status"] == "ok" and row["our_url"]:
        _bump_hit(key)
        return {"ok": True, "url": row["our_url"], "reason": "", "cached": True}
    if row and row["status"] == "fail" and not force and time.time() - row["updated_ts"] < float(_cfg["fail_ttl"]):
        return {"ok": False, "url": "", "reason": row["reason"] or "转存失败", "cached": True}

    platform = platform or urls.platform_of(url)
    if not registry.supported(platform):
        return {"ok": False, "url": "", "reason": "该平台暂不支持转存", "cached": False}

    lock = _lock_for(key)
    with lock:
        # 拿到锁后再查一次：等锁期间可能已经被别人转好了
        row = _get_row(key)
        if row and row["status"] == "ok" and row["our_url"]:
            _bump_hit(key)
            return {"ok": True, "url": row["our_url"], "reason": "", "cached": True}
        if row and row["status"] == "fail" and not force and time.time() - row["updated_ts"] < float(_cfg["fail_ttl"]):
            return {"ok": False, "url": "", "reason": row["reason"] or "转存失败", "cached": True}

        try:
            link = pan.transfer(url, _pwd_of(url, pwd), platform, title)
            our_url = link.url
            if not our_url:
                raise UpstreamError("转存没返回可用链接")
        except NetdiskError as exc:
            reason = str(exc) or "转存失败"
            _store(key, url, platform, "", "fail", reason)
            return {"ok": False, "url": "", "reason": reason, "cached": False}
        except Exception as exc:  # noqa: BLE001
            msg = f"转存异常：{exc.__class__.__name__}"
            _store(key, url, platform, "", "fail", msg)
            log.exception("转存未捕获异常：%s", url[:120])
            return {"ok": False, "url": "", "reason": msg, "cached": False}

        _store(
            key, url, platform, our_url, "ok", link.note, link.size,
            account_id=_account_id_of(link), fids=link.fids,
        )
        return {"ok": True, "url": our_url, "reason": link.note, "cached": False}


def _account_id_of(link) -> int:
    """转存用的账号 id 藏在 note 的 `account=N` 里（由 pan.transfer 追加）。

    单独开一个字段更干净，但 ShareLink 要跨层传一个业务概念，得不偿失。
    """
    import re as _re

    match = _re.search(r"account=(\d+)", link.note or "")
    return int(match.group(1)) if match else 0


def _platform_of(url: str) -> str:
    return urls.platform_of(url)


def stats() -> dict:
    """看板 / 排障用的小数字。"""
    if not _inited:
        return {"ok": 0, "fail": 0, "hits": 0, "bytes": 0, "recent": []}
    with _session() as conn:
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok,
                 SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END) AS fail,
                 SUM(hits) AS hits, SUM(bytes) AS bytes
               FROM mirrors"""
        ).fetchone()
        recent = conn.execute(
            """SELECT src_url, our_url, platform, status, reason, bytes, hits, updated_ts
               FROM mirrors ORDER BY updated_ts DESC LIMIT 30"""
        ).fetchall()
    return {
        "ok": int(row["ok"] or 0),
        "fail": int(row["fail"] or 0),
        "hits": int(row["hits"] or 0),
        "bytes": int(row["bytes"] or 0),
        "recent": [dict(r) for r in recent],
    }


def purge_failed(older_than_days: int = 7) -> int:
    """清掉过期失败记录，让下次点击重新尝试（cookie 修好后不必等）。"""
    if not _inited:
        return 0
    cutoff = time.time() - max(1, older_than_days) * 86400
    with _session() as conn:
        cur = conn.execute(
            "DELETE FROM mirrors WHERE status='fail' AND updated_ts < ?", (cutoff,)
        )
    return int(cur.rowcount or 0)
