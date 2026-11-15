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
import re
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
    PasscodeRequiredError,
    PasscodeWrongError,
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
    # 单条资源超过这个体积就不转。**0 = 不限制**（2026-11 起的默认）。
    # 曾经是 30，用来防"一个 100G 合集废掉一个账号"；取消后容量完全交给
    # 30 分钟窗口 + 旧副本回收来控制，账号满了就只能等回收或人工清
    "max_gb": 0.0,
    # 转存后清掉源分享里夹带的广告文件（"加群领福利"那一类）。默认关：
    # 关键词配错会删掉正片，先手工验一条再开
    "ad_filter": False,
    "ad_keywords": "",
    # 往下探几层。1 = 只看转存内容的直接子项，2 = 再进一层
    "ad_depth": 2,
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


# 启用中的平台类型集合 + 缓存时刻。搜索结果一屏 20 条，逐条查库太浪费；
# 一次 SELECT 覆盖所有平台，20 秒的滞后换一屏只查一次。
_types_cache: tuple[float, frozenset[str]] | None = None
_ACCOUNT_CACHE_TTL = 20.0


def _enabled_types() -> frozenset[str]:
    global _types_cache
    now = time.time()
    if _types_cache and now - _types_cache[0] < _ACCOUNT_CACHE_TTL:
        return _types_cache[1]
    with _session() as conn:
        rows = conn.execute(
            "SELECT DISTINCT type FROM pan_accounts WHERE status=1"
        ).fetchall()
    value = frozenset(str(row["type"]) for row in rows)
    _types_cache = (now, value)
    return value


def has_account(platform: str) -> bool:
    """这个平台现在有没有启用中的账号。

    用途只有一个：决定搜索结果卡片上的提取码要不要抹掉。有账号才可能转成
    我们自己的链接（那时的码由落地页给），没账号点击必然回退原链接——
    那种情况下抹码等于把用户扔在一个要密码的页面前，而他手上什么都没有。
    """
    return registry.resolve_type(platform) in _enabled_types()


def _invalidate_account_cache() -> None:
    """账号增删改之后立刻失效，别让"刚加完账号"还要等 20 秒。"""
    global _types_cache
    _types_cache = None


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
        account_id = int(cur.lastrowid or 0)
    _invalidate_account_cache()
    return account_id


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
    # 只有启用状态会改变 has_account 的答案；fail_count 每次转存都在动，
    # 跟着清缓存等于把缓存白做
    if "status" in sets:
        _invalidate_account_cache()


def delete_account(account_id: int) -> None:
    with _session() as conn:
        conn.execute("DELETE FROM pan_accounts WHERE id=?", (account_id,))
    _invalidate_account_cache()


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
                # 百度会带 __expires__（真实到期时间）；其它盘不给，就退回旧默认值
                expires_at = int(pairs.get("__expires__") or 0) or int(time.time()) + 3600 * 12
                update_account(
                    account.id,
                    access_token=pairs.get("__token__", account.access_token),
                    refresh_token=pairs.get("__refresh__", account.refresh_token),
                    expires_at=expires_at,
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

    # 0 就是把 max_file_bytes 一起清零——0 在 base.mirror 里表示不检查体积。
    # 这里不能省成"不传"：不传的话 registry.create 不会覆盖 adapter 的类默认值，
    # 百度那边还留着 100G 的老默认，等于换了个阈值继续限
    max_bytes = int(float(_cfg["max_gb"]) * 1024**3) if float(_cfg["max_gb"]) > 0 else 0

    last: NetdiskError | None = None
    for account in candidates:
        client = registry.create(
            key,
            account,
            timeout=float(_cfg["timeout"]),
            on_cookie=_credential_hook(account),
            max_bytes=max_bytes,
        )
        try:
            link = client.mirror(url, pwd, title)
            _reset_fail(account)
            _scrub_ads(client, link)
            link.note = (link.note + f"|account={account.id}").strip("|")
            return link
        except _SWITCHABLE as exc:
            _bump_fail(account, exc)
            last = exc
            continue
        except (ViolationError, UnsupportedError, TooLargeError):
            # 这三类换账号也没用：资源本身的问题，不是账号的问题
            raise
        except (PasscodeRequiredError, PasscodeWrongError):
            # 缺码/错码也不是账号的问题：换账号重试只会把每个账号的 fail_count
            # 都刷一遍，最后仍然失败，还把好账号标记成"有问题"
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


def _scrub_ads(client, link) -> None:
    """转存完顺手把源分享夹带的广告文件删掉（"加群领福利"那一类）。

    失败一律吞掉：广告没清干净只是脏，转存失败是故障，两件事不能混。
    删掉的文件必须从 `link.fids` 里摘掉——那串会进 mirrors，回收任务照着它
    删，留着已经删过的 fid 只会让下一轮 delete 报错、连累同批正常文件。
    """
    if not _cfg.get("ad_filter") or not link.fids:
        return
    keywords = [
        k.strip() for k in str(_cfg.get("ad_keywords") or "").split(",") if k.strip()
    ]
    if not keywords:
        log.warning("广告清理已开启但关键词为空（MIRROR_AD_KEYWORDS），本次跳过")
        return
    try:
        removed = client.scrub_ads(
            link.fids, keywords, max_depth=int(_cfg.get("ad_depth") or 2)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("广告文件清理失败（不影响转存）：%s", exc)
        return
    if not removed:
        return
    gone = set(removed)
    link.fids = [f for f in link.fids if f not in gone]
    log.info("广告文件已清 %s 个，分享里只剩正片", len(removed))


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
        detail = client.probe_detail()
        if detail.get("ok"):
            _reset_fail(account)
            return {
                "ok": True,
                "reason": detail.get("reason", ""),
                "detail": detail.get("detail") or {},
            }
        reason = detail.get("reason") or "账号登录态不可用"
        _bump_fail(account, AuthError(reason))
        return {"ok": False, "reason": reason, "detail": detail.get("detail") or {}}
    except NetdiskError as exc:
        _bump_fail(account, exc)
        return {"ok": False, "reason": str(exc), "detail": {}}
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


# ---- 违规分享巡检 ----

#: 每个账号最多翻几页（200/页）。4 页 = 800 条，再多说明该人工进去看了
_VIOLATED_MAX_PAGES = 4
#: 列表接口单页条数，跟 _collect_violated 里的"翻完了没"判断绑在一起
_VIOLATED_PAGE_SIZE = 200


#: 从我们自己的分享链接里反解 share_id。夸克/UC 都是 `/s/<id>`，
#: 提取码在 query 里，不影响。百度的短链也是这个形状，够通用。
_SHARE_ID_RE = re.compile(r"/s/([0-9A-Za-z_-]{4,})")


def _protected_share_ids(mirrors_session) -> set[str]:
    """当前还有映射指着、且窗口没过的分享 id —— 撤销时必须绕开。

    这是这个任务唯一的硬保护。上游的 `_filter=violated` 没有文档，万一哪次
    改版把它当成未知参数忽略掉，返回的就是账号下的**全部**分享；照着撤
    等于把用户手上的链接当场打死。有了这张排除表，即使筛选失效，最多也只
    撤掉那些已经没人指的（本来就是死链，撤了正好）。
    """
    if mirrors_session is None:
        return set()
    now = time.time()
    try:
        with mirrors_session() as conn:
            rows = conn.execute(
                "SELECT our_url FROM mirrors WHERE status='ok' AND expires_at > ?",
                (now,),
            ).fetchall()
    except sqlite3.Error:
        return set()
    out: set[str] = set()
    for row in rows:
        match = _SHARE_ID_RE.search(str(row["our_url"] or ""))
        if match:
            out.add(match.group(1))
    return out


def _collect_violated(client, max_pages: int = _VIOLATED_MAX_PAGES) -> list[str]:
    """把一个账号的违规分享 id 全捞出来。"""
    ids: list[str] = []
    for page in range(1, max(1, int(max_pages)) + 1):
        batch = client.list_shares(
            only_violated=True, page=page, size=_VIOLATED_PAGE_SIZE
        )
        ids.extend(s.share_id for s in batch if s.share_id)
        if len(batch) < _VIOLATED_PAGE_SIZE:
            break
    return ids


def check_violated(
    *, dry_run: bool = False, max_pages: int = _VIOLATED_MAX_PAGES,
    mirrors_session=None,
) -> dict:
    """扫每个账号下被平台判违规的分享，批量撤销。

    为什么单独做这件事：我们转存的是别人的东西，被判违规的分享挂在账号上，
    代价是限流甚至封号。**删文件撤不掉它**——分享条目还在，这是"回收"和
    "撤销"的根本区别。竞品也单独跑这个任务（每 5 分钟，且只做夸克）。

    不需要 mirrors 里有 share_id：扫的是**账号**不是映射，历史数据不用补。
    但 mirrors 要用来做排除表（`mirrors_session` 传 resolver.session）：
    窗口内还在用的分享绝不撤，详见 _protected_share_ids。

    没实现撤销接口的网盘（百度/迅雷/光鸭/139）抛 UnsupportedError，静默跳过，
    不拖累其他账号。dry_run=True 只列不撤，上线前用它确认"要撤的是哪些"。
    """
    if not _inited:
        return {
            "accounts": 0, "skipped": 0, "scanned": 0, "revoked": 0,
            "failed": 0, "protected": 0, "errors": ["pan 未初始化"], "samples": [],
        }

    stats: dict = {
        "accounts": 0, "skipped": 0, "scanned": 0, "revoked": 0,
        "failed": 0, "protected": 0, "errors": [], "samples": [],
    }
    protected = _protected_share_ids(mirrors_session)

    for account in [a for a in list_accounts() if a.enabled]:
        if not (account.cookie or account.access_token or account.refresh_token):
            # 半配的账号（后台建了但没填凭据）每轮都建客户端只会刷一堆
            # ConfigError，巡检本身对它也没意义
            stats["skipped"] += 1
            continue
        try:
            client = registry.create(
                account.type,
                account,
                timeout=float(_cfg["timeout"]),
                on_cookie=_credential_hook(account),
            )
        except UnsupportedError:
            stats["skipped"] += 1
            continue
        except NetdiskError as exc:
            stats["errors"].append(f"{account.type}#{account.id} 建客户端失败：{exc}")
            continue

        try:
            try:
                ids = _collect_violated(client, max_pages)
            except UnsupportedError:
                # 这个盘没有"列分享"接口，不是错误，是没做
                stats["skipped"] += 1
                continue

            if not ids:
                continue

            stats["accounts"] += 1
            stats["scanned"] += len(ids)

            # 先摘掉还在用的：这一步不让任何配置背锅
            keep = [sid for sid in ids if sid in protected]
            ids = [sid for sid in ids if sid not in protected]
            if keep:
                stats["protected"] += len(keep)
                log.info(
                    "%s#%s 有 %s 条分享仍在 30 分钟窗口内，跳过不撤",
                    account.type, account.id, len(keep),
                )
            if not ids:
                continue

            if dry_run:
                stats["samples"].extend(
                    f"{account.type}#{account.id}:{sid}" for sid in ids[:5]
                )
                continue

            result = client.revoke_shares(ids)
            stats["revoked"] += int(result.get("ok") or 0)
            stats["failed"] += int(result.get("fail") or 0)
        except NetdiskError as exc:
            stats["errors"].append(f"{account.type}#{account.id}：{exc}")
        except Exception as exc:  # noqa: BLE001
            # 一个账号的意外不该让整轮巡检停在半路
            stats["errors"].append(f"{account.type}#{account.id}：{exc.__class__.__name__}")
        finally:
            client.close()

    return stats


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
                # 能不能自动续期。false 就是"token 用满 30 天就没了"，
                # 后台要能一眼看出来，别等它悄悄死掉
                "hasOAuth": bool(a.extra.get("client_id") and a.extra.get("client_secret")),
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
