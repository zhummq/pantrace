"""搜索缓存：L1 内存 + L2 SQLite，带 `complete` 语义。

对应 pansou 的 `util/cache/` 与 `service/search_service.go` 的
`writeFinalMainCache` / `mergeIntoMainCache`。

## 三条来自实测的硬规则（不要"优化"掉）

1. **`complete=False` 的条目不算命中完整结果**。4 秒超时返回的空结果必须能被
   下次请求识别出来重抓，否则它会被当权威缓存钉住一整个 TTL。
2. **写入必须走 `merge_put`（读 → 合并 → 写回），不是 `put` 覆盖**。
   pansou 的实测：后台补齐用覆盖写时，受控场景只剩 13 条、**丢掉 74% 的并发结果**。
3. **整批零产出不写缓存**（判定在 `base.py`，这里只提供不写的出口）。
   "全部成功但零产出"会被完整度判定误认为"完整"，于是源改版后的空结果被缓存一小时，
   而所有健康检查都是绿的。

## 为什么用同步 sqlite3

读-改-写中间一旦有 `await`，两个协程就会读到同一份旧值、各自并进自己那部分再写回，
先完成的那个结果消失。同步实现没有让出点，天然原子。表只有几 MB，毫秒级操作，
阻塞代价远小于这个 bug 的代价。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .types import CachedResponse, Link, SearchResult

log = logging.getLogger("pantrace.search.cache")

# pansou 的 defaultCacheTTL = 1h。它的实测结论是"缓存不死、后台合并往里加"
# 才让结果变多，缩短 TTL 重搜反而更少（-32 / -72 条）。所以 TTL 别调小。
DEFAULT_TTL = 3600.0
# 过期后仍保留一段宽限期，供"失败回退旧缓存"使用
GRACE = 1800.0
DEFAULT_MAX_ENTRIES = 2048


def merge_results(*groups: list[SearchResult]) -> list[SearchResult]:
    """按 key 去重，同 key 保留完整度更高的那个。

    对应 pansou 的 `mergeSearchResults`。不是"保留先到的"：先到的往往是信息最少的。
    """
    best: dict[str, SearchResult] = {}
    for group in groups:
        for r in group:
            key = r.result_key()
            current = best.get(key)
            if current is None or r.completeness_score() > current.completeness_score():
                best[key] = r
    return list(best.values())


def _to_json(results: list[SearchResult]) -> str:
    return json.dumps([asdict(r) for r in results], ensure_ascii=False, separators=(",", ":"))


def _from_json(payload: str) -> list[SearchResult]:
    out: list[SearchResult] = []
    for raw in json.loads(payload):
        links = [Link(**ln) for ln in raw.get("links") or []]
        out.append(
            SearchResult(
                unique_id=raw.get("unique_id", ""),
                message_id=raw.get("message_id", ""),
                channel=raw.get("channel", ""),
                title=raw.get("title", ""),
                content=raw.get("content", ""),
                datetime=raw.get("datetime", 0.0) or 0.0,
                links=links,
                tags=raw.get("tags") or [],
                images=raw.get("images") or [],
                # 老缓存（extras 上线前写入的）没有这个键，要给默认值，
                # 否则重启后读到旧条目会 KeyError，整条缓存被当成损坏丢掉。
                extras=raw.get("extras") or {},
            )
        )
    return out


class SearchCache:
    """两级缓存。L1 挡热词，L2 挡重启。"""

    def __init__(self, db_path: str | Path | None = None, ttl: float = DEFAULT_TTL, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.ttl = ttl
        self.max_entries = max_entries
        # 全局锁：读-改-写必须整段互斥。粒度粗，但表小、正确性优先。
        self._lock = threading.Lock()
        self._l1: dict[str, CachedResponse] = {}
        self._db: sqlite3.Connection | None = None
        if db_path:
            self._init_db(Path(db_path))

    # ---------- L2 初始化 ----------

    def _init_db(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS search_cache (
              cache_key   TEXT PRIMARY KEY,
              payload     TEXT NOT NULL,
              complete    INTEGER DEFAULT 0,
              created_at  REAL NOT NULL,
              last_access REAL NOT NULL,
              access_cnt  INTEGER DEFAULT 0
            )
            """
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON search_cache(created_at)")
        self._db.commit()
        log.info("搜索缓存已就绪：%s", path)

    # ---------- 读 ----------

    def get(self, key: str, now: float | None = None) -> CachedResponse | None:
        """读缓存。不判断是否过期 —— 过期与否由调用方决定（它还要看 complete）。"""
        now = now if now is not None else time.time()
        with self._lock:
            entry = self._l1.get(key)
            if entry is None and self._db is not None:
                entry = self._load_l2(key)
                if entry is not None:
                    self._l1[key] = entry
            if entry is None:
                return None
            entry.access_count += 1
            entry.last_access = now
            if self._db is not None:
                self._db.execute(
                    "UPDATE search_cache SET access_cnt = access_cnt + 1, last_access = ? WHERE cache_key = ?",
                    (now, key),
                )
                self._db.commit()
            return entry

    def _load_l2(self, key: str) -> CachedResponse | None:
        assert self._db is not None
        row = self._db.execute(
            "SELECT payload, complete, created_at, last_access, access_cnt FROM search_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        payload, complete, created_at, last_access, access_cnt = row
        try:
            results = _from_json(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("缓存反序列化失败，按未命中处理 %s: %s", key, exc)
            return None
        return CachedResponse(
            results=results,
            timestamp=created_at,
            complete=bool(complete),
            last_access=last_access,
            access_count=access_cnt,
        )

    # ---------- 写 ----------

    def put(self, key: str, results: list[SearchResult], complete: bool, now: float | None = None) -> None:
        """直接覆盖写。只用于"这一轮结果确实更完整、无需保留旧值"的场景。

        默认应该用 `merge_put`。覆盖写是那个 74% 事故的成因，这里保留只是为了
        显式表达"我知道在覆盖什么"。
        """
        now = now if now is not None else time.time()
        with self._lock:
            self._store(key, results, complete, now)

    def merge_put(self, key: str, results: list[SearchResult], complete: bool, now: float | None = None) -> int:
        """读 → 合并 → 写回。返回合并后的条数。

        后台补齐必须走这条，对应 pansou 的 `writeFinalMainCache`。
        """
        now = now if now is not None else time.time()
        with self._lock:
            existing = self._l1.get(key)
            if existing is None and self._db is not None:
                existing = self._load_l2(key)
            merged = results
            access_count = 1
            last_access = now
            if existing is not None and existing.results:
                merged = merge_results(existing.results, results)
                access_count = existing.access_count
                last_access = existing.last_access
            self._store(key, merged, complete, now, access_count, last_access)
            return len(merged)

    def _store(
        self,
        key: str,
        results: list[SearchResult],
        complete: bool,
        now: float,
        access_count: int = 1,
        last_access: float = 0.0,
    ) -> None:
        entry = CachedResponse(
            results=results,
            timestamp=now,
            complete=complete,
            last_access=last_access or now,
            access_count=access_count,
        )
        self._l1[key] = entry
        if self._db is not None:
            self._db.execute(
                """
                INSERT INTO search_cache (cache_key, payload, complete, created_at, last_access, access_cnt)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  payload = excluded.payload,
                  complete = excluded.complete,
                  created_at = excluded.created_at,
                  last_access = excluded.last_access,
                  access_cnt = excluded.access_cnt
                """,
                (key, _to_json(results), 1 if complete else 0, now, entry.last_access, access_count),
            )
            self._db.commit()

    # ---------- 维护 ----------

    def cleanup(self, now: float | None = None) -> int:
        """清掉超过 TTL + 宽限期的条目。返回删除条数。"""
        now = now if now is not None else time.time()
        cutoff = now - (self.ttl + GRACE)
        removed = 0
        with self._lock:
            for key in [k for k, v in self._l1.items() if v.timestamp < cutoff]:
                self._l1.pop(key, None)
                removed += 1
            if self._db is not None:
                cur = self._db.execute("DELETE FROM search_cache WHERE created_at < ?", (cutoff,))
                self._db.commit()
                removed = max(removed, cur.rowcount or 0)
            # 总量超限时淘汰最久未访问的
            if len(self._l1) > self.max_entries:
                keep = sorted(self._l1.items(), key=lambda kv: kv[1].last_access, reverse=True)[: self.max_entries]
                self._l1 = dict(keep)
        return removed

    def stats(self) -> dict[str, int]:
        return {"l1_entries": len(self._l1)}

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
