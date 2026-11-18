"""TG 频道的动态配置：库优先于 .env，后台改完立即生效。

为什么要有这一层：频道名是运营要随手调的东西——某个频道被封、换了一批源、
临时加一个定向频道，都得改。改 .env + 重启一轮对线上太重（systemd 起的
服务没人记得 reload），而且 TG 源是零成本通道，调频道本身就该是低成本动作。

约定（和 `pan.py` 的广告配置同一套）：
- `.env` 的 `TG_CHANNELS` 是**默认值**，只在库里没有覆盖时才用；
- 库里一旦有 `channels` 行，哪怕是空数组，也表示"运营明确要零频道"，
  不再回退到 .env —— 这是禁用 TG 采集的正确方式（比删插件更安全，
  不会把 zero_yield 记到源健康上）；
- 保存时热更内存，下一次搜索就用新频道；缓存键本身带频道列表
  （见 `tg_cache_key`），所以旧结果不会被错用。

存储借用埋点库 `analytics.db`，不另起一个文件：备份、部署、权限都只管一份。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

KEY_CHANNELS = "channels"
MAX_CHANNELS = 20
# Telegram 公开用户名：字母数字下划线，长度 3–64。拒绝 URL / 路径 / 空格，
# build_search_url 里的 quote 是兜底，不是给乱输入开绿灯。
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")

_lock = threading.RLock()
_db_path: Path | None = None
# None = 库里没有覆盖，沿用 .env；[] = 明确禁用所有频道
_channels: list[str] | None = None


def normalize(value, *, allow_empty: bool = True) -> list[str]:
    """清洗频道列表：@ 可省略，逗号/换行/空白都能分隔，去重且拒绝非法字符。

    去重按小写比（Telegram 用户名不区分大小写），但保留原来的写法。
    """
    if value is None:
        raw_items: list = []
    elif isinstance(value, str):
        raw_items = re.split(r"[,，\s]+", value.strip())
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raise ValueError("频道列表格式不对，应为字符串或数组")

    names: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        if raw is None:
            continue
        name = str(raw).strip().lstrip("@").strip()
        if not name:
            continue
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"频道名不合法：{name}（只认字母、数字、下划线，3–64 位）")
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)

    if not names and not allow_empty:
        raise ValueError("至少要填一个频道名")
    if len(names) > MAX_CHANNELS:
        raise ValueError(f"频道最多 {MAX_CHANNELS} 个，当前 {len(names)} 个")
    return names


def init(db_path: str | Path, *, default_channels=()) -> None:
    """启动时读库里的覆盖值。读失败不阻断服务，沿用 .env。"""
    global _db_path, _channels
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        _db_path = path
        _channels = None
        try:
            with _session() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS tg_settings ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_ts REAL NOT NULL)"
                )
                row = conn.execute(
                    "SELECT value FROM tg_settings WHERE key=?", (KEY_CHANNELS,)
                ).fetchone()
            if row:
                _channels = normalize(json.loads(str(row["value"])))
                log.info("TG 频道使用库中配置：%s", _channels or "（空，已禁用）")
        except (sqlite3.Error, ValueError, TypeError) as exc:
            _channels = None
            log.warning("读取 TG 频道库内配置失败，沿用 TG_CHANNELS：%s", exc)


def configured() -> list[str] | None:
    """库里的覆盖值。None = 没有覆盖；空列表 = 明确禁用。返回副本。"""
    with _lock:
        return list(_channels) if _channels is not None else None


def effective(default_channels=()) -> list[str]:
    """当前真正会拿去采集的频道列表。"""
    override = configured()
    if override is not None:
        return override
    try:
        return normalize(default_channels)
    except ValueError as exc:
        log.warning("TG_CHANNELS 配置不合法（%s），视为未配置频道", exc)
        return []


def save(value) -> list[str]:
    """校验 + 落库 + 热更。返回生效后的列表。"""
    global _channels
    names = normalize(value)
    with _lock:
        if _db_path is None:
            raise RuntimeError("TG 配置存储未初始化")
        with _session() as conn:
            conn.execute(
                "INSERT INTO tg_settings(key, value, updated_ts) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_ts=excluded.updated_ts",
                (KEY_CHANNELS, json.dumps(names, ensure_ascii=False), time.time()),
            )
        _channels = names
    log.info("TG 频道配置已更新：%s", names or "（空，已禁用）")
    return list(names)


def clear() -> None:
    """删掉覆盖值，回到 .env 的 TG_CHANNELS。"""
    global _channels
    with _lock:
        if _db_path is None:
            raise RuntimeError("TG 配置存储未初始化")
        with _session() as conn:
            conn.execute("DELETE FROM tg_settings WHERE key=?", (KEY_CHANNELS,))
        _channels = None
    log.info("TG 频道配置已恢复默认（TG_CHANNELS）")


@contextmanager
def _session():
    """每次新开连接：调用方 with 完必须关掉，不关在 Windows 上会撞锁。"""
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _reset_state() -> None:
    """仅供单测：把全局状态清回未初始化。"""
    global _db_path, _channels
    with _lock:
        _db_path = None
        _channels = None
