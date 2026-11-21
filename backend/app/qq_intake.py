"""QQ 群消息 → 待入库资源。

和 `intake.py`（表格导入）的分工：

- **共同**：链接提取、提取码就近分配、标题清洗，全部复用 `search.clean` 与
  `intake.assign_codes`。项目里只允许存在一套 URL 正则——这条不能破，否则
  今天群消息能认出来的写法，明天表格导入认不出来。
- **不同**：表格是"一行一条、列位固定"；聊天消息是"标题可能独占一行、链接
  可能独占一行、一条消息里挂好几个盘"。只有这一层语义需要单独写。

标题归属规则（群里九成的分享都是这个写法）：

    三体 全集                      ← 标题行，进 pending
    https://pan.quark.cn/s/aaa    ← 链接行没有自带标题，用 pending
    https://pan.baidu.com/s/bbb   ← 同一段继续复用 pending
    （空行）
    活着                           ← 空行清段，pending 换人
    https://pan.quark.cn/s/ccc

**空行分段是必须的**。不分段的话，上一段的标题会漏给下一段没有自带标题的
链接，落库后表现为"点开《三体》是《活着》"——搜得到、点开错，是最难查的
一类脏数据，而且 `_prepare` 按 `platform:pwd_id` 去重也救不回来。

另一条硬规则：**链接行自带标题时，pending 不动**。不这么做的话，一条消息里
先给个总标题再逐条列「第一季 https://…」「第二季 https://…」，总标题会被
第一条消耗掉，后面的全变占位。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .intake import _CODE_ONLY, assign_codes, clean_title
from .search import clean
from .sdk import urls

log = logging.getLogger("pantrace.qq_intake")

# 单条消息上限。正常分享文案几十字，超过这个量级基本是刷屏广告或误粘贴了
# 整篇文章，截断比解析更有用。
MAX_TEXT = 8000
MAX_TITLE = 120

# CQ 码。AstrBot 给的是纯文本，但 NapCat 某些配置下会把 CQ 码原样带进来。
_CQ = re.compile(r"\[CQ:[^\]]*\]")
# QQ 客户端的占位符（图片/表情/卡片）。留着会污染标题。
_PLACEHOLDER = re.compile(
    r"\[(?:图片|表情|动画表情|语音|视频|文件|卡片消息|回复|@[^\]]*|图片表情)\]"
)
# 行首字段名标注："片名：三体" 里只剥「片名：」，不动标题本体。
# 只认这几个明确是字段名的词，别把「网盘」也剥了——"百度网盘 三体"是标题。
_LEAD_LABEL = re.compile(
    r"^\s*(?:链接|下载地址|地址|分享链接|资源链接|分享|资源|片名|剧名|书名|名称|标题"
    r"|提取码|访问码|密码|pwd|code|url|link)\s*[:：]\s*",
    re.I,
)
# 元信息行："导演：辛爽" "年份：2023" "大小：18.7G"。
# 这类行**不能参与标题竞争**。只靠"取更长的"是不够的——「年份：2023」比
# 「漫长的季节」长一个字，按长度规则它会赢，然后一整条资源就顶着
# "年份：2023" 入库了。能搜到链接、搜不到片名，没人会去查。
#
# 「片名/剧名/名称/标题」不在此列：它们是标题字段，剥掉字段名后剩下的
# 才是真标题（由 `_LEAD_LABEL` 处理）。
_META_LINE = re.compile(
    r"^\s*(?:导演|主演|演员|编剧|制片|出品|年份|年代|地区|国家|类型|题材|语言|字幕"
    r"|集数|季数|更新时间|更新|大小|体积|清晰度|分辨率|画质|格式|简介|标签"
    r"|评分|豆瓣|时长|码率|来源|整理)\s*[:：]",
)
# 整行只剩这些词就等于没标题（"链接"、"点击打开"、"提取码如下"）。
# 长词在前，先削长的，避免「下载地址」被「下载」切一半。
_LABEL_WORDS = (
    "下载地址", "分享链接", "资源链接", "网盘链接", "访问码", "提取码", "点击打开",
    "点击这里", "链接", "地址", "网盘", "分享", "下载", "提取", "密码",
    "url", "link", "pwd", "code", "点击", "打开", "戳", "这里", "如下", "在下面",
)
# 平台名单独一行不是标题。群里最常见的写法就是
#     「三体 全集 / 夸克 https://… / 百度 https://…」
# 每行前面那个"夸克/百度"是盘标，不是资源名。不排掉的话，多盘备份会变成
# 三条分别叫「夸克」「百度」的垃圾标题，而且总标题一次都用不上。
#
# 这里**不做全局替换**、只做整串相等判断：`uc` / `115` 这类短词当正则去削
# 正文会误伤（"Luck" 里的 uc），整串比对没有这个问题。
_PLATFORM_ONLY = frozenset({
    "夸克", "夸克网盘", "百度", "百度网盘", "百度云", "阿里", "阿里云盘", "阿里云",
    "uc", "uc网盘", "迅雷", "迅雷网盘", "115", "115网盘", "123", "123云盘",
    "天翼", "天翼云盘", "天翼云", "移动", "移动云盘", "139", "联通", "联通云盘",
    "光鸭", "光鸭云盘", "蓝奏", "蓝奏云", "微云", "城通", "城通网盘",
    "pikpak", "磁力", "网盘链接",
})


@dataclass
class MessageResult:
    """一条消息的解析结果。"""

    items: list[dict] = field(default_factory=list)
    # 丢弃原因。宁可报出来让人去查，也不要静默吞掉——群里没人会回头核对。
    skipped: list[str] = field(default_factory=list)

    @property
    def links(self) -> int:
        return len(self.items)


def _clean_text(text: str) -> str:
    """剥掉 CQ 码、占位符和 BOM，统一换行。不改动正文内容。"""
    t = str(text or "")
    t = t.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    t = _CQ.sub(" ", t)
    t = _PLACEHOLDER.sub(" ", t)
    return t[:MAX_TEXT]


def _as_title(line: str) -> str:
    """把一行洗成可当标题的文本；洗不出东西就返回空串。

    判据是「剥掉标签词和符号之后还剩至少两个字」。这条比"长度够不够"准：
    "链接："、"提取码如下"、"点击打开"都短，但更关键的是它们**没有实词**。
    """
    line = str(line or "")
    # 元信息行直接出局，不参与标题竞争（否则「年份：2023」会靠长度赢）
    if _META_LINE.match(line):
        return ""
    candidate = clean_title(_LEAD_LABEL.sub("", line))
    if not candidate:
        return ""
    core = candidate
    for word in _LABEL_WORDS:
        core = re.sub(re.escape(word), "", core, flags=re.I)
    core = re.sub(r"[\s:：\-—_·.。!！?？,，/|｜\[\]【】()（）]+", "", core)
    if len(core) < 2:
        return ""
    # 只剩一个提取码（"提取码：abcd" 剥完就是 abcd）不是标题。
    # 单独出现的码行在群里非常普遍，漏这条会把上一条真标题顶掉。
    if _CODE_ONLY.match(core):
        return ""
    # 只剩平台名（"夸克" / "百度网盘"）不算标题
    if core.lower() in _PLATFORM_ONLY:
        return ""
    return candidate[:MAX_TITLE]


def parse_message(
    text: str,
    *,
    source_name: str = "",
    source_level: int = 1,
) -> MessageResult:
    """一条群消息 → 待入库条目。

    `source_level` 默认 1：群主/管理员手动发出来的链接是人工筛过的，
    质量理应高于上游爬取（3）和影子回填（2）。和 TG 自建频道同级。
    """
    res = MessageResult()
    raw = _clean_text(text)
    if not raw.strip():
        res.skipped.append("空消息")
        return res

    lines = raw.splitlines()
    # 按行提取，不拿整段的结果当入库清单。
    # `extract_net_disk_links` 的夸克/迅雷段用「互相包含」判重（那是为了合并
    # 同一条链接的不同尾巴），跨行用会把 `/s/xyz` 和下一行的 `/s/xyz2` 当成
    # 同一条丢掉——分享 id 是随机的，这种前缀关系不常见，但一旦撞上就是
    # 第二条资源连同提取码一起消失，且日志里看不出任何异常。
    line_links = [clean.extract_net_disk_links(line) for line in lines]
    every_link = [u for group in line_links for u in group]
    if not every_link:
        res.skipped.append("没有网盘链接")
        return res

    # 提取码全局就近分配：整段扫，码在链接右优先，一码只给一链。
    # 跨行也认——"链接在第 2 行、提取码在第 3 行"是群里最标准的写法。
    codes = assign_codes(raw, list(dict.fromkeys(every_link)))

    pending = ""
    pending_used = False
    for line, links in zip(lines, line_links):
        if not line.strip():
            pending, pending_used = "", False   # 空行清段，标题不许越界
            continue

        if not links:
            cand = _as_title(line)
            if cand:
                # 段内已有链接消费过这个标题 → 新标题行说明换资源了，直接换。
                # 还没被消费 → 连续两条标题行大概率是「大标题 + 导演/年份」这类
                # 元信息（群里极常见），取更长的那个；让"导演：辛爽"顶掉片名
                # 是这条链路最容易犯、又最难被发现的错。
                if pending_used or len(cand) > len(pending):
                    pending, pending_used = cand, False
            continue

        # 链接行：先把链接抠掉，剩下的部分才可能是标题
        rest = line
        for one in links:
            rest = rest.replace(one, " ")
        inline = _as_title(rest)
        title = inline or pending
        if not inline and pending:
            pending_used = True   # inline 优先时不动 pending（见模块头注释）

        for one in links:
            parsed = urls.parse(one)
            if not parsed.ok:
                res.skipped.append(f"识别不出分享 id：{one[:60]}")
                continue
            code = codes.get(one) or parsed.passcode
            display = title or f"{parsed.platform} 分享 {parsed.pwd_id[:8]}"
            res.items.append({
                "shareUrl": parsed.url or one,
                "title": display,
                "platform": parsed.platform,
                "sharePwd": code,
                "fileCount": 0,
                "sizeBytes": 0,
                "sourceName": source_name,
                "sourceKind": "qq",
                "sourceLevel": source_level,
            })

    return res


# ---------------------------------------------------------------- AI 结构化
#
# 群里很多分享是「一段介绍 + 几张图 + 标题写两行 + 提取码隔了三行」，
# 规则解析能抠出链接，但标题常常是半截或干脆是广告词。AstrBot 那边配了
# 大模型，让它把消息整理成 `{title, shareUrl, sharePwd}` 明显更准。
#
# 但**模型不能是可信来源**。它的三种典型错法都能直接造成脏数据：
#   1. 把链接补全 / 改写（少个字符就是死链，用户点开报"分享已失效"）；
#   2. 把短 id 记错，拼出一条原文没有的链接；
#   3. 原文没写提取码，它照着常见规律猜一个。
# 所以这里只把 AI 当"标题改写器"用：**链接必须以原文为准**，模型给的链接
# 必须能在原文里找到同一个 `platform:pwd_id`，否则整条丢掉；提取码也必须
# 在原文里出现过，否则退回规则解析的结果。
#
# 另一半同样重要：**模型漏掉的链接照样入库**。结构化是为了让标题更准，
# 不是让它决定"哪条资源值得收"。漏一条就是用户搜不到，而日志里毫无痕迹。

_PWD_SHAPE = re.compile(r"^[A-Za-z0-9]{2,12}$")


def _pick_pwd(value: object, text: str, fallback: str) -> str:
    """AI 给的提取码要能在原文里找到，否则用规则解析的结果。"""
    pwd = str(value or "").strip()
    if not pwd:
        return fallback
    if _PWD_SHAPE.match(pwd) and pwd.lower() in text.lower():
        return pwd
    return fallback


def merge_ai_items(
    text: str,
    ai_items: list[dict] | None,
    *,
    source_name: str = "",
    source_level: int = 1,
) -> MessageResult:
    """把 AI 的结构化结果和规则解析的结果合并。

    规则解析先跑一遍，不只是为了兜底，更是为了拿到"原文里到底有哪些链接"
    这份证据——AI 的每一条都要对着它验。代价是一次纯正则，可以忽略。
    """
    base = parse_message(text, source_name=source_name, source_level=source_level)
    res = MessageResult()
    if not base.items:
        res.skipped.extend(base.skipped or ["没有网盘链接"])
        return res

    by_key: dict[str, dict] = {}
    for item in base.items:
        parsed = urls.parse(str(item.get("shareUrl") or ""))
        if parsed.ok:
            by_key[f"{parsed.platform}:{parsed.pwd_id}"] = item

    raw = _clean_text(text)
    used: set[str] = set()
    for one in ai_items or []:
        if not isinstance(one, dict):
            continue
        parsed = urls.parse(str(one.get("shareUrl") or one.get("url") or "").strip())
        if not parsed.ok:
            res.skipped.append("AI 返回的链接不合法")
            continue
        key = f"{parsed.platform}:{parsed.pwd_id}"
        if key not in by_key:
            res.skipped.append("AI 返回的链接不在原文里（疑似编造）")
            continue
        if key in used:          # 模型把同一条链接写了两遍
            continue
        used.add(key)
        origin = by_key[key]
        # 标题以 AI 为准，但它给空（或只剩标点）时退回规则结果
        title = clean_title(str(one.get("title") or ""))[:MAX_TITLE]
        res.items.append({
            **origin,
            "title": title or str(origin.get("title") or ""),
            "sharePwd": _pick_pwd(one.get("sharePwd"), raw, str(origin.get("sharePwd") or "")),
        })

    for key, item in by_key.items():
        if key not in used:
            res.items.append(dict(item))

    # AI 条目被拒必须留痕：不然"为什么这条标题没用模型结果"只能靠猜。
    # 只记链接本身被否的情况，标题为空属于正常降级，不刷日志。
    ai_rejected = [r for r in res.skipped if r.startswith("AI ")]
    if ai_rejected:
        log.info(
            "QQ 采集：AI 条目被拒 %s 条（%s），已按规则解析入库",
            len(ai_rejected), "；".join(dict.fromkeys(ai_rejected)),
        )
    return res


def parse_batch(
    messages: list[dict],
    *,
    default_source: str = "",
) -> tuple[list[dict], list[dict]]:
    """批量解析。返回 `(items, dropped)`。

    `messages` 每项形如 `{text, groupName, groupId, senderName, role, id}`。
    `dropped` 保留消息 id 和原因，方便在管理端对账："这条为什么没进库"。

    带 `items` 的消息（AstrBot 侧已经用大模型结构化过）走 `merge_ai_items`，
    **仍然不跳过规则解析**——那份结果既是兜底，也是校验 AI 的依据。
    """
    items: list[dict] = []
    dropped: list[dict] = []
    for msg in messages:
        text = str(msg.get("text") or "")
        if not text.strip():
            dropped.append({"id": msg.get("id", ""), "reason": "空消息"})
            continue
        # 来源名优先用群名：落库后在管理端的「来源」列能一眼看出是哪个群
        source = str(msg.get("groupName") or default_source or "").strip()
        if not source and msg.get("groupId"):
            source = f"QQ群{msg['groupId']}"
        ai_items = msg.get("items") or []
        if ai_items:
            got = merge_ai_items(text, ai_items, source_name=source)
        else:
            got = parse_message(text, source_name=source)
        if not got.items:
            dropped.append({
                "id": msg.get("id", ""),
                "reason": got.skipped[0] if got.skipped else "无有效链接",
            })
            continue
        items.extend(got.items)
    return items, dropped
