"""TG 阶段的可达性门。

逐行翻译 pansou `service/tg_reachability.go`（241 行）。

背景（pansou 注释里的实测结论，对我们同样成立）：t.me 在不通的网络里不是"连接被拒绝"，
而是"连接被静默丢包"，于是所有频道请求全部挂满超时——整阶段稳定耗满 4 秒，换来的结果恒为 0 条。

三条硬约束，缺一不可：
  1. **探测只在后台跑，搜索路径只读状态**。结论过期时也只读旧结论（fail-open），绝不阻塞。
  2. **门关时早返回，且不写缓存**。网络不通导致的空结果若写进 TTL 缓存，网络恢复后会继续骗人。
  3. **拿不准一律按"可达"处理**。门只用来省掉必然失败的请求。

判定依据是"能否拿到 HTTP 响应"而不是状态码：404 同样证明网络通，只有传输层失败才算不可达。
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

# tg_reachability.go:37-53
TG_PROBE_BUDGET = 3.0          # 单次探测总预算。实测跨洋握手+响应头偶尔超 1.5 秒，放到 3 秒
TG_REACHABLE_TTL = 300.0       # 可达结论保鲜期
TG_UNREACHABLE_TTL = 60.0      # 不可达结论保鲜期：故意取短，恢复后一分钟内回来自动转正常
TG_PROBE_CHECK_INTERVAL = 3.0  # 后台重探检查间隔
TG_PROBE_REQUIRED_FAILURES = 2 # 连续失败多少次才敢关门

# 探测用 HEAD 而不是 GET：只取响应头，不下载正文。频道页正文有 133KB，
# 探测只需要"能不能拿到响应"这一个比特。
TG_PROBE_UA = "Mozilla/5.0 (compatible; PanTrace-TGProbe)"


class TGReachability:
    """全局可达性状态。对应 Go 里那组 atomic 变量 + sync.Mutex。"""

    def __init__(self) -> None:
        # fail-open：默认可达，未探测过或结论过期都按可达处理
        self.reachable = True
        self.checked_at = 0.0
        self.reason = "尚未探测（按可达处理）"
        # 连续探测失败次数：达到 TG_PROBE_REQUIRED_FAILURES 才敢把门关上
        self.fail_streak = 0

        self._logged_once = False
        self._last_logged_ok = False
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------ 只读接口

    def is_reachable(self) -> bool:
        """报告 TG 阶段当前是否值得执行。只读内存，无 IO，可在请求路径上直接调用。"""
        return self.reachable

    def snapshot(self) -> dict:
        """供 /api/health 暴露，便于一眼看出当前空结果是不是网络导致的。"""
        return {
            "reachable": self.reachable,
            "reason": self.reason,
            "fail_streak": self.fail_streak,
            "required_failures": TG_PROBE_REQUIRED_FAILURES,
            "checked_seconds_ago": (
                int(time.monotonic() - self.checked_at) if self.checked_at > 0 else None
            ),
        }

    def is_fresh(self) -> bool:
        """tg_reachability.go:135 `tgReachabilityFresh`。"""
        if self.checked_at == 0:
            return False
        if not self.reachable or self.fail_streak > 0:
            ttl = TG_UNREACHABLE_TTL
        else:
            ttl = TG_REACHABLE_TTL
        return (time.monotonic() - self.checked_at) < ttl

    # ------------------------------------------------------------ 后台探测

    def start(self) -> asyncio.Task:
        """启动后台探测循环。重复调用无副作用（对应 `sync.Once`）。

        事件循环会被换（测试里常见），所以换循环时重建任务；同一循环内只启动一次。
        """
        loop = asyncio.get_running_loop()
        if self._task is not None and not self._task.done() and self._loop is loop:
            return self._task
        if self._task is not None and not self._task.done():
            # 旧循环的探测任务已经没人调度了，直接丢弃引用
            self._task.cancel()
        self._loop = loop
        logger.info(
            "[TG可达性] 后台探测已启动：可达结论保鲜 %.0fs、不可达结论保鲜 %.0fs；搜索路径只读结论不阻塞",
            TG_REACHABLE_TTL,
            TG_UNREACHABLE_TTL,
        )
        self._task = loop.create_task(self._probe_loop())
        return self._task

    async def _probe_loop(self) -> None:
        """tg_reachability.go:84 `tgProbeLoop`。"""
        await self.probe_once()

        while True:
            await asyncio.sleep(TG_PROBE_CHECK_INTERVAL)
            # 正在确认失败（连击未达阈值）时尽快复检，让确认过程几秒内结束；
            # 否则按结论保鲜期决定是否重探。
            if 0 < self.fail_streak < TG_PROBE_REQUIRED_FAILURES:
                await self.probe_once()
                continue
            if self.is_fresh():
                continue
            await self.probe_once()

    async def probe_once(self, client: httpx.AsyncClient | None = None, url: str = "") -> bool:
        """tg_reachability.go:147 `probeTGReachability`。

        单飞：后台循环与手动触发可能同时到达，没必要并发探测同一件事。
        """
        # 单飞（对应 Go 的 tgProbeMu）
        async with _probe_lock():
            target = url or tg_probe_url()
            reachable, reason = await probe_tg_url(client, target)
            self.checked_at = time.monotonic()
            self.reason = reason

            if reachable:
                self.fail_streak = 0
                self.reachable = True
                self._log_result(True, reason)
                return True

            # 失败：先记连击，未达阈值就保持上一次结论（fail-open），不因单次超时关掉 TG。
            self.fail_streak += 1
            if self.fail_streak < TG_PROBE_REQUIRED_FAILURES:
                self.reason = (
                    f"{reason}（连续失败 {self.fail_streak}/{TG_PROBE_REQUIRED_FAILURES}，暂不改判）"
                )
                logger.warning(
                    "[TG可达性] 探测失败 %d/%d，暂不改判，%.0fs 后复检：%s",
                    self.fail_streak,
                    TG_PROBE_REQUIRED_FAILURES,
                    TG_PROBE_CHECK_INTERVAL,
                    reason,
                )
                return self.reachable

            self.reachable = False
            confirmed = f"连续 {self.fail_streak} 次探测失败：{reason}"
            self.reason = confirmed
            self._log_result(False, confirmed)
            return False

    def _log_result(self, reachable: bool, reason: str) -> None:
        """tg_reachability.go:181。首次结论与翻转各打一次，周期性重复结论不打。"""
        if not self._logged_once:
            self._logged_once = True
            self._last_logged_ok = reachable
            if reachable:
                logger.info("[TG可达性] 首次探测：可达（%s）", reason)
            else:
                logger.warning(
                    "[TG可达性] 首次探测：不可达，TG 阶段将跳过（不写缓存、不计频道失败）：%s", reason
                )
            return
        if self._last_logged_ok == reachable:
            return
        self._last_logged_ok = reachable
        if reachable:
            logger.info("[TG可达性] 已恢复可达：%s", reason)
        else:
            logger.warning(
                "[TG可达性] 转为不可达，TG 阶段将跳过（不写缓存、不计频道失败）：%s", reason
            )


_PROBE_LOCK: asyncio.Lock | None = None
_PROBE_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


def _probe_lock() -> asyncio.Lock:
    """按事件循环懒建锁。

    asyncio.Lock 不能跨事件循环复用（会抛 "got Future attached to a different loop"），
    而测试里每个用例都是新的 loop。Go 的 sync.Mutex 没有这个约束，
    所以这里换成"换循环就重建"，语义等价（同一时刻只允许一次探测）。
    """
    global _PROBE_LOCK, _PROBE_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _PROBE_LOCK is None or _PROBE_LOCK_LOOP is not loop:
        _PROBE_LOCK = asyncio.Lock()
        _PROBE_LOCK_LOOP = loop
    return _PROBE_LOCK
TG = TGReachability()


def tg_probe_url(first_channel: str = "") -> str:
    """tg_reachability.go:205 `tgProbeURL`。

    用真实频道页而不是站点首页——"首页能开但 /s/ 路径被单独阻断"是可能发生的，
    探测必须覆盖真正要走的路径。
    """
    if not first_channel:
        return "https://t.me/"
    return f"https://t.me/s/{first_channel}"


async def probe_tg_url(client: httpx.AsyncClient | None, target: str) -> tuple[bool, str]:
    """tg_reachability.go:223 `probeTGURL`。

    判定标准是"拿到了 HTTP 响应"而不是状态码：404/403/405 都说明网络通，
    只有传输层失败（DNS、连接、TLS、超时）才算不可达。
    """
    own_client = False
    if client is None:
        client = httpx.AsyncClient(
            timeout=TG_PROBE_BUDGET,
            trust_env=False,       # 和项目 use_env_proxy=False 对齐，见 base.build_clients
            follow_redirects=True,
        )
        own_client = True

    try:
        resp = await client.head(target, headers={"User-Agent": TG_PROBE_UA})
        # HEAD 没有正文，但仍要关闭响应体，否则连接无法复用
        await resp.aclose()
        return True, f"探测 {target} 返回 HTTP {resp.status_code}"
    except httpx.InvalidURL as exc:
        # 对应 Go 的 NewRequest 失败：URL 是我们自己拼的，出错说明配置有问题，按可达处理
        return True, f"构造探测请求失败，按可达处理: {exc}"
    except Exception as exc:  # 传输层失败：DNS / 连接 / TLS / 超时
        return False, f"探测 {target} 失败: {type(exc).__name__}: {exc}"
    finally:
        if own_client:
            await client.aclose()
