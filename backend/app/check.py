"""分享链接存活检测：只打各网盘的**公开**接口，不碰我们的转存账号。

为什么要有这个模块
------------------
搜索结果里混着大量已删/已取消的分享。现在唯一的兜底是"2 个不同访客报失效
才隐藏"（feedback.HIDE_THRESHOLD），也就是**事后**：用户点开 → 转存失败 →
报一条工单 → 等第二个人也踩到。这个模块把判断提前到用户点开之前，同时把
`dead_link` 这一类工单自动闭环掉。

三条硬约束（改之前先读）
------------------------
1. **不在搜索链路里同步做**。搜索本身已经要等上游 2~60 秒，不能再加 20 条
   外部请求。检测一律异步：搜索返回后提交线程池，结果落表，下一次渲染时读表。
2. **不用转存账号**。这里打的都是不需要登录的匿名接口（夸克/UC 的
   sharepage/token、阿里的 get_share_by_anonymous、123 的 share/info）。
   账号是稀缺资源，拿去探活等于烧配额，还会把风控引到账号上。
3. **判不准就必须说判不准**。网络错误、限流、验证码、接口改版，一律
   `uncertain`，绝不 `bad`。`bad` 会让结果从页面消失——误杀的代价远大于漏放。

状态与 TTL
----------
    ok           链接有效（列表能读到内容）
    bad          已失效 / 已取消 / 违规 / 空分享 —— 出口会隐藏
    locked       需要提取码，而结果里没带码或码不对 —— 只提示，不隐藏
    uncertain    判不了（超时、限流、接口异常、需要人机验证）
    unsupported  该平台没有匿名检测接口，或链接形不成 id

TTL 的取舍和 PanSou 相反：**ok 和 bad 都是 6 小时**。
- ok 要短，因为它决定"用户现在看到的链接还能不能开"，一天前的结论没有意义；
- bad 也要短，因为任何一次误判都会让一条好资源消失，6 小时必须给它自愈的机会；
- locked 24 小时（码不会隔夜就对）、unsupported 7 天（纯省事）、uncertain 30 分钟
  （可能是刚才被打限流了，很快就好）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .sdk import urls as url_util
from .config import get_settings

log = logging.getLogger("pantrace.check")

STATE_OK = "ok"
STATE_BAD = "bad"
STATE_LOCKED = "locked"
STATE_UNCERTAIN = "uncertain"
STATE_UNSUPPORTED = "unsupported"

STATES = (STATE_OK, STATE_BAD, STATE_LOCKED, STATE_UNCERTAIN, STATE_UNSUPPORTED)

_TTL = {
    STATE_OK: 6 * 3600,
    STATE_BAD: 6 * 3600,
    STATE_LOCKED: 24 * 3600,
    STATE_UNSUPPORTED: 7 * 86400,
    STATE_UNCERTAIN: 1800,
}

# 上游平台 key → 检测用的盘类型。前端平台列表用的是 `ali` / `yidong`，
# 检测接口按网盘自己的叫法（aliyun / mobile）。对齐不了的一律 unsupported。
_ALIAS = {
    "quark": "quark",
    "uc": "uc",
    "baidu": "baidu",
    "ali": "aliyun",
    "aliyun": "aliyun",
    "123": "123",
    "yidong": "mobile",
    "mobile": "mobile",
    "c139": "mobile",
    "guangya": "guangya",
    "xunlei": "xunlei",
    "115": "115",
    "tianyi": "tianyi",
}

# 真正实现了 checker 的平台。其余平台直接落 unsupported，不去发请求。
_IMPLEMENTED = frozenset({"quark", "uc", "aliyun", "123", "baidu"})

# 夸克和 UC 共用一套接口，只差 pr / api 基址 / referer / UA。
# 与 sdk/adapters/quark/const.py 的 PROFILES 保持一致——那边改了这里也要改，
# 但**不要 import 那边的常量**：这是匿名检测，不该依赖转存 SDK 的模块加载。
_CLOUD_PROFILES = {
    "quark": {
        "api": "https://drive-pc.quark.cn/1/clouddrive",
        "pr": "ucpro",
        "referer": "https://pan.quark.cn",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
            "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
        ),
        # token 走 drive-h，详情走 drive-pc。两个域名的风控策略不同，
        # 实测 token 走 drive-pc 也能通，但 drive-h 更稳（PanSou 同样这么分）。
        "token_api": "https://drive-h.quark.cn/1/clouddrive",
    },
    "uc": {
        "api": "https://pc-api.uc.cn/1/clouddrive",
        "pr": "UCBrowser",
        "referer": "https://drive.uc.cn",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
            "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
        ),
        "token_api": "https://pc-api.uc.cn/1/clouddrive",
    },
}

# 夸克/UC 的错误码。和文档里的 code 一一对应，含义照 sdk 的 ERROR_MAP：
# 这几个是"分享本身没了"，可以隐藏；41008 是要码；其余码（含 41007 码错）
# 归 locked 或 uncertain，宁可只提示不隐藏。
_QUARK_BAD_CODES = {41004, 41006, 41009, 41010, 41011, 41012, 41026, 41031}
_QUARK_LOCKED_CODES = {41007, 41008}
_BAD_WORDS = ("不存在", "失效", "违规", "过期", "取消", "删除", "封禁")
_LOCKED_WORDS = ("提取码", "访问码", "密码", "passcode")

# 同一平台两次请求之间至少隔这么久（秒）。匿名接口没有配额说明，
# 4 个 worker 全速打等于每秒 8~10 发，容易被当成爬虫。0.25s 大约 4 发/秒，
# 一页 20 条在 5~8 秒内检完，用户感知不到。
_HOST_INTERVAL = 0.25

_db_path: Path | None = None
_inited = False
_enabled = True
_hide_bad = True
_timeout = 8.0
_per_page = 20
_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_session: requests.Session | None = None
_last_hit: dict[str, float] = {}
_last_hit_lock = threading.Lock()
_write_count = 0
_write_lock = threading.Lock()

# 单次查询最多带多少个 key。搜索一页最多 50 条，留余量。
_MAX_KEYS = 200


# ---------------------------------------------------------------- 生命周期


def init(
    db_path: str | Path,
    *,
    enabled: bool = True,
    hide_bad: bool = True,
    workers: int = 3,
    timeout: float = 8.0,
    per_page: int = 20,
) -> None:
    """初始化。和 analytics / feedback 共用同一个 SQLite 文件。"""
    global _db_path, _inited, _enabled, _hide_bad, _timeout, _per_page, _pool
    _db_path = Path(db_path).expanduser()
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _enabled = bool(enabled)
    _hide_bad = bool(hide_bad)
    _timeout = max(2.0, float(timeout or 8.0))
    _per_page = max(0, int(per_page or 0))
    _ensure_schema()
    _prune()
    if _pool is None:
        _pool = ThreadPoolExecutor(
            max_workers=max(1, int(workers or 3)),
            thread_name_prefix="linkcheck",
        )
    _inited = True


def is_ready() -> bool:
    return _inited and _enabled


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("check 未初始化，先调用 init()")
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _session_ctx():
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _ensure_schema() -> None:
    with _session_ctx() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS link_check (
                key        TEXT PRIMARY KEY,
                platform   TEXT NOT NULL,
                url        TEXT NOT NULL,
                state      TEXT NOT NULL,
                summary    TEXT NOT NULL DEFAULT '',
                checked_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lc_expires  ON link_check(expires_at);
            CREATE INDEX IF NOT EXISTS idx_lc_state    ON link_check(state, checked_at);
            """
        )


def _prune() -> None:
    """清掉过期超过 7 天的行。表只增不减会把 analytics.db 撑大。"""
    try:
        with _session_ctx() as conn:
            conn.execute("DELETE FROM link_check WHERE expires_at < ?", (time.time() - 7 * 86400,))
    except Exception as exc:  # noqa: BLE001
        log.warning("清理 link_check 失败：%s", exc)


# ---------------------------------------------------------------- 键与读写


def platform_key(raw: str) -> str:
    """上游平台 key → 检测用的盘类型。不认识的原样返回（后面判 unsupported）。"""
    value = str(raw or "").strip().lower()
    return _ALIAS.get(value, value)


def key_of(platform: str, url: str) -> str:
    """缓存键 = 盘类型 + 分享 id。

    必须按分享 id 而不是 URL：同一个分享有 `/s/xxx`、`/s/xxx?pwd=abcd`、
    `share/init?surl=1xxx` 多种写法，按 URL 算会导致同一份内容被反复检测。
    """
    parsed = url_util.parse(url)
    disk = disk_of(url, platform)
    pwd_id = _share_id(disk, url, parsed)
    if pwd_id:
        return f"{disk}:{pwd_id}"
    digest = hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:16]
    return f"{disk}:url:{digest}"


# sdk/urls.py 的域名白名单只收"我们能转存"的盘。检测要覆盖 123 这类
# 没有转存适配器的平台，所以这里补一条只用于取 share id 的规则。
_EXTRA_RULES = {
    "123": (re.compile(r"/s/([A-Za-z0-9_-]+)", re.I),),
    "tianyi": (re.compile(r"/t/([A-Za-z0-9_-]+)", re.I),),
}


def _share_id(disk: str, url: str, parsed=None) -> str:
    parsed = parsed or url_util.parse(url)
    if parsed.pwd_id:
        return parsed.pwd_id
    for pattern in _EXTRA_RULES.get(disk, ()):
        match = pattern.search(url)
        if match:
            return match.group(1)
    return ""


def disk_of(url: str, platform: str = "") -> str:
    """定盘类型。上游平台 key 优先，其次按 URL 识别，最后按域名兜底。

    上游给的 key 是 `ali` / `yidong` 这种自家叫法；`sdk/urls.py` 认得的是
    转存白名单里的盘。两边都认不出时（比如 123），按域名兜底——否则
    检测会被静默跳过，看起来像"这个平台没实现"。
    """
    disk = platform_key(platform)
    if disk in _IMPLEMENTED:
        return disk
    parsed = url_util.parse(url)
    disk = platform_key(parsed.platform)
    if disk in _IMPLEMENTED:
        return disk
    host = str(url or "").lower()
    for needle, name in (
        ("pan.quark.cn", "quark"),
        ("drive.uc.cn", "uc"),
        ("fast.uc.cn", "uc"),
        ("alipan.com", "aliyun"),
        ("aliyundrive.com", "aliyun"),
        ("pan.baidu.com", "baidu"),
        ("123pan.com", "123"),
        ("123684.com", "123"),
        ("123865.com", "123"),
        ("123912.com", "123"),
        ("123592.com", "123"),
    ):
        if needle in host:
            return name
    return disk


def _fresh_map(keys: list[str]) -> dict[str, dict]:
    """批量读未过期的结论。过期的不返回，调用方据此决定要不要重检。"""
    if not keys or _db_path is None:
        return {}
    keys = keys[:_MAX_KEYS]
    placeholders = ",".join("?" * len(keys))
    try:
        with _session_ctx() as conn:
            rows = conn.execute(
                f"SELECT key, state, summary, checked_at, expires_at FROM link_check "
                f"WHERE key IN ({placeholders}) AND expires_at > ?",
                (*keys, time.time()),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("读 link_check 失败：%s", exc)
        return {}
    return {row["key"]: dict(row) for row in rows}


def _save(key: str, platform: str, url: str, state: str, summary: str) -> None:
    now = time.time()
    ttl = _TTL.get(state, 1800)
    try:
        with _session_ctx() as conn:
            conn.execute(
                "INSERT INTO link_check(key, platform, url, state, summary, checked_at, expires_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET state=excluded.state, summary=excluded.summary, "
                "checked_at=excluded.checked_at, expires_at=excluded.expires_at, url=excluded.url",
                (key, platform, url[:500], state, summary[:200], now, now + ttl),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("写 link_check 失败：%s", exc)


# ---------------------------------------------------------------- 对外：标注与过滤


def annotate(items: list[dict]) -> tuple[list[dict], int]:
    """给结果补 `checkState`，并把确认失效的摘掉。

    返回 (新列表, 被摘掉的条数)。

    **只复制不原地改**：items 是 `_cache` 里那份对象的引用，就地写字段会把
    "已核验" 固化进缓存，改开关或回滚都无效——`_public_body` 踩过同样的坑。
    """
    if not _inited or not items:
        return items, 0
    keys = [key_of(it.get("platform") or "", it.get("shareUrl") or "") for it in items]
    known = _fresh_map(keys)
    if not known:
        return items, 0
    kept: list[dict] = []
    dropped = 0
    for item, key in zip(items, keys):
        row = known.get(key)
        if not row:
            kept.append(item)
            continue
        state = row["state"]
        if state == STATE_BAD and _hide_bad:
            dropped += 1
            continue
        kept.append({**item, "checkState": state})
    return kept, dropped


# ---------------------------------------------------------------- 调度


def schedule(items: list[dict], *, force: bool = False) -> int:
    """把未检测/已过期的结果提交给线程池，返回提交条数。

    调用点必须在**响应之后**（BackgroundTasks 里），不能挡在请求链路上。
    """
    if not is_ready() or _pool is None or not items:
        return 0
    # per_page=0 表示"不要自动检"，不能理解成"检全部"——那会把一页 50 条全打出去
    cap = _per_page if _per_page > 0 else 0
    if not cap:
        return 0
    jobs: list[tuple[str, str, str, str]] = []
    for raw in items[:cap]:
        url = str(raw.get("shareUrl") or "").strip()
        if not url.startswith("http"):
            continue
        platform = str(raw.get("platform") or "")
        disk = disk_of(url, platform)
        if disk not in _IMPLEMENTED:
            continue
        key = key_of(platform, url)
        jobs.append((key, disk, url, str(raw.get("sharePwd") or "")))
    if not jobs:
        return 0

    if not force:
        known = _fresh_map([job[0] for job in jobs])
        jobs = [job for job in jobs if job[0] not in known]
    if not jobs:
        return 0

    submitted = 0
    for key, disk, url, pwd in jobs:
        with _inflight_lock:
            if key in _inflight:
                continue
            _inflight.add(key)
        _pool.submit(_run, key, disk, url, pwd)
        submitted += 1
    return submitted


def _run(key: str, disk: str, url: str, pwd: str) -> None:
    started = time.perf_counter()
    try:
        state, summary = check_one(disk, url, pwd)
    except Exception as exc:  # noqa: BLE001
        # 检测函数内部已经吞掉了预期内的异常，走到这里说明是我们自己的 bug。
        # 落 uncertain 而不是 bad：不能因为代码问题让用户看不到资源。
        log.exception("检测异常 %s", key)
        state, summary = STATE_UNCERTAIN, f"检测异常：{type(exc).__name__}"
    finally:
        with _inflight_lock:
            _inflight.discard(key)
    _save(key, disk, url, state, summary)
    ms = int((time.perf_counter() - started) * 1000)
    if state == STATE_BAD:
        log.info("链接已失效 %s %s（%s，%dms）", key, url[:60], summary, ms)
    else:
        log.debug("link check %s → %s %s %dms", key, state, summary, ms)
    _maybe_prune()


def _maybe_prune() -> None:
    global _write_count
    with _write_lock:
        _write_count += 1
        due = _write_count % 500 == 0
    if due:
        _prune()


# ---------------------------------------------------------------- HTTP 基础


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        settings = get_settings()
        s = requests.Session()
        # 与 providers 同理：本机开着代理时 requests 会走代理，表现成"读超时"，
        # 排查半天看不出是代理。默认直连，需要时在配置里显式开。
        s.trust_env = settings.use_env_proxy
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        if settings.proxy_url:
            s.proxies.update({"http": settings.proxy_url, "https": settings.proxy_url})
        _session = s
    return _session


def _throttle(host: str) -> None:
    """同域请求之间留最小间隔。拿锁 sleep 是可接受的：只有 worker 线程会进来。"""
    with _last_hit_lock:
        last = _last_hit.get(host, 0.0)
        now = time.monotonic()
        wait = _HOST_INTERVAL - (now - last)
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.monotonic()


def _request_page(url: str, **kwargs) -> tuple[int, str, str]:
    """取 HTML 页面：返回 (状态码, 正文前 4KB, Location 头)。

    `_request` 只解析 JSON，百度这类只回 HTML 的判定点用它。默认不跟随跳转，
    跳转本身就是结论。**必须关压缩**：百度部分响应声明的 gzip 实际解不开，
    requests 会抛 ContentDecodingError，禁掉才能拿到正文。
    """
    host = url.split("/")[2] if "://" in url else url
    _throttle(host)
    kwargs.setdefault("timeout", (4.0, _timeout))
    kwargs.setdefault("allow_redirects", False)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("accept-encoding", "identity")
    resp = _get_session().request("GET", url, headers=headers, **kwargs)
    body = resp.text if resp.status_code == 200 else ""
    return resp.status_code, body[:4000], resp.headers.get("location", "")


def _request(method: str, url: str, **kwargs) -> tuple[int, Any]:
    """发请求，返回 (状态码, 解析后的 JSON 或 None)。不抛异常。"""
    host = url.split("/")[2] if "://" in url else url
    _throttle(host)
    kwargs.setdefault("timeout", (4.0, _timeout))
    kwargs.setdefault("allow_redirects", True)
    resp = _get_session().request(method, url, **kwargs)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _words(text: str) -> tuple[bool, bool]:
    """从上游的中文文案里兜底判断 (bad, locked)。JSON 字段不全时用。"""
    lowered = str(text or "")
    is_locked = any(word in lowered for word in _LOCKED_WORDS)
    is_bad = any(word in lowered for word in _BAD_WORDS)
    return is_bad, is_locked


# ---------------------------------------------------------------- 各网盘检测


def check_one(disk: str, url: str, pwd: str = "") -> tuple[str, str]:
    """检测一条链接，返回 (state, summary)。**不抛异常**。"""
    disk = platform_key(disk)
    if disk not in _IMPLEMENTED:
        return STATE_UNSUPPORTED, "该平台暂未支持检测"
    parsed = url_util.parse(url)
    pwd_id = _share_id(disk, url, parsed)
    if not pwd_id:
        return STATE_UNSUPPORTED, "无法解析分享地址"
    passcode = (pwd or parsed.passcode or "").strip()
    try:
        if disk in ("quark", "uc"):
            return _check_cloud(disk, pwd_id, passcode)
        if disk == "aliyun":
            return _check_aliyun(pwd_id)
        if disk == "123":
            return _check_123(pwd_id, passcode)
        if disk == "baidu":
            return _check_baidu(pwd_id, passcode)
    except requests.RequestException as exc:
        return STATE_UNCERTAIN, f"网络异常（{type(exc).__name__}）"
    except Exception as exc:  # noqa: BLE001
        log.debug("检测 %s 失败：%s", url, exc, exc_info=True)
        return STATE_UNCERTAIN, f"检测失败（{type(exc).__name__}）"
    return STATE_UNSUPPORTED, "该平台暂未支持检测"


def _check_cloud(disk: str, pwd_id: str, passcode: str) -> tuple[str, str]:
    """夸克 / UC：先换 stoken，再读详情。两家的字段完全一致。"""
    if not pwd_id:
        return STATE_UNSUPPORTED, "无法解析分享地址"
    profile = _CLOUD_PROFILES[disk]
    headers = {
        "content-type": "application/json",
        "origin": profile["referer"],
        "referer": profile["referer"] + "/",
        "user-agent": profile["ua"],
        "accept": "application/json, text/plain, */*",
    }
    params = {"pr": profile["pr"], "fr": "pc"}

    status, payload = _request(
        "POST",
        f"{profile['token_api']}/share/sharepage/token",
        params=params,
        json={"pwd_id": pwd_id, "passcode": passcode, "support_visit_limit_private_share": True},
        headers=headers,
    )
    if not isinstance(payload, dict):
        return STATE_UNCERTAIN, f"分享令牌接口返回非 JSON（HTTP {status}）"
    code = _as_int(payload.get("code"), -1)
    message = str(payload.get("message") or "")
    if code == 0:
        stoken = str((payload.get("data") or {}).get("stoken") or "")
        if not stoken:
            return STATE_UNCERTAIN, "拿到分享令牌为空"
    elif code in _QUARK_LOCKED_CODES:
        return STATE_LOCKED, message or "需要提取码"
    elif code in _QUARK_BAD_CODES:
        return STATE_BAD, message or "链接失效"
    else:
        is_bad, is_locked = _words(message)
        if is_locked:
            return STATE_LOCKED, message
        if is_bad:
            return STATE_BAD, message
        return STATE_UNCERTAIN, message or f"未知返回码 {code}"

    # stoken 里带 `+` `/` `=`，必须交给 requests 编码（params），
    # 自己拼字符串会把 `+` 解成空格，接口回 14001「非法token」。
    status, payload = _request(
        "GET",
        f"{profile['api']}/share/sharepage/detail",
        params={"pwd_id": pwd_id, "stoken": stoken, "ver": 2, **params},
        headers=headers,
    )
    if not isinstance(payload, dict):
        return STATE_UNCERTAIN, f"分享详情接口返回非 JSON（HTTP {status}）"
    code = _as_int(payload.get("code"), -1)
    message = str(payload.get("message") or "")
    data = payload.get("data") or {}
    if code == 0:
        if data.get("is_expire"):
            return STATE_BAD, "分享已过期"
        files = data.get("list") or []
        if files:
            return STATE_OK, f"可访问（{len(files)} 项）"
        return STATE_BAD, "分享里没有内容"
    if code in _QUARK_LOCKED_CODES:
        return STATE_LOCKED, message or "需要提取码"
    if code in _QUARK_BAD_CODES:
        return STATE_BAD, message or "链接失效"
    is_bad, is_locked = _words(message)
    if is_locked:
        return STATE_LOCKED, message
    if is_bad:
        return STATE_BAD, message
    return STATE_UNCERTAIN, message or f"未知返回码 {code}"


def _check_aliyun(share_id: str) -> tuple[str, str]:
    """阿里云盘：get_share_by_anonymous 直接把分享元信息吐出来，最省事的一家。"""
    if not share_id:
        return STATE_UNSUPPORTED, "无法解析分享地址"
    status, payload = _request(
        "POST",
        # 用 api.alipan.com，不要 api.aliyundrive.com：后者在本机实测被
        # 连接重置（curl 也是 000），前者正常。2026-11 阿里云盘已整体切到
        # alipan 域名，老域名还在但链路不稳。
        "https://api.alipan.com/adrive/v3/share_link/get_share_by_anonymous",
        params={"share_id": share_id},
        json={"share_id": share_id},
        headers={
            "content-type": "application/json",
            "origin": "https://www.alipan.com",
            "referer": "https://www.alipan.com/",
            "x-canary": "client=web,app=share,version=v2.3.1",
        },
    )
    if not isinstance(payload, dict):
        return STATE_UNCERTAIN, f"分享接口返回非 JSON（HTTP {status}）"
    code = str(payload.get("code") or "")
    message = str(payload.get("message") or "")
    if code:
        lowered = code.lower()
        if "cancelled" in lowered or "canceled" in lowered or "notfound" in lowered:
            return STATE_BAD, message or "分享已取消或不存在"
        if "forbidden" in lowered or "expired" in lowered or "sharelink" in lowered:
            return STATE_BAD, message or "链接失效"
        if "exceed" in lowered or "frequency" in lowered or "limit" in lowered:
            return STATE_UNCERTAIN, message or "请求过于频繁"
        return STATE_UNCERTAIN, message or code
    file_count = payload.get("file_count")
    if payload.get("share_name") or payload.get("share_title") or (_as_int(file_count, 0) > 0):
        return STATE_OK, "可访问"
    return STATE_UNCERTAIN, message or "接口未返回分享信息"


def _check_123(share_key: str, passcode: str) -> tuple[str, str]:
    """123 云盘：只有 123865 这个入口还回 JSON（123pan.com 已改版成 HTML）。"""
    if not share_key:
        return STATE_UNSUPPORTED, "无法解析分享地址"
    status, payload = _request(
        "GET",
        "https://www.123865.com/api/share/info",
        params={"shareKey": share_key},
        headers={
            "accept": "application/json, text/plain, */*",
            "platform": "web",
            "origin": "https://www.123865.com",
            "referer": "https://www.123865.com/",
        },
    )
    if not isinstance(payload, dict):
        return STATE_UNCERTAIN, f"分享接口返回非 JSON（HTTP {status}）"
    code = _as_int(payload.get("code"), -1)
    message = str(payload.get("message") or "")
    data = payload.get("data") or {}
    if code == 0:
        # 有码且我们手里也有码，就当可用；不能验码对错（接口不校验）。
        if data.get("HasPwd") and not passcode:
            return STATE_LOCKED, "需要提取码"
        return STATE_OK, "可访问"
    if code == 5104 or "失效" in message:
        return STATE_BAD, message or "链接失效"
    return STATE_UNCERTAIN, message or f"未知返回码 {code}"


def _check_baidu(share_id: str, passcode: str) -> tuple[str, str]:
    """百度网盘：匿名 list 对任何链接都回 9019「need verify」，所以分两条路。

    带提取码：verify 换 BDCLND → list，能给出 ok / bad / locked，结论最硬。
    不带码：改打 `share/init` 页，这个页面不查 cookie——
      · 分享存在且要码 → 200，正文含「请输入提取码」
      · 分享不存在或已失效 → 404（通用「页面不存在」页）
    实测 7 条样本两轮全稳（3 条活的 200、4 条死的 404）。
    """
    if not share_id:
        return STATE_UNSUPPORTED, "无法解析分享地址"
    short = share_id[1:] if share_id.startswith("1") and len(share_id) > 1 else share_id
    if not passcode:
        return _check_baidu_init(short)
    headers = {"referer": "https://pan.baidu.com/"}
    status, payload = _request(
        "POST",
        "https://pan.baidu.com/share/verify",
        params={"surl": short, "pwd": passcode},
        data={"pwd": passcode, "vcode": "", "vcode_str": ""},
        headers={**headers, "content-type": "application/x-www-form-urlencoded"},
    )
    if not isinstance(payload, dict):
        return STATE_UNCERTAIN, f"验证接口返回非 JSON（HTTP {status}）"
    errno = _as_int(payload.get("errno"), -1)
    if errno in (-9, -12):
        return STATE_LOCKED, "提取码错误或缺失"
    if errno not in (0, -1):
        return STATE_UNCERTAIN, str(payload.get("errmsg") or f"验证失败 errno={errno}")
    randsk = str(payload.get("randsk") or "")
    cookie = f"BDCLND={quote(randsk, safe='')}" if randsk else ""

    status, payload = _request(
        "GET",
        "https://pan.baidu.com/share/list",
        params={
            "web": 1, "page": 1, "num": 20, "order": "time", "desc": 1,
            "showempty": 0, "shorturl": short, "root": 1, "clienttype": 0,
        },
        headers={**headers, "accept": "application/json, text/plain, */*", **({"cookie": cookie} if cookie else {})},
    )
    if not isinstance(payload, dict):
        return STATE_UNCERTAIN, f"列表接口返回非 JSON（HTTP {status}）"
    errno = _as_int(payload.get("errno"), -1)
    if errno == 0:
        return (STATE_OK, "可访问") if payload.get("list") else (STATE_BAD, "分享里没有内容")
    if errno in (-9, -12):
        return STATE_LOCKED, "需要提取码"
    if errno in (-7, 105, 115, 117, 145):
        return STATE_BAD, "链接失效"
    return STATE_UNCERTAIN, str(payload.get("errmsg") or f"未知 errno={errno}")


def _check_baidu_init(short: str) -> tuple[str, str]:
    """百度无提取码时的判定：只看 share/init 页的状态码。

    这是目前唯一不需要 cookie 的百度判定点。宁可 uncertain 也不猜：
    只有 200 带提取码提示、404 两个明确信号才下结论。
    """
    status, text, location = _request_page(
        "https://pan.baidu.com/share/init",
        params={"surl": short},
        headers={
            "referer": "https://pan.baidu.com/",
            "accept": "text/html,application/xhtml+xml",
        },
    )
    if status == 404:
        return STATE_BAD, "链接不存在或已失效"
    if status in (301, 302, 303, 307, 308):
        # 不需要提取码的公开分享会直接跳到 /s/1xxx，能跳说明分享还在
        return (STATE_OK, "可访问") if location else (STATE_UNCERTAIN, "分享页无跳转地址")
    if status == 200:
        if "请输入提取码" in text or "提取码" in text:
            return STATE_LOCKED, "需要提取码"
        # 200 但不是提取码页：多半是中转页或被安全验证拦住，不下结论
        return STATE_UNCERTAIN, "分享页无法识别（可能需要安全验证）"
    return STATE_UNCERTAIN, f"分享页返回 HTTP {status}"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 观测


def probe(url: str, pwd: str = "") -> dict:
    """手工检测一条并落库。后台「测试链接」用，验接口是否还活着。

    和批量检测走同一个 checker，**不绕过**：手工能过、批量不过，说明问题在
    调度或过滤，不在接口。`force` 由调用方决定（这里 always 覆盖旧结论）。
    """
    parsed = url_util.parse(url)
    disk = disk_of(url)
    started = time.perf_counter()
    state, detail = check_one(disk, url, pwd)
    key = key_of(disk, url)
    if disk in _IMPLEMENTED and _inited:
        _save(key, disk, url, state, detail)
    return {
        "key": key,
        "platform": disk,
        "state": state,
        "summary": detail,
        "ms": int((time.perf_counter() - started) * 1000),
        "supported": disk in _IMPLEMENTED,
    }


def summary() -> dict:
    """给 /api/health 和看板用的一句话统计。永远不抛异常。"""
    if not _inited or _db_path is None:
        return {"enabled": False}
    now = time.time()
    try:
        with _session_ctx() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM link_check WHERE expires_at > ? GROUP BY state",
                (now,),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM link_check").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return {"enabled": _enabled, "error": str(exc)}
    by_state = {row["state"]: row["n"] for row in rows}
    return {
        "enabled": _enabled,
        "hideBad": _hide_bad,
        "rows": int(total),
        "live": by_state,
        "bad": by_state.get(STATE_BAD, 0),
        "inflight": len(_inflight),
    }


def recent(state: str = "", limit: int = 30) -> list[dict]:
    """看板用：最近检测记录。"""
    if not _inited or _db_path is None:
        return []
    limit = max(1, min(int(limit or 30), 200))
    try:
        with _session_ctx() as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM link_check WHERE state=? ORDER BY checked_at DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM link_check ORDER BY checked_at DESC LIMIT ?", (limit,)
                ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("读最近检测记录失败：%s", exc)
        return []
    return [dict(row) for row in rows]
