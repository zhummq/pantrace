"""盘迹 PanTrace · AstrBot 插件。

两件事：

1. **搜索**（`/搜`）—— 调用后端 `/api/search`，把结果排成适合 QQ 阅读的短列表。
2. **采集**（静默）—— 监听指定群里群主/管理员发的分享消息，提取网盘链接
   上报给后端 `/api/ingest/qq` 入库。

采集链路里，插件先让 AstrBot 已配置的大模型把消息整理成
`{title, shareUrl, sharePwd}`，再由后端核对入库。

但**链接的唯一可信来源永远是原文**：插件不写 URL 正则、不猜 share id，
后端会拿 `search/clean.py` 的正则结果核对每一条 AI 条目，对不上
`platform:pwd_id` 的直接丢弃，AI 漏掉的链接也会被补回来。所以模型只
负责"标题写得更像人话"，不可能把死链、编造的链接带进库。
"""

import asyncio
import inspect
import json
import re
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

# 网盘域名。采集侧只用它做一次粗筛：消息里没有这些域名就完全不上报。
# 为什么不复用后端的解析结果来判断：那要走一次 HTTP 才知道"有没有链接"，
# 群里每分钟几十条闲聊，全发过去等于拿后端当过滤器。这里宁可粗一点。
_PAN_HOST = re.compile(
    r"(?:pan\.quark\.cn|pan\.baidu\.com|alipan\.com|aliyundrive\.com|drive\.uc\.cn"
    r"|fast\.uc\.cn|pan\.xunlei\.com|115\.com|115cdn\.com|anxia\.com"
    r"|123pan\.|123684\.com|123865\.com|123912\.com|123592\.com"
    r"|cloud\.189\.cn|yun\.139\.com|caiyun\.139\.com|guangyapan\.com"
    r"|lanzou[a-z]?\.com|weiyun\.com|ctfile\.com|mypikpak\.com|pikpak\.com)",
    re.I,
)

_VALID_ROLES = ("owner", "admin", "member")

# ---------------------------------------------------------------- AI 结构化
#
# 正则能把链接抠出来，但标题经常只是半截：群里常见的是"标题写两行 + 提取码
# 隔三行 + 尾巴挂一句广告"，规则解析只能靠长度和字段名去猜哪行是片名。
# AstrBot 这边本来就配了大模型，让它直接读整条消息整理成结构化字段，比猜准。
#
# 分工写死在两边：**模型只负责标题和取舍，链接必须原文照抄**。后端会拿正则
# 结果核对每一条 AI 条目——对不上 `platform:pwd_id` 的直接丢，所以模型把短链
# 补全、把 id 记错一个字符都不会污染库。同理它漏掉的链接也会被正则补回来，
# 结构化只改善标题，绝不减少入库数量。
_STRUCT_SYSTEM = """你是 QQ 群分享消息的网盘资源抽取器。把一条群消息整理成 JSON，除此之外什么都不做。

只输出 JSON，不要解释、不要 Markdown 代码块：
{"items":[{"title":"资源名","shareUrl":"https://...","sharePwd":"abcd"}]}

规则：
1. shareUrl 必须逐字照抄原文里的链接，不得改写、补全、拼接，也不得凭印象写。原文有几个链接就输出几条。
2. title 是资源名：保留片名、季数、年份、清晰度（4K/1080P）、合集等信息；去掉「链接」「提取码」「下载地址」这类标签词，去掉网盘平台名（夸克/百度/阿里云盘/迅雷/UC 等），去掉广告和拉群话术（更多资源关注公众号、加群领取等）、表情和多余标点。
3. 标题写在链接上一行时，它属于后面的那些链接；同一个资源备份在多个网盘时，几条共用同一个标题。
4. 一条消息里有多个不同资源时必须拆成多条，各用自己的标题，不要用一个总标题套住全部。
5. sharePwd 只填原文明确写出的提取码/访问码/密码；原文没有就填空字符串，绝对不要猜。
6. 原文没有任何网盘链接时输出 {"items":[]}。"""

# 一次 flush 里最多给几条消息过模型。15 秒内批量进来几十条时多半是刷屏或
# 广告轰炸，全部送模型既慢又费钱；超出的直接走正则，链接一样入库。
_AI_MAX_PER_FLUSH = 6
# 并发上限。群里连发时几条消息同时结构化，但别把模型服务的限流打满。
_AI_CONCURRENCY = 3


async def _maybe_await(value):
    """AstrBot 各版本里 get_using_provider / get_provider_by_id 有同步也有异步。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _text_of(resp) -> str:
    """从 LLMResponse / 字符串里取纯文本。"""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp.strip()
    text = getattr(resp, "completion_text", "") or ""
    if not text:
        chain = getattr(resp, "result_chain", None)
        getter = getattr(chain, "get_plain_text", None)
        if callable(getter):
            try:
                text = getter()
            except Exception:  # noqa: BLE001
                text = ""
    return str(text or "").strip()


def _extract_json(text: str):
    """从模型输出里捞出 JSON。返回 dict / list，捞不到返回 None。

    模型的三层套娃必须都处理：直接给 JSON、包在 ```json 围栏里、前面先说
    "好的，以下是整理结果："再给 JSON。最后一种最普遍，所以兜底是找第一个
    `{` 到最后一个 `}`。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    fenced = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    fenced = re.sub(r"\s*```$", "", fenced).strip()
    for candidate in (fenced, raw):
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(raw[start:end + 1])
            except (ValueError, TypeError):
                continue
    return None


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

        # ---- 采集侧状态 ----
        self._pending: list[dict] = []        # 待上报消息队列
        self._flush_task: asyncio.Task | None = None
        self._seen: OrderedDict[str, float] = OrderedDict()   # message_id 去重窗口
        self._roles: dict[str, tuple[str, float]] = {}        # (群:人) -> (角色, 时间)
        self._warned_no_group = False          # 「没配群号」只提醒一次
        self._warned_no_llm = False            # 「没取到大模型」只提醒一次
        # 最近一条群消息的会话标识。结构化在后台任务里跑，手上没有 event，
        # 只能借用它去问 AstrBot「这个会话该用哪个模型」。空串就退回全局默认。
        self._last_umo = ""

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

    # ------------------------------------------------------------ 采集侧
    #
    # 目标是"群里有人发了资源，库自己变厚"。三条硬约束：
    #
    # 1. **静默**。默认不产生任何回复，否则群里每来一条分享机器人就吭一声，
    #    两天就被踢了。要回执的话把 capture_ack 打开。
    # 2. **不上报闲聊**。群白名单 → 身份白名单 → 域名粗筛，三道都过才发。
    # 3. **绝不因为上报失败影响聊天**。异常全部吞掉记日志，不抛给事件循环。

    def _list_conf(self, key: str, default: list[str]) -> list[str]:
        """配置项读成列表。

        AstrBot 的 list 项返回 list，但用户可能在**同一个格子里**填
        「123456, 789012」——那样 list 里只有一个元素。所以每项都要再按逗号
        拆一次，否则白名单里是个永远匹配不上的 `"123456, 789012"`，
        表现是"明明配了群号却一条都不采集"，还不报错。
        """
        raw = self.config.get(key, default)
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        out: list[str] = []
        for item in items:
            for token in str(item or "").replace("，", ",").split(","):
                token = token.strip()
                if token:
                    out.append(token)
        return out

    def _watched_groups(self) -> set[str]:
        """要监听的群号。**空 = 一个都不采**。

        不采用"空 = 全部群"这种便利默认：仓库里 `ENABLED_SOURCES` 就是按
        「空 = 一个都不启用」定的，同一个项目里两套相反约定，迟早有人把
        测试配置带上线，把整站所有群的链接都收进来。要采集就显式写群号。
        """
        return set(self._list_conf("capture_groups", []))

    def _watched_roles(self) -> set[str]:
        roles = {r.lower() for r in self._list_conf("capture_roles", ["owner", "admin"])}
        return {r for r in roles if r in _VALID_ROLES} or {"owner", "admin"}

    def _raw_role(self, event: AstrMessageEvent) -> str:
        """从 OneBot 原始事件里读发送者身份。NapCat 的群消息 event.sender.role
        就是权威值，比额外查一次群成员接口快且省。

        注意这**不是** `event.is_admin()`：那个是 AstrBot 自己的管理员名单，
        和"这个群的群主/管理员"完全是两回事，混用会让随便一个 bot 管理员
        在任意群里都能往库里灌链接。
        """
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return ""
        sender = raw.get("sender")
        if not isinstance(sender, dict):
            return ""
        role = str(sender.get("role") or "").strip().lower()
        return role if role in _VALID_ROLES else ""

    async def _role_of(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        """发送者在该群的身份。原始事件里没有才去查，查到缓存 10 分钟。

        缓存不是省流量，是省事：NapCat 每查一次群成员都会在服务端留痕，
        群里刷屏时逐条查会明显拖慢消息处理。
        """
        role = self._raw_role(event)
        if role:
            return role

        key = f"{group_id}:{user_id}"
        now = time.time()
        cached = self._roles.get(key)
        if cached and now - cached[1] < 600:
            return cached[0]

        try:
            # 延迟导入：非 aiocqhttp 平台（QQ 官方机器人等）没有这个模块，
            # 放在文件顶部会让整个插件在那些平台上加载失败。
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            if isinstance(event, AiocqhttpMessageEvent):
                info = await event.bot.api.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=True,
                )
                role = str((info or {}).get("role") or "").strip().lower()
        except Exception as exc:  # noqa: BLE001
            logger.debug("pantrace: 查询群成员身份失败 %s/%s: %s", group_id, user_id, exc)

        if role in _VALID_ROLES:
            self._roles[key] = (role, now)
            if len(self._roles) > 800:      # 简单封顶，别让缓存无限长
                self._roles.clear()
        return role

    def _harvest_card(self, event: AstrMessageEvent) -> str:
        """从卡片 / JSON / XML 消息段里挖标题和网盘链接，按「标题行 + 链接行」排版。

        QQ 里转发的网盘分享多数是**卡片消息**，`message_str` 里只有一句
        「[卡片消息]」甚至空白，标题和链接都在 json 段里（`meta.news.title`
        和 `jumpUrl`）。不挖这一层，群里最标准的分享方式反而一条都采不到。

        只留网盘域名的链接：卡片里同时有头像 CDN、封面图、跳转统计链，
        全塞给后端会被当成脏数据，还白跑一遍解析。

        返回多行文本而不是一行，是为了让后端的「标题在上、链接在下」规则
        直接生效——那边本来就是按行认标题的，拼成一行反而要另写分支。
        """
        blocks: list[str] = []
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for seg in message:
            # 不依赖具体的消息段类名：AstrBot 各版本对 Json/卡片段的封装不同，
            # 统一按"对象的属性字典里有没有 URL"来找，稳。
            try:
                blob = json.dumps(getattr(seg, "__dict__", {}) or {}, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                continue
            # 卡片里的链接常写成 `https:\/\/pan.quark.cn\/s\/xxx`（转义斜杠），
            # 经过 json.dumps 后反斜杠还会翻倍成 `\\/`。只替换 `\/` 会剩一个
            # 反斜杠，拼出 `https:////pan...` 这种废链——`\\+/` 把连续反斜杠
            # 连同后面的斜杠一起吞掉才对。
            blob = re.sub(r"\\+/", "/", blob)

            urls = [u for u in re.findall(r"https?://[^\s\"'<>\\]+", blob) if _PAN_HOST.search(u)]
            if not urls:
                continue

            # 标题优先取 title 字段，退而取 desc。只要不含 URL、长度像话就用
            title = ""
            for field in ("title", "desc"):
                match = re.search(rf'"{field}"\s*:\s*"([^"]{{2,80}})"', blob)
                if match:
                    cand = match.group(1).strip()
                    if cand and "http" not in cand.lower():
                        title = cand
                        break

            if title:
                blocks.append(title)
            blocks.extend(list(dict.fromkeys(urls))[:10])   # 同段内按原序去重
        return "\n".join(blocks)

    async def _provider_chat(self, provider, prompt: str, system: str) -> str:
        """调 Provider.text_chat。各版本参数有差异，逐级降级。"""
        method = getattr(provider, "text_chat", None)
        if not callable(method):
            return ""
        try:
            return _text_of(await method(prompt=prompt, system_prompt=system))
        except TypeError:
            # 很老的版本不收 system_prompt，只能拼进 prompt 里
            try:
                return _text_of(await method(prompt=f"{system}\n\n{prompt}"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("pantrace: text_chat 调用失败：%s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pantrace: text_chat 调用失败：%s", exc)
        return ""

    async def _llm_text(self, prompt: str, system: str) -> str:
        """用 AstrBot 里已配置的大模型跑一次补全，拿不到就返回空串。

        两套接口都要接：
        - v4.5.7+：`get_current_chat_provider_id` + `llm_generate`；
        - 旧版：`get_using_provider().text_chat`。
        只写新接口的话，用户的 AstrBot 稍旧一点，这个功能就静默失效，
        表现成"配了 AI 结构化却一点效果都没有"，很难查。
        """
        ctx = self.context
        want = str(self.config.get("ai_provider", "") or "").strip()

        generate = getattr(ctx, "llm_generate", None)
        if callable(generate):
            pid = want
            if not pid:
                getter = getattr(ctx, "get_current_chat_provider_id", None)
                if callable(getter):
                    try:
                        pid = str(await _maybe_await(getter(umo=self._last_umo)) or "")
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("pantrace: 取当前模型 id 失败：%s", exc)
                        pid = ""
            if pid:
                try:
                    resp = await generate(
                        chat_provider_id=pid, prompt=prompt, system_prompt=system,
                    )
                    # 调用成功就不再走旧接口：模型返回空文本也只是一次空结果，
                    # 再补一次调用等于每条消息花两份钱。
                    return _text_of(resp)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pantrace: llm_generate 失败，改走旧接口：%s", exc)

        provider = None
        getter = getattr(ctx, "get_using_provider", None)
        if callable(getter):
            try:
                provider = await _maybe_await(getter(umo=self._last_umo))
            except TypeError:
                # 更老的签名：位置参数或没有 umo
                try:
                    provider = await _maybe_await(getter())
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pantrace: get_using_provider() 失败：%s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.debug("pantrace: get_using_provider 失败：%s", exc)

        if provider is None and want:
            by_id = getattr(ctx, "get_provider_by_id", None)
            if callable(by_id):
                try:
                    provider = await _maybe_await(by_id(provider_id=want))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pantrace: get_provider_by_id 失败：%s", exc)

        if provider is None:
            return ""
        return await self._provider_chat(provider, prompt, system)

    def _ai_timeout(self) -> int:
        return max(5, min(60, int(self.config.get("ai_timeout", 20))))

    async def _structure_one(self, text: str) -> list[dict]:
        """一条消息 → `[{title, shareUrl, sharePwd}]`。失败返回空列表。"""
        text = str(text or "").strip()
        if not text:
            return []
        prompt = f'群消息原文（其中可能夹着图片/卡片占位符，忽略即可）：\n"""\n{text[:2000]}\n"""'
        try:
            raw = await asyncio.wait_for(
                self._llm_text(prompt, _STRUCT_SYSTEM), timeout=self._ai_timeout(),
            )
        except asyncio.TimeoutError:
            logger.debug("pantrace: AI 结构化单条超时")
            return []
        except Exception as exc:  # noqa: BLE001
            logger.debug("pantrace: AI 结构化调用异常：%s", exc)
            return []

        if not raw:
            if not self._warned_no_llm:
                logger.warning(
                    "pantrace: 没取到大模型，AI 结构化已跳过，本次改用规则解析。"
                    "请到 WebUI「模型提供商」里配置并启用一个模型；"
                    "不需要这个功能就把插件配置里的「AI 结构化」关掉。"
                )
                self._warned_no_llm = True
            return []

        data = _extract_json(raw)
        if data is None:
            logger.debug("pantrace: AI 返回的不是 JSON，已忽略：%s", raw[:200])
            return []

        rows = data.get("items") if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return []

        out: list[dict] = []
        for row in rows[:10]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("shareUrl") or row.get("url") or "").strip()
            if not url.lower().startswith("http"):
                continue
            out.append({
                "title": str(row.get("title") or "").strip()[:120],
                "shareUrl": url,
                "sharePwd": str(row.get("sharePwd") or row.get("pwd") or "").strip()[:16],
            })
        return out

    async def _structure_batch(self, batch: list[dict]) -> int:
        """给待上报的每消息补上 `items` 字段。返回成功条数。

        在**后台 flush 任务**里跑，不在消息处理函数里跑：模型动辄几秒，
        放在 handler 里会把群消息处理（以及别的插件）一起拖慢。
        """
        if not self.config.get("ai_structure", True) or not batch:
            return 0
        todo = [m for m in batch if not m.get("items")][:_AI_MAX_PER_FLUSH]
        if not todo:
            return 0

        sem = asyncio.Semaphore(_AI_CONCURRENCY)

        async def one(msg: dict) -> None:
            async with sem:
                got = await self._structure_one(str(msg.get("text") or ""))
                if got:
                    msg["items"] = got

        started = time.time()
        try:
            # 整批一个总预算。模型服务卡住时，这一批剩下的直接退回正则解析
            # ——链接照样入库，只是标题糙一点，总比后台任务挂死强。
            await asyncio.wait_for(
                asyncio.gather(*(one(m) for m in todo), return_exceptions=True),
                timeout=self._ai_timeout(),
            )
        except asyncio.TimeoutError:
            logger.warning("pantrace: AI 结构化整批超时，未完成的部分退回规则解析")
        except Exception as exc:  # noqa: BLE001
            logger.debug("pantrace: AI 结构化批次异常：%s", exc)

        done = sum(1 for m in todo if m.get("items"))
        if done:
            flat = [i for m in todo for i in (m.get("items") or [])]
            # 默认日志级别是 INFO，debug 里那行平时看不见。这里只打标题：
            # 一眼能判断"模型有没有把广告词当片名"，不用翻库去搜。
            titles = " ｜ ".join(str(i.get("title") or "")[:36] for i in flat[:5])
            logger.info(
                "pantrace: AI 结构化 %s/%s 条 → %s 个资源，耗时 %.1fs：%s",
                done, len(todo), len(flat), time.time() - started, titles,
            )
            logger.debug("pantrace: 结构化明细 %s", json.dumps(flat, ensure_ascii=False)[:800])
        return done

    async def _report(self, messages: list[dict]) -> tuple[bool, dict]:
        """把一批消息发给后端。返回 (成功, 返回数据)。"""
        base = self._base_url()
        if not base:
            return False, {"msg": "没配置 base_url"}
        token = str(self.config.get("ingest_token", "") or "").strip()
        if not token:
            return False, {"msg": "没配置 ingest_token（和后端 QQ_INGEST_TOKEN 要一致）"}

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{base}/api/ingest/qq",
                    json={"messages": messages},
                    headers={
                        "x-ingest-token": token,
                        "User-Agent": "AstrBot-PanTrace/1.1",
                    },
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        return False, {"msg": f"HTTP {resp.status}：{text[:120]}"}
                    body = await resp.json()
        except asyncio.TimeoutError:
            return False, {"msg": "上报超时"}
        except aiohttp.ClientError as exc:
            return False, {"msg": f"{type(exc).__name__}"}
        except Exception as exc:  # noqa: BLE001
            return False, {"msg": str(exc)[:120]}

        if body.get("code") != 0:
            return False, {"msg": str(body.get("msg", ""))[:120]}
        return True, body.get("data") or {}

    async def _flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending[:50], self._pending[50:]
        # 先让大模型把消息整理成结构化字段，再由后端核对入库。
        # 失败/超时/没配模型都不影响上报：没 items 的消息走后端正则解析。
        ai_done = await self._structure_batch(batch)
        ok, data = await self._report(batch)
        if not ok:
            # 重试一次：网络抖一下就丢一批链接太亏。仍失败就放弃——
            # 群里消息是流，攒着只会等网络好了灌进一批过期数据。
            ok, data = await self._report(batch)
        if not ok:
            logger.warning("pantrace: 采集上报失败，丢弃 %s 条：%s", len(batch), data.get("msg"))
            return
        logger.info(
            "pantrace: 采集 %s 条消息（AI 结构化 %s 条）→ 链接 %s，新增 %s / 更新 %s",
            len(batch), ai_done, data.get("links"), data.get("added"), data.get("touched"),
        )

    async def _delayed_flush(self, delay: float = 1.5) -> None:
        """攒一小会儿再发。一条消息发一次请求，群里连着发十条就是十次 HTTP。"""
        await asyncio.sleep(delay)
        await self._flush()

    def _schedule_flush(self) -> None:
        # 队列顶到 200 说明后端长时间不可用，先丢最老的，别把内存撑爆
        if len(self._pending) > 200:
            self._pending = self._pending[-200:]
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_flush())

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息静默采集。不回复、不拦截，其他插件和 LLM 照常处理。"""
        if not self.config.get("capture_enabled", False):
            return

        try:
            group_id = str(event.get_group_id() or "")
            if not group_id:
                return
            watched = self._watched_groups()
            if not watched:
                # 只在第一次抱怨，别在群里每条消息刷一遍日志
                if not self._warned_no_group:
                    logger.warning(
                        "pantrace: 采集已开启但没配监听的群号，不会采集任何消息。"
                        "去插件配置里填群号（纯数字，多个用逗号分隔）。"
                    )
                    self._warned_no_group = True
                return
            if group_id not in watched:
                return

            sender_id = str(event.get_sender_id() or "")
            # 自己发的消息按普通成员处理不了，直接跳过（免得回声）
            if sender_id and sender_id == str(event.get_self_id() or ""):
                return

            role = await self._role_of(event, group_id, sender_id)
            if role not in self._watched_roles():
                return

            text = (event.message_str or "").strip()
            cards = self._harvest_card(event)
            if not _PAN_HOST.search(text) and not cards:
                return

            # 同一条消息只处理一次。AstrBot 在多插件/多适配器下可能重复派发。
            message_id = str(getattr(event.message_obj, "message_id", "") or "")
            if message_id:
                now = time.time()
                if message_id in self._seen:
                    return
                self._seen[message_id] = now
                while len(self._seen) > 500:
                    self._seen.popitem(last=False)

            # 记下会话标识，后台结构化时用它问「这个会话该用哪个模型」
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if umo:
                self._last_umo = umo

            self._pending.append({
                "id": message_id,
                "groupId": group_id,
                "groupName": "",          # 群名要另查，收益不值这个请求
                "senderId": sender_id,
                "senderName": event.get_sender_name() or "",
                "role": role,
                "text": f"{text}\n{cards}".strip(),
            })
            self._schedule_flush()

            if self.config.get("capture_ack", False):
                yield event.plain_result("已收录，稍后入库。")
        except Exception as exc:  # noqa: BLE001
            # 采集链路任何异常都不许冒到事件循环：它挂掉会连带影响 /搜 和群里其他功能
            logger.warning("pantrace: 群消息采集异常：%s", exc)

    async def terminate(self):
        """插件卸载/重载时清理会话资源。

        先把队列里剩下的消息发出去：重载通常在改配置之后，攒着的几条
        直接丢掉的话，用户会以为"明明发了链接却没进库"。
        """
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        try:
            await self._flush()
        except Exception as exc:  # noqa: BLE001
            logger.debug("pantrace: 卸载时补发失败：%s", exc)
