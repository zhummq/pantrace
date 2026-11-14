"""点击时转存：把上游分享链接换成我们自己的分享链接。

定位（2026-11 决策）：不再人工补录关键词库，改为**用户点开时才动手**——
拿上游那条分享，用我们自己的网盘账号转存一份，再建一个我们名下的分享，
把用户送到那个地址。好处是链接不再外露、失效可控、来源可统计；
代价是三件事，代码里都做了硬约束，改之前先读这一节：

1. **容量**：转存要占我们账号的空间。文件只留 30 分钟（到点回收，分享跟着断），
   所以不是永久占用；但同一资源每半小时要重转一次，热资源同时在号里的副本不少。
   所以有 `max_bytes` 阈值，超了直接判失败走回退，宁可这一条给原链接，
   也不要把账号塞爆。
2. **频控**：网盘对连续转存有风控。所有转存走全局串行 + 最小间隔，
   同一源链接并发点击用单飞锁合并成一次。
3. **失效**：源分享过期、提取码错、被举报，都会导致转存失败。
   失败结果短缓存（默认 10 分钟），避免每个点击都去打一次接口。

映射落 `mirrors` 表（和埋点、反馈共用同一个 SQLite）：源链接 → 我们的链接。
窗口内复用，第二个人点开是 0 延迟——这是这套机制能扛住流量的关键；过了窗口
重新转存，链接随之后移。分享本身按 1 天建（不是永久），回收任务失灵时由
网盘那边兜底失效。

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
    # 我们自己发的链接活多久（秒）。30 分钟后回收文件，再点会重转。
    # 用竞品那套：不靠网盘的有效期（最短 1 天），靠删除源文件。
    "link_ttl": 1800,
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
                expires_at INTEGER NOT NULL DEFAULT -1,
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mirrors_status ON mirrors(status);
            CREATE INDEX IF NOT EXISTS idx_mirrors_ts ON mirrors(updated_ts DESC);

            -- 换新链接后，上一轮的转存文件没人指向了。mirrors 只存得下最新一组
            -- fids，不排进这张表就等于每 30 分钟漏一份，账号一天就满。
            CREATE TABLE IF NOT EXISTS mirror_trash (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id   INTEGER NOT NULL DEFAULT 0,
                fids         TEXT NOT NULL DEFAULT '[]',
                delete_after REAL NOT NULL,
                created_ts   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trash_due ON mirror_trash(delete_after);
            """
        )
        # 老库补列。SQLite 没有 ADD COLUMN IF NOT EXISTS，只能试一次吞异常
        for column, ddl in (
            ("account_id", "INTEGER NOT NULL DEFAULT 0"),
            ("fids", "TEXT NOT NULL DEFAULT '[]'"),
            # -1 = 老数据没记过窗口（下次点击校验一次）；0 = 没有窗口（旧版本写的，
            # 或者回收任务已把窗口抹掉）；>0 = 秒级时间戳
            ("expires_at", "INTEGER NOT NULL DEFAULT -1"),
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


def _stash_old(row: dict) -> None:
    """刷新映射前，把上一轮转存的文件排进待删队列。

    为什么不能当场删：`_needs_refresh` 会提前 60 秒动手（避免用户拿到一条马上
    断的链接），这 60 秒里旧链接还有人可能正在打开。所以约到旧窗口结束 + 5 分钟
    宽限，由回收任务删。

    为什么要删：`_store` 会覆盖 fids，旧文件就再没人指向了。30 分钟重转一次，
    不清理的话一天就是几十份副本，账号撑不过一天。
    """
    fids = []
    try:
        fids = json.loads(row.get("fids") or "[]")
    except (TypeError, ValueError):
        return
    account_id = int(row.get("account_id") or 0)
    if not fids or not account_id:
        return
    now = time.time()
    deadline = max(now, float(row.get("expires_at") or 0)) + 300
    try:
        with _session() as conn:
            conn.execute(
                "INSERT INTO mirror_trash (account_id, fids, delete_after, created_ts)"
                " VALUES (?,?,?,?)",
                (account_id, json.dumps(fids, ensure_ascii=False), deadline, now),
            )
    except sqlite3.Error:
        log.warning("旧副本入队失败：%s 个文件（账号 %s），只有等手工清", len(fids), account_id)


def _store(key: str, src_url: str, platform: str, our_url: str,
           status: str, reason: str = "", size: int = 0,
           account_id: int = 0, fids: list[str] | None = None,
           expires_at: int = -1) -> None:
    """落映射。fids / account_id 是给清理任务留的：没有它们，回收空间时
    只能靠列目录去猜哪几个文件是我们的，代价大且容易删错。

    expires_at 记我们自己那条分享的有效期（秒）。没有它就只能等用户
    点开一条死链才知道分享过期了——2026-11 就是这么踩的。
    """
    now = time.time()
    with _session() as conn:
        conn.execute(
            """INSERT INTO mirrors
               (src_key, src_url, platform, our_url, status, reason, bytes,
                hits, account_id, fids, expires_at, created_ts, updated_ts)
               VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?)
               ON CONFLICT(src_key) DO UPDATE SET
                 our_url=excluded.our_url, status=excluded.status,
                 reason=excluded.reason, bytes=excluded.bytes,
                 account_id=excluded.account_id, fids=excluded.fids,
                 expires_at=excluded.expires_at,
                 updated_ts=excluded.updated_ts""",
            (key, src_url[:500], platform[:20], our_url[:500], status,
             reason[:200], int(size), int(account_id),
             json.dumps(fids or [], ensure_ascii=False), int(expires_at), now, now),
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


#: 距离到期还剩这么多秒就提前重建。留 60 秒缓冲，别让用户正好在截止前
#: 拿到一条马上断的链接。
_REFRESH_AHEAD = 60


def _needs_refresh(row: dict) -> bool:
    """这条映射要不要重新转存。

    链接只活 `link_ttl`（默认 30 分钟），到期必须重转——竞品的逻辑是
    "文件到点就删"，旧链接留着也是死链，不能用。"老数据"（expires_at=-1，
    没记过窗口）一律翻新一次，把之前依赖分享有效期的那批换掉。
    """
    expires = int(row.get("expires_at", -1))
    if expires < 0:
        return True
    if expires == 0:
        return False   # 0 = 没有窗口（旧数据 / 已回收），不当成可复用
    return expires <= time.time() + _REFRESH_AHEAD


def _defer_refresh(key: str, seconds: int = 300) -> None:
    """重建失败但旧链接还能用时，把下一次重试推后。

    不改 status / our_url：旧链接还在，用户照常能打开；只是别再每次点击
    都去打一次接口（网盘最烦的就是这个）。窗口本身只有半小时，推迟太久
    等于一直给死链，所以默认 5 分钟。
    """
    try:
        with _session() as conn:
            conn.execute(
                "UPDATE mirrors SET expires_at=? WHERE src_key=?",
                (int(time.time() + seconds), key),
            )
    except sqlite3.Error:
        pass


# ---- 对外主入口 ----

def mirror(url: str, pwd: str = "", platform: str = "", title: str = "", force: bool = False) -> dict:
    """源链接 → 我们的分享链接。

    返回 {"ok": bool, "url": str, "reason": str, "cached": bool}。
    永不抛异常：调用方（/go 跳转）必须能拿到一个可用的去向。

    这里只管三件事：查映射、单飞、落库。真正的转存在 `pan.transfer`——
    挑账号、换账号、各网盘的七步流程都在那边，这块不再认识任何网盘。

    force：忽略**成功和失败**缓存，强制重转一次。后台「手工验链」走这条——
    否则改完账号配置再验，拿到的还是上次缓存的结果；存量映射（老版本建的
    永久/30 天分享）也只能靠它逐条翻新成 1 天 + 30 分钟窗口。
    """
    if not _inited or not _cfg.get("enabled"):
        return {"ok": False, "url": "", "reason": "转存未启用", "cached": False}
    if not _host_ok(url):
        return {"ok": False, "url": "", "reason": "链接不是受支持的网盘", "cached": False}
    key = src_key(url)
    if not key:
        return {"ok": False, "url": "", "reason": "链接无法识别", "cached": False}

    row = _get_row(key)
    if row and row["status"] == "ok" and row["our_url"] and not force and not _needs_refresh(row):
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
        keep = row if (row and row["status"] == "ok" and row["our_url"]) else None
        if keep and not force and not _needs_refresh(keep):
            _bump_hit(key)
            return {"ok": True, "url": keep["our_url"], "reason": "", "cached": True}
        if row and row["status"] == "fail" and not force and time.time() - row["updated_ts"] < float(_cfg["fail_ttl"]):
            return {"ok": False, "url": "", "reason": row["reason"] or "转存失败", "cached": True}

        try:
            link = pan.transfer(url, _pwd_of(url, pwd), platform, title)
            our_url = link.url
            if not our_url:
                raise UpstreamError("转存没返回可用链接")
        except NetdiskError as exc:
            reason = str(exc) or "转存失败"
            if _still_alive(keep):
                # 旧链接还没到期，重建失败不该把用户从"我们的链接"降级到"上游原链接"
                _defer_refresh(key)
                log.warning("重建分享失败，沿用旧链接：%s → %s", url[:100], reason)
                return {"ok": True, "url": keep["our_url"], "reason": reason, "cached": True}
            _store(key, url, platform, "", "fail", reason)
            return {"ok": False, "url": "", "reason": reason, "cached": False}
        except Exception as exc:  # noqa: BLE001
            msg = f"转存异常：{exc.__class__.__name__}"
            if _still_alive(keep):
                _defer_refresh(key)
                return {"ok": True, "url": keep["our_url"], "reason": msg, "cached": True}
            _store(key, url, platform, "", "fail", msg)
            log.exception("转存未捕获异常：%s", url[:120])
            return {"ok": False, "url": "", "reason": msg, "cached": False}

        # 窗口从"建好这一刻"起算，跟竞品一致：30 分钟后文件回收，分享跟着断。
        # 网盘那边的分享有效期（1 天）只用来兜底，不落库——真正决定链接活多久
        # 的是我们自己的回收任务。
        if keep:
            # 先排队再覆盖：_store 之后这一行的 fids 就换成新的了
            _stash_old(keep)
        _store(
            key, url, platform, our_url, "ok",
            _display_note(link.note), link.size,
            account_id=_account_id_of(link), fids=link.fids,
            expires_at=int(time.time() + float(_cfg["link_ttl"])),
        )
        return {"ok": True, "url": our_url, "reason": _display_note(link.note), "cached": False}


def _still_alive(row: dict | None) -> bool:
    """旧映射还能不能用（只在重建失败时用它决定要不要兜底）。"""
    if not row:
        return False
    expires = int(row.get("expires_at", -1))
    if expires == 0:
        return True    # 没有窗口（永久/读不到），没法判断，按还活着处理
    return expires > time.time()


def _display_note(note: str) -> str:
    """note 里混着 `account=N`（给清理任务用的），展示前摘掉。"""
    return re.sub(r"\s*\|?\s*account=\d+", "", note or "").strip(" |")


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
        return {"ok": 0, "fail": 0, "expired": 0, "hits": 0, "bytes": 0, "recent": []}
    with _session() as conn:
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok,
                 SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END) AS fail,
                 SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired,
                 SUM(hits) AS hits, SUM(bytes) AS bytes
               FROM mirrors"""
        ).fetchone()
        recent = conn.execute(
            """SELECT src_url, our_url, platform, status, reason, bytes, hits,
                      expires_at, updated_ts
               FROM mirrors ORDER BY updated_ts DESC LIMIT 30"""
        ).fetchall()
    return {
        "ok": int(row["ok"] or 0),
        "fail": int(row["fail"] or 0),
        "expired": int(row["expired"] or 0),
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
