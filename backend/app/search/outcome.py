"""批次结果统计：缓存写入判定 + 后台补齐判定。

对应 pansou `service/batch_search.go` 的 `batchSearchOutcome`
（`cacheTTL` / `shouldBackfill` / `complete`）。

这个类存在的唯一理由，是把三条**看起来能做、实际会出事**的判定集中到一处：

1. **全失败不写缓存** —— 没成功过就没有可信内容
2. **整批零产出不写缓存** —— "全部成功但一条都没出"会被 `complete` 判成完整，
   于是源改版后的空结果被缓存一整个 TTL，而所有健康检查都是绿的。这是最容易踩的坑。
3. **超时超过 1/3 不补齐** —— 缺失比例过高说明上游整体变慢，此时补齐只会把
   请求量再放大一倍，雪上加霜。

别把这三条挪进各处调用点。pansou 有两处路径各写一份，后来漂移过。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BatchOutcome:
    total: int = 0
    succeeded: int = 0                 # 本轮正常跑完（含零产出）
    failed: int = 0                    # 明确报错
    yielded: int = 0                   # 实际贡献的可用条目数
    missing: list[str] = field(default_factory=list)   # 超时未完成 / 未返回
    empty: list[str] = field(default_factory=list)     # 跑完但零产出
    last_errors: dict[str, str] = field(default_factory=dict)
    _observed: set[str] = field(default_factory=set)   # 已经上报过的源，避免 finalize 重复计入

    def observe(self, source: str, results_count: int, error: BaseException | None, timed_out: bool = False) -> None:
        self._observed.add(source)
        if timed_out:
            self.missing.append(source)
            return
        if error is not None:
            self.failed += 1
            self.last_errors[source] = str(error)[:120]
            return
        self.succeeded += 1
        self.yielded += results_count
        if results_count == 0:
            self.empty.append(source)

    def finalize(self, submitted: list[str]) -> None:
        """补齐"提交了但一次都没上报"的——它们才是真正的超时未完成。

        这里必须用 `_observed` 判断，不能只看 missing/errors/empty：
        成功返回的源不在这三个集合里，按那三个集合判断会把每一轮正常返回的源
        全算成"超时"，于是 should_backfill 的比例门永远触发不了（超时数=总数）。
        """
        self.total = max(self.total, len(submitted))
        for name in submitted:
            if name not in self._observed:
                self.missing.append(name)

    # ---------- 三条判定 ----------

    @property
    def timed_out(self) -> int:
        return len(self.missing)

    def complete(self) -> bool:
        """注意：这个判定**只看超时和失败，不看产出**。

        所以它单独用会漏掉"全部成功但零产出"—— 那正是下面 cache_decision 要补的。
        """
        return not self.missing and self.failed == 0

    def cache_decision(self, require_yield: bool = False) -> tuple[bool, str]:
        """本轮结果该不该写缓存。返回 (是否写, 原因)。

        对应 pansou 的 `cacheTTL`，两档 TTL 已按它的实测结论删掉（那一档换来的收益
        约为零，反而让条目存活时间在 0.2 秒和 30 秒之间跳动）。
        """
        if self.succeeded == 0:
            return False, "全部失败，没有可信内容"
        if require_yield and self.yielded == 0:
            # 全成功但零产出：多半是源改版后返回 200 + 空列表。
            # 写进去会把空结果钉住一整个 TTL。
            return False, "整批零产出，不写缓存（疑似源改版）"
        return True, "ok"

    def should_backfill(self, enabled: bool) -> bool:
        """值不值得后台补齐。对应 `shouldBackfill`。"""
        if not enabled or self.succeeded == 0 or self.total == 0 or self.timed_out == 0:
            return False
        # 超时超过三分之一就不补：上游整体变慢时，补齐把请求量再放大一倍
        return self.timed_out * 3 <= self.total

    def summary(self) -> str:
        return (
            f"成功 {self.succeeded}/{self.total}，失败 {self.failed}，"
            f"零产出 {len(self.empty)}，超时未完成 {self.timed_out}，"
            f"产出 {self.yielded} 条"
        )
