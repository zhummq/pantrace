"""盘迹 PanTrace · AstrBot 网盘资源搜索插件。

调用 PanTrace 后端 /api/search，把结果格式化成适合 QQ 聊天阅读的短列表。
后端负责签名/风控/转存，本插件只做「转发 + 排版 + 缓存」，不碰任何凭据。
"""

import asyncio
import time
from collections import OrderedDict

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 平台 key -> 中文名（与后端 main.py 的 PLATFORMS 一致，仅用于展示兜底）
PLATFORM_NAMES = {
    "quark": "夸克", "baidu": "百度", "ali": "阿里", "uc": "UC",
    "xunlei": "迅雷", "115": "115", "123": "123", "tianyi": "天翼",
    "yidong": "移动", "wo": "联通", "guangya": "光鸭", "lanzou": "蓝奏",
    "weiyun": "微云", "ctfile": "城通", "pikpak": "PikPak", "magnet": "磁力",
}
# 常用平台别名，方便用户输入
PLATFORM_ALIASES = {
    "夸克": "quark", "百度": "baidu", "阿里": "ali", "阿里云": "ali",
    "uc": "uc", "迅雷": "xunlei", "123": "123", "天翼": "tianyi",
    "移动": "yidong", "联通": "wo", "光鸭": "guangya", "蓝奏": "lanzou",
    "微云": "weiyun", "城通": "ctfile",
}
VALID_KEYS = set(PLATFORM_NAMES)


class LRUCache:
    """带 TTL 的 LRU 缓存。上游很慢且有限流，缓存是可用性底线。"""

    def __init__(self, ttl: int, maxsize: int):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> dict | None:
        async with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            ts, value = item
            if time.time() - ts > self.ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return dict(value)

    async def set(self, key: str, value: dict) -> None:
        async with self._lock:
            self._data[key] = (time.time(), dict(value))
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)


class PanTracePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._cache = LRUCache(
            ttl=int(config.get("cache_ttl", 300)),
            maxsize=int(config.get("cache_maxsize", 200)),
        )
        # 单飞锁：同一关键词并发只放一个请求打上游，其余等待复用结果
        self._inflight: dict[str, asyncio.Lock] = {}
        self._inflight_guard = asyncio.Lock()

    def _base_url(self) -> str:
        return str(self.config.get("base_url", "")).strip().rstrip("/")

    def _limit(self) -> int:
        return max(1, min(10, int(self.config.get("result_limit", 6))))

    def _timeout(self) -> int:
        return max(10, min(120, int(self.config.get("timeout", 80))))

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._inflight_guard:
            lock = self._inflight.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._inflight[key] = lock
            return lock

    def _normalize_platforms(self, text: str) -> tuple[list[str], str]:
        """解析平台参数，返回 (key列表, 未识别的词)。"""
        if not text:
            return [], ""
        keys, unknown = [], []
        for token in text.replace("，", ",").split(","):
            token = token.strip().lower()
            if not token:
                continue
            mapped = PLATFORM_ALIASES.get(token, token)
            if mapped in VALID_KEYS:
                if mapped not in keys:
                    keys.append(mapped)
            else:
                unknown.append(token)
        return keys, ",".join(unknown)

    async def _search_api(
        self, query: str, platforms: list[str], page: int
    ) -> tuple[dict | None, str]:
        """调后端搜索接口。返回 (body, 错误文案)。"""
        base = self._base_url()
        if not base:
            return None, "插件还没配置 PanTrace 服务地址（base_url），请在插件配置里填写。"
        url = f"{base}/api/search"
        params = {"q": query, "page": str(page), "page_size": str(self._limit())}
        if platforms:
            params["platforms"] = ",".join(platforms)

        timeout = aiohttp.ClientTimeout(total=self._timeout())
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, params=params, headers={"User-Agent": "AstrBot-PanTrace/1.0"}
                ) as resp:
                    if resp.status != 200:
                        try:
                            err = await resp.json()
                            msg = err.get("msg", "")
                        except Exception:
                            msg = await resp.text()
                        return None, f"搜索服务返回 {resp.status}：{str(msg)[:120]}"
                    data = await resp.json()
        except asyncio.TimeoutError:
            return None, "搜索超时了，上游这次太慢。过十几秒再试一次。"
        except aiohttp.ClientError as exc:
            return None, f"连不上搜索服务：{type(exc).__name__}。检查 base_url 和服务是否在线。"
        except Exception as exc:  # noqa: BLE001
            logger.warning("pantrace search error: %s", exc)
            return None, f"搜索出错了：{str(exc)[:100]}"

        if data.get("code") != 0:
            return None, f"搜索失败：{str(data.get('msg', '未知错误'))[:120]}"
        return data, ""

    def _human_size(self, size_bytes: int) -> str:
        if not size_bytes or size_bytes <= 0:
            return ""
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(size_bytes)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.0f}{unit}" if unit in ("B", "KB") else f"{size:.1f}{unit}"
            size /= 1024
        return ""

    def _abs_url(self, raw: str) -> str:
        """把 /go/xxx 相对路径拼成绝对地址；本就是 http(s) 的原样返回。"""
        raw = (raw or "").strip()
        if not raw:
            return ""
        if raw.startswith(("http://", "https://")):
            return raw
        base = self._base_url()
        if raw.startswith("/") and base:
            return base + raw
        return raw

    def _item_url(self, item: dict) -> str:
        """聊天展示用链接：/go/<token> 拼成绝对地址。长链由 Markdown 藏在
        [点我打开](url) 后面，聊天窗里只显示四个字，不需要后端短链。"""
        return self._abs_url(item.get("shareUrl") or "")

    def _format(self, query: str, body: dict, page: int, platforms: list[str]) -> str:
        """QQ Markdown 展示格式（用户指定样式，照截图还原）：

        🔍 尸兄 · 1000 条

        尸兄 点我打开
        夸克网盘　18.7GB

        尸兄 点我打开
        夸克网盘　18.7GB
        ...

        ——
        下一页 /搜 尸兄 3 · 打开后请先转存到自己的网盘 · 链接约 30 分钟有效

        每条独立展示：加粗标题 + [点我打开] 链接 + 平台/大小一行，条目间空行。
        不做分组、不加序号——用户实测这条列感最清楚。长链藏在 markdown 链接
        目标里，聊天窗只显示「点我打开」。Markdown 不被允许时框架降级纯文本。
        """
        items = body.get("items") or []
        pagination = body.get("pagination") or {}
        total = pagination.get("total", 0)
        total_pages = pagination.get("totalPages", 0)
        cached = body.get("cached", False)

        # 头部：🔍 关键词 · 数量 · 平台
        header = f"🔍 {query}"
        if total:
            header += f"　·　{total} 条"
        if platforms:
            header += f"　·　{'/'.join(PLATFORM_NAMES.get(p, p) for p in platforms)}"
        lines: list[str] = [header]

        if not items:
            lines.extend(["", "这次没搜到。换个关键词，或去掉平台筛选再试。"])
            return "\n".join(lines)

        # 每条：标题 + 点我打开 / 平台 · 大小 / 提取码（如有）
        for item in items:
            title = (item.get("title") or "未命名").strip()
            if len(title) > 46:
                title = title[:44] + "…"
            platform = item.get("platformName") or PLATFORM_NAMES.get(item.get("platform", ""), "")
            size = self._human_size(item.get("sizeBytes", 0))
            meta = "　".join(p for p in (platform, size) if p)
            pwd = (item.get("sharePwd") or "").strip()
            url = self._item_url(item)

            label = f"**{title}**"
            if url:
                label += f" [点我打开]({url})"
            lines.extend(["", label])
            if meta:
                lines.append(meta)
            if pwd:
                lines.append(f"提取码：{pwd}")

        # 尾部：分隔线 + 分页 + 操作提示
        lines.append("")
        lines.append("———")
        tips = []
        if total_pages and page < total_pages:
            nxt = page + 1
            cmd = f"/搜 {query}"
            if platforms:
                cmd += " " + ",".join(platforms)
            cmd += f" {nxt}"
            tips.append(f"下一页 {cmd}")
        tips.append("打开后请先转存到自己的网盘 · 链接约 30 分钟有效")
        if cached:
            tips.append("缓存")
        lines.append("　·　".join(tips))
        return "\n".join(lines)

    @filter.command("搜", alias={"search", "网盘", "找资源"})
    async def search(
        self,
        event: AstrMessageEvent,
        query: str = "",
        arg2: str = "",
        arg3: str = "",
    ):
        """网盘资源搜索。用法：/搜 关键词 [平台] [页码]
        例：/搜 三体      /搜 三体 夸克,百度      /搜 三体 夸克 2
        """
        if not query.strip():
            yield event.plain_result(
                "用法：/搜 关键词 [平台] [页码]\n"
                "例：/搜 三体\n"
                "    /搜 三体 夸克,百度\n"
                "    /搜 三体 夸克 2\n"
                "不填平台搜全部，页码默认第 1 页。"
            )
            return

        query = query.strip()
        event.stop_event()

        # 解析 arg2 / arg3：哪个是平台、哪个是页码
        platforms: list[str] = []
        page = 1
        for token in (arg2, arg3):
            if not token:
                continue
            if token.isdigit():
                page = max(1, min(100, int(token)))
            else:
                keys, _unknown = self._normalize_platforms(token)
                platforms.extend(keys)

        cache_key = f"{query}|{','.join(platforms)}|{page}|{self._limit()}"
        cached = await self._cache.get(cache_key)
        if cached:
            yield event.plain_result(self._format(query, cached, page, platforms))
            return

        lock = await self._get_lock(cache_key)
        async with lock:
            # 双检：等锁期间别人可能已经填好缓存
            cached = await self._cache.get(cache_key)
            if cached:
                yield event.plain_result(self._format(query, cached, page, platforms))
                return

            body, err = await self._search_api(query, platforms, page)
            if err:
                yield event.plain_result(f"❌ {err}")
                return
            await self._cache.set(cache_key, body)
            yield event.plain_result(self._format(query, body, page, platforms))

    @filter.command("搜帮助")
    async def search_help(self, event: AstrMessageEvent):
        """查看网盘搜索插件的用法。"""
        yield event.plain_result(
            "📖 盘迹网盘搜索\n"
            "/搜 关键词 —— 搜索全部平台\n"
            "/搜 关键词 平台 —— 指定平台（夸克,百度,阿里…）\n"
            "/搜 关键词 平台 页码 —— 翻页\n"
            "群聊直接发命令即可；私聊同样可用。\n"
            "链接点击后在网页里打开，请先转存到自己的网盘。"
        )

    async def terminate(self):
        """插件卸载/重载时清理会话资源。"""
        pass
