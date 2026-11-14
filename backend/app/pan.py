"""业务层：多账号、转存流水线、空间回收。

SDK 只负责"和一个网盘说话"，这里负责"用哪个账号说、失败了怎么办、空间怎么还"。

和竞品的三处关键差异（都是有意的）：

1. **失败会换账号**。它不是——随机挑一个，那个账号 cookie 过期就直接给用户
   报错。我们按 fail_count 升序挑，遇到 AuthError / QuotaExceeded 换下一个，
   最多试 3 个。用户不该为我们的账号问题买单。
2. **不搞 30 分钟定时删文件**。它删，是因为它的缓存只活 25 分钟；我们的
   mirrors 是永久映射，删文件等于把链接打死。这里改成"回收冷映射"：
   30 天零点击、且分享已失效的才删。
3. **凭据写回是回调驱动的**。夸克刷 __puus、百度/迅雷/光鸭换 refresh_token，
   都会在请求过程中发生。不落库的话，进程一重启就集体失效。
"""

from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .sdk import registry, urls
from .sdk.errors import (
    AuthError,
    ConfigError,
    NetdiskError,
    QuotaExceededError,
    TooLargeError,
    UnsupportedError,
    UpstreamError,
    ViolationError,
)
from .sdk.types import Account, ShareLink

log = logging.getLogger("pantrace.pan")

_db_path: Path | None = None
_inited = False
_lock = threading.Lock()
_cfg: dict = {
    "timeout": 15.0,
    "max_attempts": 3,
    "cold_days": 30,
    # 同一个平台两次转存之间至少隔这么多秒。并发点击打到同一个网盘是最容易
    # 触发风控的行为，比单次转存失败代价大得多
    "min_interval": 3.0,
    # 单条资源超过这个体积就不转。网盘空间是有限资源，一个 100G 的合集
    # 能把一个账号废掉，而它带来的点击和 1G 的资源一模一样
    "max_gb": 30.0,
}

# 换账号有意义的两类错。其余错换了也白换，直接抛
_SWITCHABLE = (AuthError, QuotaExceededError)

# 每个平台一个 [锁, 上次放行时刻]，见 _rate_gate
_gates: dict[str, list] = {}
_gates_guard = threading.Lock()


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
    for key, value in cfg.items():
        if key in _cfg:
            _cfg[key] = value


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("pan 未初始化，先调用 init()")
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
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
            CREATE TABLE IF NOT EXISTS pan_accounts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                type          TEXT NOT NULL,
                name          TEXT NOT NULL DEFAULT '',
                cookie        TEXT NOT NULL DEFAULT '',
                access_token  TEXT NOT NULL DEFAULT '',
                refresh_token TEXT NOT NULL DEFAULT '',
                expires_at    INTEGER NOT NULL DEFAULT 0,
                temp_dir      TEXT NOT NULL DEFAULT '',
                status        INTEGER NOT NULL DEFAULT 1,   -- 1 启用 / 0 停用
                fail_count    INTEGER NOT NULL DEFAULT 0,
                last_error    TEXT NOT NULL DEFAULT '',
                extra         TEXT NOT NULL DEFAULT '{}',
                created_ts    REAL NOT NULL,
                updated_ts    REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pan_accounts_type ON pan_accounts(type, status);
            CREATE INDEX IF NOT EXISTS idx_pan_accounts_fail ON pan_accounts(fail_count);
            """
        )


# ---- 账号 ----

def _row_to_account(row: sqlite3.Row) -> Account:
    try:
        extra = json.loads(row["extra"] or "{}")
    except ValueError:
        extra = {}
    return Account(
        id=int(row["id"]),
        type=str(row["type"]),
        name=str(row["name"]),
        cookie=str(row["cookie"]),
        access_token=str(row["access_token"]),
        refresh_token=str(row["refresh_token"]),
        expires_at=int(row["expires_at"]),
        temp_dir=str(row["temp_dir"]),
        status=int(row["status"]),
        fail_count=int(row["fail_count"]),
        last_error=str(row["last_error"]),
        extra=extra if isinstance(extra, dict) else {},
    )


def list_accounts(platform: str = "") -> list[Account]:
    with _session() as conn:
        if platform:
            rows = conn.execute(
                "SELECT * FROM pan_accounts WHERE type=? ORDER BY fail_count, id",
                (registry.resolve_type(platform),),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM pan_accounts ORDER BY type, id").fetchall()
    return [_row_to_account(r) for r in rows]


def add_account(
    platform: str,
    *,
    name: str = "",
    cookie: str = "",
    access_token: str = "",
    refresh_token: str = "",
    expires_at: int = 0,
    temp_dir: str = "",
    extra: dict | None = None,
) -> int:
    now = time.time()
    with _session() as conn:
        cur = conn.execute(
            """INSERT INTO pan_accounts
               (type,name,cookie,access_token,refresh_token,expires_at,temp_dir,extra,created_ts,updated_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                registry.resolve_type(platform), name, cookie, access_token,
                refresh_token, int(expires_at), temp_dir,
                json.dumps(extra or {}, ensure_ascii=False), now, now,
            ),
        )
        return int(cur.lastrowid or 0)


def update_account(account_id: int, **fields) -> None:
    allowed = {
        "name", "cookie", "access_token", "refresh_token",
        "expires_at", "temp_dir", "status", "fail_count", "last_error", "extra",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "extra" in sets and not isinstance(sets["extra"], str):
        sets["extra"] = json.dumps(sets["extra"], ensure_ascii=False)
    sets["updated_ts"] = time.time()
    columns = ", ".join(f"{k}=?" for k in sets)
    with _session() as conn:
        conn.execute(
            f"UPDATE pan_accounts SET {columns} WHERE id=?",
            (*sets.values(), account_id),
        )


def delete_account(account_id: int) -> None:
    with _session() as conn:
        conn.execute("DELETE FROM pan_accounts WHERE id=?", (account_id,))


def _pick(platform: str, limit: int) -> list[Account]:
    """按 fail_count 升序挑账号，同分随机打散。

    纯随机（竞品的做法）会让一个已经挂着 5 次失败的账号继续被选中；
    纯排序又会让排第一的账号被打爆。升序 + 同分随机是折中。
    """
    accounts = [a for a in list_accounts(platform) if a.enabled]
    if not accounts:
        return []
    buckets: dict[int, list[Account]] = {}
    for account in accounts:
        buckets.setdefault(account.fail_count, []).append(account)
    ordered: list[Account] = []
    for count in sorted(buckets):
        group = buckets[count]
        random.shuffle(group)
        ordered.extend(group)
    return ordered[: max(1, limit)]


def _bump_fail(account: Account, error: NetdiskError) -> None:
    update_account(
        account.id,
        fail_count=account.fail_count + 1,
        last_error=str(error)[:300],
    )
    log.warning("账号 %s#%s 失败（%s）：%s", account.type, account.id, error.kind, error)


def _reset_fail(account: Account) -> None:
    if account.fail_count:
        update_account(account.id, fail_count=0, last_error="")


def _credential_hook(account: Account):
    """客户端刷凭据时回调。cookie 串里用 __token__ / __refresh__ 传新 token。

    不落库的后果：进程重启后所有 access_token 都是旧的，第一次点击必然
    401，然后才被迫刷新——用户会看到一次莫名其妙的失败。
    """

    def _hook(payload: str) -> None:
        try:
            if "__token__" in payload:
                pairs = dict(
                    part.split("=", 1) for part in payload.split(";") if "=" in part
                )
                update_account(
                    account.id,
                    access_token=pairs.get("__token__", account.access_token),
                    refresh_token=pairs.get("__refresh__", account.refresh_token),
                    expires_at=int(time.time()) + 3600 * 12,
                )
                account.access_token = pairs.get("__token__", account.access_token)
                account.refresh_token = pairs.get("__refresh__", account.refresh_token)
            else:
                merged = f"{account.cookie}; {payload}" if account.cookie else payload
                update_account(account.id, cookie=merged)
                account.cookie = merged
        except Exception:  # noqa: BLE001
            log.warning("账号 %s 凭据回写失败", account.id)

    return _hook


# ---- 转存流水线 ----

def _rate_gate(platform: str) -> None:
    """同平台转存节流。拿锁顺序睡：同一个网盘上的转存被排成一条队。

    按平台分开锁，是为了别让夸克的 3 秒把百度的点击也一起拖住。
    """
    interval = float(_cfg["min_interval"])
    if interval <= 0:
        return
    with _gates_guard:
        gate = _gates.get(platform)
        if gate is None:
            gate = _gates[platform] = [threading.Lock(), 0.0]
    with gate[0]:
        wait = interval - (time.monotonic() - gate[1])
        if wait > 0:
            time.sleep(wait)
        gate[1] = time.monotonic()


def transfer(url: str, pwd: str = "", platform: str = "", title: str = "") -> ShareLink:
    """源分享 → 我们的分享。失败抛异常，**不做任何缓存判断**（那是 resolver 的事）。

    这里只做三件事：挑账号、失败换下一个、把成功的账号记在那个 ShareLink 上。
    """
    if not _inited:
        raise ConfigError("pan 未初始化")
    parsed = urls.parse(url)
    key = registry.resolve_type(platform or parsed.platform)
    if not urls.parse(url).ok and key == "unknown":
        raise UnsupportedError("链接不是受支持的网盘分享")
    if not registry.supported(key):
        raise UnsupportedError(f"{platform or key} 暂不支持转存")

    candidates = _pick(key, int(_cfg["max_attempts"]))
    if not candidates:
        raise ConfigError(f"{key} 没有可用账号，先在后台加一个")

    _rate_gate(key)

    last: NetdiskError | None = None
    for account in candidates:
        client = registry.create(
            key,
            account,
            timeout=float(_cfg["timeout"]),
            on_cookie=_credential_hook(account),
            max_bytes=int(float(_cfg["max_gb"]) * 1024**3),
        )
        try:
            link = client.mirror(url, pwd, title)
            _reset_fail(account)
            link.note = (link.note + f"|account={account.id}").strip("|")
            return link
        except _SWITCHABLE as exc:
            _bump_fail(account, exc)
            last = exc
            continue
        except (ViolationError, UnsupportedError, TooLargeError):
            # 这三类换账号也没用：资源本身的问题，不是账号的问题
            raise
        except NetdiskError as exc:
            # 分享本身的错换账号也没用，但先记一笔，方便看是哪个账号在出问题
            last = exc
            if exc.share_bad:
                raise
            _bump_fail(account, exc)
            continue
        finally:
            client.close()

    raise last or UpstreamError("所有账号都失败了")


def probe(account_id: int) -> dict:
    """单账号体检。后台"测试"按钮和巡检都用它。"""
    accounts = [a for a in list_accounts() if a.id == account_id]
    if not accounts:
        return {"ok": False, "reason": "账号不存在"}
    account = accounts[0]
    try:
        client = registry.create(account.type, account, timeout=float(_cfg["timeout"]))
    except NetdiskError as exc:
        return {"ok": False, "reason": str(exc)}
    try:
        alive = client.probe()
        if alive:
            _reset_fail(account)
            return {"ok": True, "reason": ""}
        _bump_fail(account, AuthError("账号探测失败"))
        return {"ok": False, "reason": "账号登录态不可用"}
    except NetdiskError as exc:
        _bump_fail(account, exc)
        return {"ok": False, "reason": str(exc)}
    finally:
        client.close()


# ---- 空间回收 ----

def recycle_expired(mirrors_session, *, grace_seconds: int = 300) -> dict:
    """回收过了有效窗口的转存文件（竞品模式：链接只活半小时）。

    不靠网盘自己的分享有效期——夸克最短只给 1 天，做不到 30 分钟。
    真正让链接失效的是**删掉源文件**，这是竞品验证过的路子。

    行保留、状态改 `expired`：下次点击走重转，hits 统计也不丢。
    宽限 grace_seconds 是给"刚好在截止前点开"的人留的余量——
    文件不能卡着 30:00 删，否则他正在转存时源就没了。
    """
    now = time.time()
    with _session() as conn:
        rows = conn.execute(
            """SELECT src_key, platform, account_id, fids, expires_at FROM mirrors
               WHERE status='ok' AND expires_at > 0 AND expires_at < ?""",
            (now - grace_seconds,),
        ).fetchall()

    removed_rows = 0
    removed_files = 0
    errors: list[str] = []
    accounts = {a.id: a for a in list_accounts()}

    for row in rows:
        account_id = int(row["account_id"] or 0)
        fids = _parse_fids(row["fids"])
        key = row["src_key"]
        if not fids:
            # 没有 fids 说明转存时就没拿到（老数据），删不了文件，直接标记回收
            with mirrors_session() as conn:
                conn.execute(
                    "UPDATE mirrors SET status='expired' WHERE src_key=?", (key,)
                )
            removed_rows += 1
            continue

        account = accounts.get(account_id)
        if account is None or not account.enabled:
            # 账号没了就删不了，留着自己过不去；标记出来让人在后台看见
            errors.append(f"{key[:8]}: 账号 {account_id} 不可用，文件未回收")
            continue

        try:
            client = registry.create(account.type, account, timeout=float(_cfg["timeout"]))
            try:
                client.delete(fids)
                removed_files += len(fids)
            finally:
                client.close()
        except NetdiskError as exc:
            errors.append(f"{key[:8]}: {exc}")
            continue

        with mirrors_session() as conn:
            conn.execute(
                "UPDATE mirrors SET status='expired', fids='[]' WHERE src_key=?", (key,)
            )
        removed_rows += 1

    return {"rows": removed_rows, "files": removed_files, "errors": errors}


def recycle_trash(mirrors_session, *, limit: int = 50) -> dict:
    """删掉"换新链接之后剩下来的旧副本"。

    `mirrors` 每行只存得下最新一组 fids：窗口到期重转一次，上一轮的文件就
    失去引用了，recycle_expired 永远扫不到它们。30 分钟一转，一天几十份，
    账号撑不过一天，所以重转时必须把旧 fids 排进 `mirror_trash` 队列。

    到点就删；删不掉也出队——留在队列里只会每 5 分钟重试一次，永远删不掉的
    条目会把队头堵死。失败写在返回值里，由日志暴露出来人工处理。
    """
    now = time.time()
    with mirrors_session() as conn:
        rows = conn.execute(
            """SELECT id, account_id, fids FROM mirror_trash
               WHERE delete_after <= ? ORDER BY id LIMIT ?""",
            (now, int(limit)),
        ).fetchall()

    removed_files = 0
    errors: list[str] = []
    accounts = {a.id: a for a in list_accounts()}

    for row in rows:
        rid = int(row["id"])
        account_id = int(row["account_id"] or 0)
        fids = _parse_fids(row["fids"])
        account = accounts.get(account_id)

        if fids and account is not None and account.enabled:
            try:
                client = registry.create(account.type, account, timeout=float(_cfg["timeout"]))
                try:
                    client.delete(fids)
                    removed_files += len(fids)
                finally:
                    client.close()
            except NetdiskError as exc:
                errors.append(f"旧副本 {rid}: {exc}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"旧副本 {rid}: {exc.__class__.__name__}")
        elif fids:
            errors.append(f"旧副本 {rid}: 账号 {account_id} 不可用，文件未回收")

        with mirrors_session() as conn:
            conn.execute("DELETE FROM mirror_trash WHERE id=?", (rid,))

    return {"rows": len(rows), "files": removed_files, "errors": errors}


def cleanup(mirrors_session, *, cold_days: int = 0) -> dict:
    """回收冷映射占用的空间。

    参数是 resolver 的 session 生成器——mirrors 表归它管，这里不直接碰 SQL，
    免得两处各写一份 schema 然后慢慢漂移。

    只删两类：
      1. status=fail 且超过 7 天的记录（没有文件，纯行）
      2. hits=0、超过 cold_days 天没人点的 ok 记录（连文件一起删）
    """
    days = cold_days or int(_cfg["cold_days"])
    now = time.time()
    removed_rows = 0
    removed_files = 0
    errors: list[str] = []

    with _session() as conn:
        dead = conn.execute(
            "SELECT src_key, account_id, fids FROM mirrors WHERE status='fail' AND updated_ts < ?",
            (now - 7 * 86400,),
        ).fetchall()
        cold = conn.execute(
            """SELECT src_key, platform, account_id, fids, our_url FROM mirrors
               WHERE status='ok' AND hits <= 0 AND created_ts < ?""",
            (now - days * 86400,),
        ).fetchall()

    # 先删文件，成功了再删行。反过来会出现"行没了、文件还在"的孤儿
    for row in cold:
        account_id = int(row["account_id"] or 0)
        fids = _parse_fids(row["fids"])
        if not account_id or not fids:
            continue
        accounts = [a for a in list_accounts() if a.id == account_id]
        if not accounts or not accounts[0].enabled:
            continue
        account = accounts[0]
        try:
            client = registry.create(account.type, account, timeout=float(_cfg["timeout"]))
            try:
                client.delete(fids)
                removed_files += len(fids)
            finally:
                client.close()
        except NetdiskError as exc:
            errors.append(f"{row['src_key'][:8]}: {exc}")
            continue
        with mirrors_session() as conn:
            conn.execute("DELETE FROM mirrors WHERE src_key=?", (row["src_key"],))
            removed_rows += 1

    for row in dead:
        with mirrors_session() as conn:
            conn.execute("DELETE FROM mirrors WHERE src_key=?", (row["src_key"],))
            removed_rows += 1

    return {
        "rows": removed_rows,
        "files": removed_files,
        "errors": errors,
        "coldDays": days,
    }


def _parse_fids(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    try:
        value = json.loads(str(raw))
    except ValueError:
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


# ---- 后台视图 ----

def summary() -> dict:
    """后台账号面板用。密码类字段一律不下发。"""
    accounts = list_accounts()
    return {
        "total": len(accounts),
        "enabled": sum(1 for a in accounts if a.enabled),
        "byType": _count_by_type(accounts),
        "items": [
            {
                "id": a.id,
                "type": a.type,
                "name": a.name,
                "status": a.status,
                "tempDir": a.temp_dir,
                "hasCookie": bool(a.cookie),
                "hasToken": bool(a.access_token or a.refresh_token),
                "failCount": a.fail_count,
                "lastError": a.last_error,
                "expiresAt": a.expires_at,
            }
            for a in accounts
        ],
    }


def _count_by_type(accounts: list[Account]) -> dict[str, int]:
    out: dict[str, int] = {}
    for account in accounts:
        out[account.type] = out.get(account.type, 0) + 1
    return out
