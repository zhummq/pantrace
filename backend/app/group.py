"""群入口配置：进站弹窗 / 获取链接弹窗的二维码与群号。

存 SQLite（和埋点同一个库），一张 key-value 表，键固定为 "group"。
管理员在 /admin 看板里改，前端每次进站拉 /api/group——
改二维码、换群号、关弹窗都不需要改代码、不需要重新构建。

设计上的两个克制：
- 群最多 4 个（一个弹窗塞不下更多）。
- 二维码图片走上传接口落到 backend/data/uploads/，URL 只允许
  站内路径或 https://，挡掉 javascript: 之类的伪协议。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

CONFIG_KEY = "group"
MAX_GROUPS = 4
MAX_TEXT = 120
GROUP_TYPES = ("qq", "wechat", "other")
TYPE_NAMES = {"qq": "QQ 群", "wechat": "微信群", "other": "其他"}

# 站内路径（后端托管的 /uploads/... 或前端 public 里的图）或 https 外链
_SAFE_URL = re.compile(r"^(https?://\S+|/\S+)$", re.IGNORECASE)

DEFAULT: dict[str, Any] = {
    "enabled": True,
    "title": "盘迹资源交流群",
    "desc": "搜不到、链接失效、想找新资源——进群说一声，有人给。",
    "tip": "手机 QQ 扫一扫二维码即可加入",
    "popup": {
        "enter": True,     # 进站弹一次
        "acquire": True,   # 点「打开链接」时拦截弹一次
        "delay": 800,      # 进站后延迟多少毫秒弹
        "once": "session",  # session=每个会话一次 / day=每天一次 / always=每次进站都弹
    },
    "groups": [
        {
            "type": "qq",
            "name": "盘迹资源交流群",
            "number": "1135204145",
            "qr": "/group-qq.jpg",  # 默认图在 frontend/public，构建时进 dist
            "link": "",             # 手机端一键加群链接（选填，QM 加群链接）
            "note": "",
            "enabled": True,
        }
    ],
}


def _clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _clean_url(value: Any) -> str:
    value = _clean_text(value, 500)
    if not value:
        return ""
    if not _SAFE_URL.match(value):
        return ""
    return value


def _validate(cfg: dict) -> dict:
    """裁字段、补默认值。宽松处理：坏字段清空而不是整体报错，
    管理员填错一个群号不该让整个看板保存失败。"""
    out = {
        "enabled": bool(cfg.get("enabled", True)),
        "title": _clean_text(cfg.get("title"), 40) or DEFAULT["title"],
        "desc": _clean_text(cfg.get("desc"), 120),
        "tip": _clean_text(cfg.get("tip"), 80),
    }
    popup = cfg.get("popup") if isinstance(cfg.get("popup"), dict) else {}
    once = popup.get("once") or "session"
    # 注意别写 `popup.get("delay") or 800`：填 0（立刻弹）会变成 800
    try:
        delay = int(popup.get("delay", 800))
    except (TypeError, ValueError):
        delay = 800
    out["popup"] = {
        "enter": bool(popup.get("enter", True)),
        "acquire": bool(popup.get("acquire", True)),
        "delay": min(max(delay, 0), 10000),
        "once": once if once in ("session", "day", "always") else "session",
    }

    groups = []
    for raw in (cfg.get("groups") or [])[:MAX_GROUPS]:
        if not isinstance(raw, dict):
            continue
        gtype = raw.get("type") if raw.get("type") in GROUP_TYPES else "qq"
        groups.append(
            {
                "type": gtype,
                "name": _clean_text(raw.get("name"), 40) or TYPE_NAMES[gtype],
                "number": _clean_text(raw.get("number"), 40),
                "qr": _clean_url(raw.get("qr")),
                "link": _clean_url(raw.get("link")),
                "note": _clean_text(raw.get("note"), 60),
                "enabled": bool(raw.get("enabled", True)),
            }
        )
    out["groups"] = groups
    return out


class GroupStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS app_config ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            conn.commit()
            self._conn = conn
        return self._conn

    def init(self) -> None:
        conn = self._connect()
        row = conn.execute(
            "SELECT value FROM app_config WHERE key=?", (CONFIG_KEY,)
        ).fetchone()
        if row is None:
            self.save(DEFAULT)
            return
        # 老配置缺字段时用默认值补齐，避免前端拿到半截结构
        try:
            merged = _validate({**DEFAULT, **json.loads(row[0])})
        except Exception:
            merged = DEFAULT
        conn.execute(
            "UPDATE app_config SET value=? WHERE key=?",
            (json.dumps(merged, ensure_ascii=False), CONFIG_KEY),
        )
        conn.commit()

    def load(self) -> dict:
        row = self._connect().execute(
            "SELECT value FROM app_config WHERE key=?", (CONFIG_KEY,)
        ).fetchone()
        if row is None:
            return dict(DEFAULT)
        try:
            return _validate({**DEFAULT, **json.loads(row[0])})
        except Exception:
            return dict(DEFAULT)

    def save(self, cfg: dict) -> dict:
        cleaned = _validate(cfg)
        conn = self._connect()
        conn.execute(
            "INSERT INTO app_config (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (CONFIG_KEY, json.dumps(cleaned, ensure_ascii=False), time.time()),
        )
        conn.commit()
        return cleaned

    def public(self) -> dict:
        """给前端的版本：结构同 load()，无需再过滤——里面本来就没有敏感字段。"""
        return self.load()


STORE: GroupStore | None = None


def init(db_path: str) -> None:
    global STORE
    STORE = GroupStore(db_path)
    STORE.init()


def store() -> GroupStore:
    if STORE is None:
        raise RuntimeError("group 模块未初始化")
    return STORE
