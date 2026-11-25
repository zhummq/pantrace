"""盘迹 PanTrace · AstrBot 群分享采集插件。

**只做一件事**：静默监听指定群里群主/管理员发的网盘分享，把链接提取出来
上报给后端 `/api/ingest/qq` 入库。

不做搜索、不发命令、不回复（除非打开回执）、不拦截事件——群里的正常对话
和其他插件完全不受影响。

采集链路里，插件先让 AstrBot 已配置的大模型把消息整理成
`{title, shareUrl, sharePwd}`，再由后端核对入库。

但**链接的唯一可信来源永远是原文**：插件不写 URL 正则、不猜 share id，
后端会拿 `search/clean.py` 的正则结果核对每一条 AI 条目，对不上
`platform:pwd_id` 的直接丢弃。策略是**纯 AI 为准**——AI 给了有效条目就
只入库这些，AI 没覆盖到的链接不入库；AI 一条都没给对，才整条退回规则解析。
所以模型能把死链、编造的链接挡在库外，但它漏掉的链接也不会被补齐。

--- 架构（v2.1 重构）
----------------------------------------------------------------------
v2.0 的发送是**消息驱动**的：来一条消息就 `asyncio.create_task(延时 1.5s 发送)`，
而且只在"当前没有发送任务"时才创建。结果是：一批消息正在跑 AI（60 秒预算）
或正在等后端响应时进来的消息，不会再触发新的发送任务，只能干等到**下一条
群消息**才被顺带带出去；如果群里就此安静，它们会一直躺在内存里，直到插件
重载——而重载时 `terminate()` 先 `cancel()` 了在途任务，那一批的 batch 变量
随任务一起消失，是真的丢。

现在改成两个常驻部件：

  group handler ──入队──► 队列(list) ──► AI 补齐器（并发 3/在途 12）
                                   │
                                   └──► 发送器（常驻单任务，500ms 一轮）

- **发送器**是唯一的 HTTP 出口，按自己的节奏取"已结算"的消息发走，不依赖
  下一条消息来踢它。后端抖一下只影响发送器自己，AI 补齐照跑。
- **AI 补齐器**在消息入队时就开工，不再串行挡在 HTTP 前面。某条消息等满
  `ai_settle` 秒还没等到模型，就先按规则解析发走（保持纯 AI 策略不变：
  先发的这条没有 items，后端走正则；等到结果也不会再补，避免重复入库）。
- 队列有上限，满了丢最老的并**明确打日志**，不再静默截断。
- 失败退避重试 + 冷却；配置类错误（4xx）不重试，只节流告警。
- AI 为什么没生效，现在有原因码并节流汇总——`AI 结构化 0 条` 不再是一句
  没有下文的话。
"""

import asyncio
import inspect
import json
import re
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

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
1. shareUrl 必须逐字照抄原文里的链接，不得改写、补全、拼接，也不得凭印象写。原文有几个链接就输出几条，不要因为条数多就只给前几条。
2. title 是资源名：保留片名、季数、年份、清晰度（4K/1080P）、合集等信息；去掉「链接」「提取码」「下载地址」这类标签词，去掉网盘平台名（夸克/百度/阿里云盘/迅雷/UC 等），去掉广告和拉群话术（更多资源关注公众号、加群领取等）、表情和多余标点。
3. 标题写在链接上一行时，它属于后面的那些链接；同一个资源备份在多个网盘时，几条共用同一个标题。
4. 一条消息里有多个不同资源时必须拆成多条，各用自己的标题，不要用一个总标题套住全部。
5. sharePwd 只填原文明确写出的提取码/访问码/密码；原文没有就填空字符串，绝对不要猜。
6. 原文没有任何网盘链接时输出 {"items":[]}。"""

# 单条消息送模型的字符上限。资源合集贴动辄两三千字，早先只给 2000 字会把
# 后半段链接整段切掉——模型看不见就没法结构化，纯 AI 策略下这部分直接丢失。
# 10000 字约等于一条超长合集贴的全文，正常分享根本碰不到。
_AI_TEXT_MAX = 10000
# 单条消息最多采纳多少条 AI 条目。这是「防模型发疯灌几百条」的护栏，
# 不是「只处理前 N 个」的截断：100 个链接一条消息里已经极端罕见。
_AI_MAX_ITEMS = 100
# 同时向模型提几个请求。被限流时降低并发比加超时有用。
_AI_CONCURRENCY = 3
# 在途（排队 + 请求中）的消息上限。群里刷屏或广告轰炸时，超出的消息直接
# 按规则解析走，链接一样入库——以前是"一批只给前 10 条过模型"，现在是
# 按在途量连续限流，效果一样但不跟批绑定，不会有消息卡在"永远轮不到"。
_AI_MAX_INFLIGHT = 12

# ---------------------------------------------------------------- 运行参数
_TICK = 0.5                 # 发送器轮询间隔（秒）。只在有待办时才有实际开销
_MAX_PER_POST = 40          # 单个 POST 最多几条消息。条数越多后端越慢，分片更稳
_QUEUE_CAP = 500            # 内存队列上限。超过就丢最老的并打日志
_RETRY_BACKOFF = (2, 5, 10)  # 上报失败退避（秒）。总计 3 次尝试
# 合并窗口：刚进队列的消息先攒这么久再发，连着发的几条能合成一次 POST。
# 队列里已经堆了 >= _COALESCE_MAX 条就不再攒，直接发——那时首要目标是尽快消化。
_COALESCE = 1.0
_COALESCE_MAX = 8
_COOLDOWN = 30              # 连续失败后的冷却（秒），期间不发新批次
_COOLDOWN_CONFIG_ERROR = 120  # 4xx（令牌/地址写错）冷却更久，重试没有意义
_AI_WARN_INTERVAL = 300     # AI 未生效原因的汇总告警间隔（秒）
# 自适应等待的样本量与上限。样本只记"真正跑完的调用"（含迟到返回的），
# 这样模型是稳定 17 秒还是偶发 60 秒，能被自己的数据看出来。
_MODEL_PROBE_INTERVAL = 60   # 取不到模型名时的重试间隔（秒）
_AI_LAT_SAMPLES = 20
_AI_WAIT_MAX = 120.0
_AI_WAIT_FACTOR = 1.6
_SEEN_CAP = 500             # message_id 去重窗口
_ROLE_TTL = 600             # 群身份缓存（秒）

# AI 没生效的原因 → 给人看的一句话。查日志的人应该能直接知道去哪修。
_AI_REASON_HINT = {
    "no_provider": "没取到大模型。去 WebUI「模型提供商」启用一个，或在插件里填「指定模型提供商 ID」",
    "timeout": "模型响应超过 ai_timeout。合集贴可以调大，或换个更快的模型",
    "expired": "模型还没回话就到了等待阈值，本条先按规则解析入库了（阈值会按模型实际耗时自动放大）",
    "empty": "模型返回了空文本（拒答或被截断）",
    "bad_json": "模型没按 JSON 输出，本条退回规则解析",
    "no_items": "模型认为原文没有网盘链接（卡片内容可能没挖到）",
    "overload": "同时待处理的消息太多，本批部分消息跳过 AI 直接走规则",
    "disabled": "「AI 结构化」是关的",
}


# ---------------------------------------------------------------- 工具函数


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


def _model_of(provider) -> str:
    """从 Provider 对象里挖出模型名。

    各版本字段都不一样，只能逐个试：`get_model()`（新版有）、`model`、
    `provider_config.model`、`provider_config.model_config.model`、
    `meta().model`。全都拿不到就返回空串——**日志宁可少一行，也不能因为
    取个名字把整条 AI 链路搞挂**。
    """
    if provider is None:
        return ""

    def _clean(value) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

    getter = getattr(provider, "get_model", None)
    if callable(getter):
        try:
            name = _clean(getter())
            if name:
                return name
        except Exception:  # noqa: BLE001
            pass
    # 少数版本 get_model 是属性不是方法
    name = _clean(getattr(provider, "get_model", None))
    if name:
        return name

    for attr in ("model", "model_name", "model_id"):
        name = _clean(getattr(provider, attr, None))
        if name:
            return name

    for attr in ("provider_config", "provider_settings"):
        conf = getattr(provider, attr, None)
        if not isinstance(conf, dict):
            continue
        for field in ("model", "model_name", "model_id"):
            name = _clean(conf.get(field))
            if name:
                return name
        nested = conf.get("model_config")
        if isinstance(nested, dict):
            name = _clean(nested.get("model"))
            if name:
                return name

    meta = getattr(provider, "meta", None)
    if callable(meta):
        try:
            info = meta()
            name = _clean(getattr(info, "model", None))
            if not name and isinstance(info, dict):
                name = _clean(info.get("model"))
            if name:
                return name
        except Exception:  # noqa: BLE001
            pass
    return ""


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


_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\\)）\]】]+")


def _collapse_same_url(text: str) -> str:
    """同一条消息里**完全相同的**链接只留第一次出现。

    群里常见的排版是「正文贴一遍 + 卡片再带一遍」，同一条分享在一条消息里
    出现两次。后端按 URL 折叠，最后表现成「入库 18 条，新增 0 / 更新 9」
    这种两头对不上的数字，白跑一轮解析，还让人以为采集重复了。

    只按字符串完全相同去重，不改写、不补全任何链接，所以不会把两条不同的
    分享误并成一条。
    """
    if not text:
        return ""
    seen: set[str] = set()
    out: list[str] = []
    pos = 0
    hit = False
    for match in _URL_IN_TEXT.finditer(text):
        url = match.group(0)
        if url in seen:
            out.append(text[pos:match.start()])
            pos = match.end()
            hit = True
        else:
            seen.add(url)
    if not hit:
        return text
    out.append(text[pos:])
    return "".join(out)


@dataclass
class _Item:
    """队列里的一条待上报消息。"""

    id: str
    group_id: str
    sender_id: str
    sender_name: str
    role: str
    text: str
    created: float
    ai_state: str = "skipped"     # pending / done / skipped
    ai_reason: str = ""
    items: list[dict] = field(default_factory=list)
    taken: bool = False           # 已被发送器取走，AI 结果再回来也不采纳

    def ready(self, now: float, settle: float) -> bool:
        """可以发了吗：AI 有结论了，或者等够 ai_settle 秒。"""
        return self.ai_state != "pending" or (now - self.created) >= settle

    def payload(self) -> dict:
        return {
            "id": self.id,
            "groupId": self.group_id,
            "groupName": "",       # 群名要另查，收益不值这个请求
            "senderId": self.sender_id,
            "senderName": self.sender_name,
            "role": self.role,
            "text": self.text,
            # 没有 items 时后端走规则解析；有 items 时按纯 AI 策略核对
            **({"items": self.items} if self.items else {}),
        }


class PanTraceCollector(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # ---- 队列与常驻任务 ----
        self._queue: list[_Item] = []
        self._wake = asyncio.Event()
        self._flusher: asyncio.Task | None = None
        self._stopping = False
        self._session: aiohttp.ClientSession | None = None

        # ---- AI 补齐 ----
        self._ai_tasks: set[asyncio.Task] = set()
        self._ai_inflight = 0
        self._ai_sem: asyncio.Semaphore | None = None
        self._ai_lat: deque[float] = deque(maxlen=_AI_LAT_SAMPLES)

        # ---- 当前模型（只用于日志） ----
        self._model_name = ""       # 模型名，如 kimi-k2 / gpt-4o-mini
        self._model_pid = ""        # 对应的提供商 ID
        self._model_logged = ""     # 已经打过日志的那一个，防重复刷屏
        self._model_probed_at = 0.0  # 上次尝试询问模型名的时间

        # ---- 去重与身份缓存 ----
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._roles: dict[str, tuple[str, float]] = {}

        # ---- 重试 / 熔断 ----
        self._fail_streak = 0
        self._cooldown_until = 0.0
        self._last_config_warn = 0.0
        self._last_ai_warn = 0.0

        # ---- 一次性提醒 ----
        self._warned_no_group = False
        self._warned_cold_group: set[str] = set()
        self._overflow_dropped = 0

        # 最近一条群消息的会话标识。AI 补齐在后台任务里跑，手上没有 event，
        # 只能借用它去问 AstrBot「这个会话该用哪个模型」。空串就退回全局默认。
        self._last_umo = ""

    # ------------------------------------------------------------ 配置读取

    def _base_url(self) -> str:
        return str(self.config.get("base_url", "")).strip().rstrip("/")

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

    def _watched_senders(self) -> tuple[set[str], dict[str, set[str]]]:
        """指定还要采集哪些人。返回 (全局 QQ 号, {群号: QQ 号集合})。

        **按 QQ 号而不是昵称**：昵称随时改、还能重名，QQ 号是唯一且稳定的。
        和身份白名单取**并集**——名单里的人不看身份照收，群主/管理员照旧，
        所以它只做"再多收一个人"，不会把现有采集面缩窄。

        两种写法：
          `123456789`        这个人在所有被监听的群里都收
          `987654321:123456789` / `987654321@123456789`
                             只在这个群里收（同一个人在不同群身份不同时用）
        """
        every: set[str] = set()
        by_group: dict[str, set[str]] = {}
        for token in self._list_conf("capture_senders", []):
            group, sep, uid = token.replace("：", ":").partition(":")
            if not sep:
                group, sep, uid = token.partition("@")
            if sep:
                group, uid = group.strip(), uid.strip()
                if group and uid:
                    by_group.setdefault(group, set()).add(uid)
            elif token:
                every.add(token)
        return every, by_group

    def _ai_timeout(self) -> int:
        try:
            value = int(self.config.get("ai_timeout", 60))
        except (TypeError, ValueError):
            value = 60
        return max(5, min(60, value))

    def _ai_settle(self) -> float:
        """等待阈值的**下限**，由配置 `ai_settle` 决定。真正的阈值见 `_ai_wait`。

        这个值只在一件事上花钱：模型还没回话的那几秒。模型 3 秒就回来的消息，
        3 秒就发走了，跟阈值多大无关。所以它调大不会拖慢正常消息，只影响
        "模型慢"这一种情况——而那正是最不该硬砍的时候。
        """
        try:
            value = int(self.config.get("ai_settle", 20))
        except (TypeError, ValueError):
            value = 20
        return float(max(3, min(120, value)))

    def _ai_wait(self) -> float:
        """实际等待阈值 = max(配置下限, 近期模型耗时的 1.6 倍)。

        为什么不能只写死一个数：模型的耗时是会漂的（换模型、服务商排队、
        夜里负载），写死 15 秒，17 秒的模型就会**每条都**踩线——表现正是
        "AI 结构化一会可以一会不可以"，而且踩线那一刻标题就永久固化成
        正则版本了（后端明确不覆盖已入库标题），补不回来。

        所以让插件自己记账：每次真正跑完的调用（含迟到返回的）都记一笔，
        按 1.6 倍留余量。第一条约 17 秒的消息会踩一次线，但从那以后阈值
        自动抬到 27 秒，同样的模型不会再踩第二次。没有样本时才用配置值。

        只有一条样本也照算：宁可多等一次，也别让第二条再踩线——多等的代价
        只在"模型真的还没回话"时才会花出去，模型快的时候一秒都不多等。
        """
        base = self._ai_settle()
        if not self._ai_lat:
            return base
        recent = sorted(self._ai_lat)
        p90 = recent[min(len(recent) - 1, int(len(recent) * 0.9))]
        return max(base, min(_AI_WAIT_MAX, p90 * _AI_WAIT_FACTOR))

    def _ai_p50(self) -> float:
        """模型近期耗时的中位数（秒），没有样本返回 0。"""
        if not self._ai_lat:
            return 0.0
        recent = sorted(self._ai_lat)
        return recent[len(recent) // 2]

    def _ai_speed(self) -> str:
        """给日志用的一句话：模型近期多快、现在等多久。"""
        if not self._ai_lat:
            return f"{self._model_hint()}等待阈值 {self._ai_wait():.0f}s，还没有成功样本"
        recent = sorted(self._ai_lat)
        p90 = recent[min(len(recent) - 1, int(len(recent) * 0.9))]
        return (
            f"{self._model_hint()}等待阈值 {self._ai_wait():.0f}s，"
            f"近 {len(recent)} 次中位 {recent[len(recent)//2]:.1f}s / 最慢 {p90:.1f}s"
        )

    # ------------------------------------------------------------ 身份判定

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
        if cached and now - cached[1] < _ROLE_TTL:
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

    # ------------------------------------------------------------ 卡片消息

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
            for name in ("title", "desc"):
                match = re.search(rf'"{name}"\s*:\s*"([^"]{{2,80}})"', blob)
                if match:
                    cand = match.group(1).strip()
                    if cand and "http" not in cand.lower():
                        title = cand
                        break

            if title:
                blocks.append(title)
            blocks.extend(list(dict.fromkeys(urls))[:10])   # 同段内按原序去重
        return "\n".join(blocks)

    # ------------------------------------------------------------ 模型调用

    def _model_hint(self) -> str:
        """日志前缀：`模型 kimi-k2，`；还不知道就返回空串。"""
        return f"模型 {self._model_name}，" if self._model_name else ""

    async def _lookup_model(self, pid: str) -> str:
        """按 ID / 全局默认去问一次模型名。全部失败返回空串，不抛异常。"""
        ctx = self.context
        by_id = getattr(ctx, "get_provider_by_id", None)
        if callable(by_id) and pid:
            try:
                name = _model_of(await _maybe_await(by_id(provider_id=pid)))
                if name:
                    return name
            except Exception as exc:  # noqa: BLE001
                logger.debug("pantrace: 取模型名失败（id=%s）：%s", pid, exc)

        getter = getattr(ctx, "get_using_provider", None)
        if callable(getter):
            for kwargs in ({"umo": self._last_umo}, {}):
                try:
                    name = _model_of(await _maybe_await(getter(**kwargs)))
                except TypeError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pantrace: 取当前模型失败（%s）：%s", kwargs, exc)
                    continue
                if name:
                    return name
        return ""

    async def _note_model(self, pid: str, provider=None) -> str:
        """确认「现在用的是哪个模型」，变化时打一行 INFO。

        用户最先要问的就是这个：模型换没换、配的是不是那个带思考档的，
        以前日志里只有耗时，看不出模型是谁。现在第一次调用模型就会打一行。

        只在模型真的变了才打——每来一条消息刷一遍等于把日志淹掉。
        取不到名字也照样跑，不许因为一个日志字段影响 AI 链路。
        """
        label = _model_of(provider)
        if label:
            self._model_name, self._model_pid = label, pid
        elif self._model_name and (not pid or pid == self._model_pid):
            label = self._model_name
        elif time.time() - self._model_probed_at >= _MODEL_PROBE_INTERVAL:
            # 名字取不到时别每条消息都去问一遍上下文，60 秒一次就够。
            self._model_probed_at = time.time()
            label = await self._lookup_model(pid)
            if label:
                self._model_name, self._model_pid = label, pid

        if not label:
            return ""
        key = f"{pid}|{label}"
        if key != self._model_logged:
            self._model_logged = key
            source = f"（提供商 ID：{pid}）" if pid else "（未指定 ID，用 AstrBot 当前默认模型）"
            logger.info("pantrace: AI 结构化使用模型 %s%s", label, source)
        return label

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

    async def _current_provider_id(self) -> str:
        """问 AstrBot：这个会话该用哪个模型。

        三种情况都要试：配置里指定了 ID → 会话绑定的模型 → 全局默认。
        以前只试前两条，`get_current_chat_provider_id` 返回空串（没给会话
        绑定模型时很常见）就直接判"没模型"，白瞎一个配好的默认模型。
        """
        want = str(self.config.get("ai_provider", "") or "").strip()
        if want:
            return want

        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if not callable(getter):
            return ""
        for umo in (self._last_umo, ""):
            try:
                pid = str(await _maybe_await(getter(umo=umo)) or "")
            except TypeError:
                try:
                    pid = str(await _maybe_await(getter()) or "")
                except Exception:  # noqa: BLE001
                    pid = ""
            except Exception as exc:  # noqa: BLE001
                logger.debug("pantrace: 取当前模型 id 失败（umo=%r）：%s", umo, exc)
                pid = ""
            if pid:
                return pid
        return ""

    async def _llm_text(self, prompt: str, system: str) -> str | None:
        """用 AstrBot 里已配置的大模型跑一次补全。

        返回值的三种情况必须分开，否则日志会指错方向：
        - `None`：这台 AstrBot 上没有可用模型 → 值得提示用户去配置；
        - `""`：有模型，但调用失败或返回空文本 → 用户去配置模型也修不好；
        - 非空：正常结果。

        两套接口都要接：
        - v4.5.7+：`llm_generate`（需要 provider id）；
        - 旧版：`get_using_provider().text_chat`。
        只写新接口的话，用户的 AstrBot 稍旧一点，这个功能就静默失效，
        表现成"配了 AI 结构化却一点效果都没有"，很难查。
        """
        ctx = self.context
        pid = await self._current_provider_id()
        # 第一次真正调模型前，先把"用的是哪个模型"打到日志里。
        # 放在这里而不是 init：init 时上下文可能还没准备好。
        await self._note_model(pid)

        generate = getattr(ctx, "llm_generate", None)
        if callable(generate) and pid:
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
            # 带 umo 拿不到就再试一次不带参数：不同版本签名差异很大，
            # 而"默认模型"这条兜底必须走通，否则整个功能等于没配。
            for kwargs in ({"umo": self._last_umo}, {}):
                try:
                    provider = await _maybe_await(getter(**kwargs))
                except TypeError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pantrace: get_using_provider(%s) 失败：%s", kwargs, exc)
                if provider is not None:
                    break

        if provider is None:
            by_id = getattr(ctx, "get_provider_by_id", None)
            if callable(by_id) and pid:
                try:
                    provider = await _maybe_await(by_id(provider_id=pid))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pantrace: get_provider_by_id 失败：%s", exc)

        if provider is None:
            return None
        # 手上已经有 Provider 对象，名字比按 ID 猜更准。
        await self._note_model(pid, provider)
        return await self._provider_chat(provider, prompt, system)

    async def _structure_one(self, text: str) -> tuple[list[dict], str]:
        """一条消息 → `([{title, shareUrl, sharePwd}], 原因码)`。

        原因码是这次重构的重点：以前无论没模型、超时、返回非 JSON，都只是
        `return []`，日志里只剩一句「AI 结构化 0 条」，查不出为什么。
        """
        text = str(text or "").strip()
        if not text:
            return [], "no_items"
        prompt = f'群消息原文（其中可能夹着图片/卡片占位符，忽略即可）：\n"""\n{text[:_AI_TEXT_MAX]}\n"""'
        sem = self._ai_sem
        try:
            if sem is not None:
                async with sem:
                    raw = await asyncio.wait_for(
                        self._llm_text(prompt, _STRUCT_SYSTEM), timeout=self._ai_timeout(),
                    )
            else:
                raw = await asyncio.wait_for(
                    self._llm_text(prompt, _STRUCT_SYSTEM), timeout=self._ai_timeout(),
                )
        except asyncio.TimeoutError:
            return [], "timeout"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("pantrace: AI 结构化调用异常：%s", exc)
            return [], f"error:{type(exc).__name__}"

        if raw is None:
            return [], "no_provider"
        if not raw:
            return [], "empty"

        data = _extract_json(raw)
        if data is None:
            logger.debug("pantrace: AI 返回的不是 JSON，已忽略：%s", raw[:200])
            return [], "bad_json"

        rows = data.get("items") if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return [], "bad_json"

        out: list[dict] = []
        for row in rows[:_AI_MAX_ITEMS]:
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
        return (out, "ok") if out else ([], "no_items")

    # ------------------------------------------------------------ AI 补齐器

    def _start_ai(self, item: _Item) -> None:
        item.ai_state = "pending"
        if self._ai_sem is None:
            # 延迟到有事件循环时再建，__init__ 里建在部分版本上会绑错 loop
            self._ai_sem = asyncio.Semaphore(_AI_CONCURRENCY)
        self._ai_inflight += 1
        task = asyncio.create_task(self._enrich(item))
        self._ai_tasks.add(task)
        task.add_done_callback(self._ai_done)

    def _ai_done(self, task: asyncio.Task) -> None:
        self._ai_tasks.discard(task)
        self._ai_inflight = max(0, self._ai_inflight - 1)
        # 补一条消息就该让发送器看一眼：可能这就是它在等的那条
        self._wake.set()

    async def _enrich(self, item: _Item) -> None:
        """给一条消息补 `items`。任何异常都不许冒出来。"""
        started = time.time()
        threshold = self._ai_wait()   # 记下"这条本该等多久"，日志要用
        try:
            items, reason = await self._structure_one(item.text)
            cost = time.time() - started
            # 真正跑完的调用才计入样本（"ok" / "no_items" 都算）。报错和
            # 超时不能进，否则模型出故障时会反过来把阈值压低——那是最坏的
            # 方向：越坏越快放弃。
            if reason in ("ok", "no_items"):
                self._ai_lat.append(cost)
            if item.taken:
                # 发送器已经等不及按规则解析发走了。标题这条就补不回来了
                # （后端明确不覆盖已入库标题），但**这次耗时是宝贵样本**：
                # 记下来，下一次阈值自动放大，同样的模型不会再踩第二次。
                item.ai_state = "done"
                if items:
                    logger.info(
                        "pantrace: AI 迟到 %.1fs（本次阈值 %.0fs）返回 %s 个资源，"
                        "这条已按规则解析入库，标题未采用。%s",
                        cost, threshold, len(items), self._ai_speed(),
                    )
                return
            item.items = items
            item.ai_reason = reason
            item.ai_state = "done"
        except asyncio.CancelledError:
            item.ai_state = "skipped"
            item.ai_reason = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            item.ai_state = "skipped"
            item.ai_reason = f"error:{type(exc).__name__}"
            logger.debug("pantrace: AI 补齐异常：%s", exc)
        finally:
            self._wake.set()

    # ------------------------------------------------------------ 发送器

    def _ensure_flusher(self) -> None:
        """拉起常驻发送器。插件加载后第一次入队时创建。"""
        if self._stopping:
            return
        if self._flusher is not None and not self._flusher.done():
            return
        self._flusher = asyncio.create_task(self._flusher_loop())

    async def _flusher_loop(self) -> None:
        """常驻发送循环。

        以前是"来消息 → 建一个延时任务 → 发完就没了"，跑着的时候新来的消息
        不会再建任务，只能等下一条群消息顺带带出去。这里改成常驻：没人叫它
        也会按 500ms 的节拍自己看队列，所以**不存在"攒着不发"的状态**。
        """
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=_TICK)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._stopping:
                return
            try:
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("pantrace: 发送轮次异常：%s", exc)
                await asyncio.sleep(1)

    def _session_of(self) -> aiohttp.ClientSession:
        """复用一个 session。

        以前每批新建一个，插件重载或任务被 cancel 时经常留下
        `Unclosed client session` 警告，连接也没复用。
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                headers={"User-Agent": "AstrBot-PanTrace/2.1"},
            )
        return self._session

    async def _flush_once(self, *, force: bool = False) -> int:
        """发一轮。返回实际发出去的消息条数。

        `force=True` 时不看 AI 状态、不看冷却（卸载/排空时用）。
        """
        if not self._queue:
            return 0
        now = time.time()
        if not force and now < self._cooldown_until:
            return 0

        settle = self._ai_wait()
        coalesce = 0.0 if force else (_COALESCE if len(self._queue) < _COALESCE_MAX else 0.0)
        ready = [
            it for it in self._queue
            if force or (it.ready(now, settle) and (now - it.created) >= coalesce)
        ]
        if not ready:
            return 0

        picked = {id(it) for it in ready}
        self._queue = [it for it in self._queue if id(it) not in picked]
        for it in ready:
            it.taken = True
            if it.ai_state == "pending" and not it.ai_reason:
                # 等不到模型就先走规则。这条不改 ai_state（AI 结果回来也别采纳，
                # taken 已经挡住了），但要留个原因，日志里能看出是"模型慢"。
                it.ai_reason = "expired"

        sent = 0
        for start in range(0, len(ready), _MAX_PER_POST):
            chunk = ready[start:start + _MAX_PER_POST]
            try:
                ok, data = await self._report_with_retry(chunk, retries=not self._stopping)
            except asyncio.CancelledError:
                # 插件被重载/卸载，正好卡在 HTTP 上。这批已经从队列里取走了，
                # 不塞回去就真的丢了——所以这里必须补一刀再往外抛。
                self._requeue(chunk)
                raise
            if not ok:
                # 发不出去的放回队首，下一轮（冷却后）再试。真放不回去的
                # 由入队时的上限淘汰兜底，至少不会静默消失。
                self._requeue(chunk)
                break
            sent += len(chunk)
            self._log_batch(chunk, data)

        if self._queue:
            self._wake.set()      # 还有没发完的（AI 未结算/被冷却挡住），继续转
        return sent

    def _requeue(self, chunk: list[_Item]) -> None:
        """把没发出去的放回队首，保持原顺序。"""
        for it in chunk:
            it.taken = False
        self._queue[:0] = chunk

    async def _report_with_retry(self, chunk: list[_Item], *, retries: bool = True) -> tuple[bool, dict]:
        """上报一批，带退避重试与冷却。

        网络抖动重试值得（丢一批就是几十条链接），但配置写错重试一万次也没用，
        所以 4xx 直接放弃并长冷却，避免把后端刷成 401 洪流。

        `retries=False` 用在卸载排空：那会儿用户正在等重载完成，后端要是
        不可达，再退避 17 秒就是把"重载插件"变成"卡住 20 秒"。
        """
        last: dict = {}
        delays = (0, *_RETRY_BACKOFF) if retries else (0,)
        for idx, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            ok, data = await self._report(chunk)
            if ok:
                self._fail_streak = 0
                return True, data
            last = data
            msg = str(last.get("msg") or "")
            if msg.startswith("HTTP 4"):
                break
            if idx < len(delays) - 1:
                logger.debug("pantrace: 上报失败（%s），%.0fs 后重试", msg, delays[idx + 1])

        self._fail_streak += 1
        msg = str(last.get("msg") or "")
        if msg.startswith("HTTP 4"):
            self._cooldown_until = time.time() + _COOLDOWN_CONFIG_ERROR
            if time.time() - self._last_config_warn > _AI_WARN_INTERVAL:
                logger.warning(
                    "pantrace: 上报被后端拒绝（%s）。这多半是配置问题——检查 base_url 和 "
                    "ingest_token（要和后端 QQ_INGEST_TOKEN 一致）。%s 秒内不再尝试，"
                    "消息会留在队列里（上限 %s 条）。",
                    msg, _COOLDOWN_CONFIG_ERROR, _QUEUE_CAP,
                )
                self._last_config_warn = time.time()
        elif self._fail_streak >= 3:
            self._cooldown_until = time.time() + _COOLDOWN
            logger.warning(
                "pantrace: 连续 %s 次上报失败（%s），暂停 %s 秒。队列现有 %s 条。",
                self._fail_streak, msg or "未知原因", _COOLDOWN, len(self._queue),
            )
            self._fail_streak = 0
        return False, last

    async def _report(self, chunk: list[_Item]) -> tuple[bool, dict]:
        """把一批消息发给后端。返回 (成功, 返回数据)。"""
        base = self._base_url()
        if not base:
            return False, {"msg": "没配置 base_url"}
        token = str(self.config.get("ingest_token", "") or "").strip()
        if not token:
            return False, {"msg": "没配置 ingest_token（和后端 QQ_INGEST_TOKEN 要一致）"}

        payload = {"messages": [it.payload() for it in chunk]}
        try:
            # 后端慢在**链接数**上，不在消息条数上：一条合集贴塞 100 个链接，
            # 按「1 条消息」只给 30 秒，后端解析到一半就被客户端掐了（今天那次
            # 30+30 秒超时就是这么来的）。所以预算按 消息数 + 链接数 一起算，
            # 上界抬到 180：宁可多等，也别把干了一半的活重做一遍。
            links = sum(len(_URL_IN_TEXT.findall(it.text)) for it in chunk)
            total = max(30, min(180, 15 + 2 * len(chunk) + int(links * 1.2)))
            timeout = aiohttp.ClientTimeout(total=total, sock_connect=10)
            async with self._session_of().post(
                f"{base}/api/ingest/qq",
                json=payload,
                headers={"x-ingest-token": token},
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return False, {"msg": f"HTTP {resp.status}：{text[:120]}"}
                body = await resp.json(content_type=None)
        except aiohttp.ClientConnectorError as exc:
            # 连不上和"连上了但后端慢"要分开：前者查地址/网络，后者查后端。
            return False, {"msg": f"连不上后端（{exc.os_error or exc}）"}
        except asyncio.TimeoutError:
            return False, {"msg": f"上报超时（{total}s 内没等到响应）"}
        except aiohttp.ClientError as exc:
            return False, {"msg": f"{type(exc).__name__}: {exc}"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return False, {"msg": str(exc)[:120]}

        if not isinstance(body, dict) or body.get("code") != 0:
            return False, {"msg": str((body or {}).get("msg", ""))[:120]}
        return True, body.get("data") or {}

    def _log_batch(self, chunk: list[_Item], data: dict) -> None:
        """一批上报完的日志。这一行是日常排障的入口，信息密度要高。"""
        links = int(data.get("links") or 0)
        ai_msgs = sum(1 for it in chunk if it.items)
        ai_items = sum(len(it.items) for it in chunk)
        # 模型中位耗时也带上：这个数从 2 秒漂到 15 秒，就是换模型的信号，
        # 比等到大批消息踩线再回头查划算得多。
        speed = (
            f"，{self._model_hint()}中位 {self._ai_p50():.1f}s"
            if ai_msgs and self._ai_lat else ""
        )
        logger.info(
            "pantrace: 采集 %s 条消息（AI 命中 %s 条 / 提交 %s 个%s）→ 入库 %s 条，"
            "新增 %s / 更新 %s，队列剩余 %s",
            len(chunk), ai_msgs, ai_items, speed, links,
            data.get("added"), data.get("touched"), len(self._queue),
        )

        if links == 0:
            # 零入库是最难查的一种：上报成功、AI 也跑通了，日志里却看不出为什么。
            # 真正的原因在后端的 `dropped` 里，以前这个字段拿到了就丢，
            # 现在一律打出来，并且抬成 WARNING——一次全丢等于这批消息白采。
            reasons = [
                str(d.get("reason") or "")
                for d in (data.get("dropped") or [])
                if isinstance(d, dict)
            ]
            detail = "；".join(dict.fromkeys(r for r in reasons if r)) or "后端未给出原因"
            logger.warning(
                "pantrace: 这批 %s 条消息全部没入库（AI 命中 %s）。后端原因：%s",
                len(chunk), ai_msgs, detail,
            )
        elif links < ai_items:
            # 提交多于入库：模型把同一链接写了两遍（后端折叠），或者链接在
            # 原文里找不到（后端判编造）。不刷 WARNING，但要留痕。
            logger.debug("pantrace: AI 提交 %s 个、入库 %s 个，差额见后端「AI 条目被拒」日志",
                         ai_items, links)

        # AI 总开关是关的，那"AI 没起作用"就不是故障，别刷这条告警
        if self.config.get("ai_structure", True):
            self._log_ai_reasons(chunk)

    def _log_ai_reasons(self, chunk: list[_Item]) -> None:
        """AI 为什么没生效，汇总成一句话，节流输出。

        这是这次重构最实用的改动：以前只有 `AI 结构化 0 条`，剩下的全靠猜。
        现在直接告诉你"没配模型"还是"模型超时"还是"模型说原文没链接"。
        """
        bad = Counter(it.ai_reason for it in chunk if not it.items and it.ai_reason)
        if not bad:
            return
        now = time.time()
        if now - self._last_ai_warn < _AI_WARN_INTERVAL:
            return
        self._last_ai_warn = now
        parts = []
        for reason, count in bad.most_common(3):
            hint = _AI_REASON_HINT.get(reason, reason)
            parts.append(f"{hint}×{count}")
        logger.warning(
            "pantrace: AI 结构化没起作用（%s）—— %s", self._ai_speed(), "；".join(parts),
        )

    # ------------------------------------------------------------ 入队

    def _enqueue(self, item: _Item) -> None:
        if len(self._queue) >= _QUEUE_CAP:
            # 队列顶到上限说明后端长时间不可用（或网络断了）。以前是
            # `self._pending[-200:]` 静默截断，跑一周也不知道丢了什么。
            dropped = self._queue.pop(0)
            self._overflow_dropped += 1
            logger.warning(
                "pantrace: 待发队列已满（%s 条），丢弃最老的一条（群 %s / %s）。"
                "累计丢弃 %s 条，先查后端是否可达。",
                _QUEUE_CAP, dropped.group_id, dropped.sender_name or dropped.sender_id,
                self._overflow_dropped,
            )

        ai_on = bool(self.config.get("ai_structure", True))
        if ai_on and self._ai_inflight < _AI_MAX_INFLIGHT:
            self._start_ai(item)
        else:
            item.ai_state = "skipped"
            item.ai_reason = "overload" if ai_on else "disabled"

        self._queue.append(item)
        self._ensure_flusher()
        self._wake.set()

    # ------------------------------------------------------------ 事件入口

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
                # 只给某个群的人配了白名单、却没把这个群放进 capture_groups：
                # 两处配置是「与」，人不比群大，一条都不会采。这属于配了等于
                # 没配，静默吞掉最坑，所以撞上就明说一次。
                _, senders_by_group = self._watched_senders()
                if senders_by_group.get(group_id) and group_id not in self._warned_cold_group:
                    logger.warning(
                        "pantrace: capture_senders 里配了群 %s 的人，但这个群不在 "
                        "capture_groups 里，该群一条都不会采集。把群号补进 capture_groups "
                        "才生效——注意群一旦开启采集，群里群主/管理员的分享也会照收。",
                        group_id,
                    )
                    self._warned_cold_group.add(group_id)
                return

            sender_id = str(event.get_sender_id() or "")
            # 自己发的消息按普通成员处理不了，直接跳过（免得回声）
            if sender_id and sender_id == str(event.get_self_id() or ""):
                return

            # 指定的人先放行：名单命中的不论群身份，也不再查群成员接口——
            # 群里刷屏时那次请求是实打实的延迟。拿不到原始 role 就记 member。
            senders_all, senders_by_group = self._watched_senders()
            named = bool(sender_id) and (
                sender_id in senders_all or sender_id in senders_by_group.get(group_id, ())
            )
            if named:
                role = self._raw_role(event) or "member"
            else:
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
                if message_id in self._seen:
                    return
                self._seen[message_id] = time.time()
                while len(self._seen) > _SEEN_CAP:
                    self._seen.popitem(last=False)

            # 记下会话标识，后台 AI 补齐时用它问「这个会话该用哪个模型」
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if umo:
                self._last_umo = umo

            self._enqueue(_Item(
                id=message_id,
                group_id=group_id,
                sender_id=sender_id,
                sender_name=event.get_sender_name() or "",
                role=role,
                # 正文和卡片都带上：卡片补标题，正文补链接。同一条链接出现
                # 两次在群里很常见，这里先塌成一次，后端不用重复解析。
                text=_collapse_same_url(f"{text}\n{cards}".strip()),
                created=time.time(),
            ))

            if self.config.get("capture_ack", False):
                yield event.plain_result("已收录，稍后入库。")
        except Exception as exc:  # noqa: BLE001
            # 采集链路任何异常都不许冒到事件循环：它挂掉会连带影响群里
            # 其他插件和 LLM 的正常回复
            logger.warning("pantrace: 群消息采集异常：%s", exc)

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """插件加载完先自报一句：这次用的是哪个模型。

        以前要等群里来第一条消息、模型跑完一轮，日志里才看得出模型是谁——
        而"AI 结构化没生效"这类问题，第一时间就想知道配的到底是哪个。
        取不到名字不算失败（AstrBot 可能还没把提供商准备好），留给第一条
        消息再试一次。
        """
        try:
            label = await self._note_model(await self._current_provider_id())
        except Exception as exc:  # noqa: BLE001
            logger.debug("pantrace: 启动时取模型名失败：%s", exc)
            label = ""
        if not label:
            self._model_probed_at = 0.0   # 别让启动时的一次失败占住重试冷却

    async def terminate(self):
        """插件卸载/重载时清理。

        顺序很重要：先停发送器（它是唯一出口），再取消 AI 补齐，最后把队列
        里剩下的**直接**发出去。以前是先 cancel 在途任务，那一批的消息随任务
        一起消失——重载一次丢一次数据，而且日志里毫无痕迹。
        """
        self._stopping = True
        self._wake.set()

        if self._flusher and not self._flusher.done():
            try:
                await asyncio.wait_for(self._flusher, timeout=2)
            except asyncio.TimeoutError:
                # 卡在 HTTP 上出不来。取消它——在途那批会由 _flush_once 的
                # CancelledError 分支塞回队列，下面统一排空。
                self._flusher.cancel()
                try:
                    await self._flusher
                except BaseException:  # noqa: BLE001
                    pass
            except BaseException:  # noqa: BLE001
                pass
        self._flusher = None

        for task in list(self._ai_tasks):
            task.cancel()
        if self._ai_tasks:
            await asyncio.gather(*list(self._ai_tasks), return_exceptions=True)
        self._ai_tasks.clear()

        if self._queue:
            left = len(self._queue)
            deadline = time.time() + 15
            while self._queue:
                remaining = deadline - time.time()
                if remaining <= 0.5:
                    break
                try:
                    # 再套一层时间闸：后端挂住不响应时，单个 HTTP 本身就有
                    # 几十秒超时，不能靠循环条件去兜。
                    done = await asyncio.wait_for(
                        self._flush_once(force=True), timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pantrace: 卸载时补发失败：%s", exc)
                    break
                if not done:
                    break
            if self._queue:
                logger.warning("pantrace: 卸载时还有 %s 条没发出去（原有 %s 条）", len(self._queue), left)
            else:
                logger.info("pantrace: 卸载前把队列里的 %s 条全部发完了", left)

        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
        self._session = None
