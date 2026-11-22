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

# 一次 flush 里最多给几条消息过模型。15 秒内批量进来几十条时多半是刷屏或
# 广告轰炸，全部送模型既慢又费钱；超出的直接走正则，链接一样入库。
_AI_MAX_PER_FLUSH = 10
# 并发上限。群里连发时几条消息同时结构化，但别把模型服务的限流打满。
_AI_CONCURRENCY = 3
# 单条消息送模型的字符上限。资源合集贴动辄两三千字，早先只给 2000 字会把
# 后半段链接整段切掉——模型看不见就没法结构化，纯 AI 策略下这部分直接丢失。
# 10000 字约等于一条超长合集贴的全文，正常分享根本碰不到。
_AI_TEXT_MAX = 10000
# 单条消息最多采纳多少条 AI 条目。这是「防模型发疯灌几百条」的护栏，
# 不是「只处理前 N 个」的截断：100 个链接一条消息里已经极端罕见。
_AI_MAX_ITEMS = 100


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


class PanTraceCollector(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self._pending: list[dict] = []        # 待上报消息队列
        self._flush_task: asyncio.Task | None = None
        self._seen: OrderedDict[str, float] = OrderedDict()   # message_id 去重窗口
        self._roles: dict[str, tuple[str, float]] = {}        # (群:人) -> (角色, 时间)
        self._warned_no_group = False          # 「没配群号」只提醒一次
        self._warned_no_llm = False            # 「没取到大模型」只提醒一次
        # 配了具体的人、但这个群没开采集的群号。按群只提醒一次。
        self._warned_cold_group: set[str] = set()
        # 最近一条群消息的会话标识。结构化在后台任务里跑，手上没有 event，
        # 只能借用它去问 AstrBot「这个会话该用哪个模型」。空串就退回全局默认。
        self._last_umo = ""

    def _base_url(self) -> str:
        return str(self.config.get("base_url", "")).strip().rstrip("/")

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

    # ------------------------------------------------------------ AI 结构化

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

    async def _llm_text(self, prompt: str, system: str) -> str | None:
        """用 AstrBot 里已配置的大模型跑一次补全。

        返回值的三种情况必须分开，否则日志会指错方向：
        - `None`：这台 AstrBot 上没有可用模型 → 值得提示用户去配置；
        - `""`：有模型，但调用失败或返回空文本 → 用户去配置模型也修不好；
        - 非空：正常结果。

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
            return None
        return await self._provider_chat(provider, prompt, system)

    def _ai_timeout(self) -> int:
        return max(5, min(60, int(self.config.get("ai_timeout", 60))))

    async def _structure_one(self, text: str) -> list[dict]:
        """一条消息 → `[{title, shareUrl, sharePwd}]`。失败返回空列表。"""
        text = str(text or "").strip()
        if not text:
            return []
        prompt = f'群消息原文（其中可能夹着图片/卡片占位符，忽略即可）：\n"""\n{text[:_AI_TEXT_MAX]}\n"""'
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

        if raw is None:
            if not self._warned_no_llm:
                logger.warning(
                    "pantrace: 没取到大模型，AI 结构化已跳过，本次改用规则解析。"
                    "请到 WebUI「模型提供商」里配置并启用一个模型；"
                    "不需要这个功能就把插件配置里的「AI 结构化」关掉。"
                )
                self._warned_no_llm = True
            return []
        if not raw:
            # 有模型但这次没吐出文本（拒答、被截断、接口抖动）。这时提示
            # "去配置模型"是错的——模型是配好的，只是这一条没拿到结果。
            logger.debug("pantrace: 模型返回空文本，本条改用规则解析")
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
        # 外层预算是"整批"的，但内层并发只有 _AI_CONCURRENCY 条，超出并发的
        # 消息得排队等前一轮。原来外层直接复用单条预算：批里凑够 4 条就必然
        # 先撞外层——前半批明明已经拿到了结构化结果，却被整体判成超时丢掉。
        # 现在按轮次给预算，外层只当"整批彻底卡死"的保险丝，单条闸门在内层。
        rounds = max(1, (len(todo) + _AI_CONCURRENCY - 1) // _AI_CONCURRENCY)
        budget = self._ai_timeout() * rounds + 5
        try:
            await asyncio.wait_for(
                asyncio.gather(*(one(m) for m in todo), return_exceptions=True),
                timeout=budget,
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

    # ------------------------------------------------------------ 上报

    async def _report(self, messages: list[dict]) -> tuple[bool, dict]:
        """把一批消息发给后端。返回 (成功, 返回数据)。"""
        base = self._base_url()
        if not base:
            return False, {"msg": "没配置 base_url"}
        token = str(self.config.get("ingest_token", "") or "").strip()
        if not token:
            return False, {"msg": "没配置 ingest_token（和后端 QQ_INGEST_TOKEN 要一致）"}

        try:
            # 一批可能几十上百条消息，合集贴能让 payload 上到几 MB，后端还要逐条
            # 解析、核对、写库。按条数给超时（10 + N 秒，封顶 120），别用一个固定值
            # 去赌——超时会整批重试一次，等于把同样的活干两遍。
            timeout = aiohttp.ClientTimeout(total=max(30, min(120, 10 + len(messages))))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{base}/api/ingest/qq",
                    json={"messages": messages},
                    headers={
                        "x-ingest-token": token,
                        "User-Agent": "AstrBot-PanTrace/2.0",
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
        # 后端已经不限制单批条数（QQ_INGEST_MAX_BATCH=0），这里把手上的全发走。
        # 真正的上界是 _schedule_flush 里的 200 条内存保护，不是接口限制。
        batch, self._pending = self._pending[:], []
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
        # "AI 结构化 X 条"只说明模型跑通了，看不出最终入库多少。这里把
        # 提交数和入库数一起打出来。两者不等有两个原因：模型把同一链接写两遍
        # （后端折叠）、或者链接在原文里找不到（后端判编造）。明细看后端日志。
        # AI 没覆盖到的链接会被直接丢弃，那个数插件侧看不到（插件不做正则），
        # 只能去后端看「纯 AI 策略丢弃 N 条」。
        links = int(data.get("links") or 0)
        ai_items = sum(len(m.get("items") or []) for m in batch)
        extra = "（AI 提交 %s）" % ai_items if ai_items else "（全部走规则解析）"
        logger.info(
            "pantrace: 采集 %s 条消息（AI 结构化 %s 条）→ 入库 %s 条%s，新增 %s / 更新 %s",
            len(batch), ai_done, links, extra, data.get("added"), data.get("touched"),
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
            # 采集链路任何异常都不许冒到事件循环：它挂掉会连带影响群里
            # 其他插件和 LLM 的正常回复
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
