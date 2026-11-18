"""t.me 频道页解析。

逐行翻译 pansou `util/parser_util.go:146-928`。选择器、判定顺序、字符串切分规则
一律照抄，改一处就会和上游行为分叉。

唯一无法照搬的是"字节"语义：Go 的 `len()` / 切片按字节，Python 按字符。
凡是影响判定的地方（窗口长度、前缀判断）这里显式换成字节；纯切分的
`line[len("名称："):]` 保持字符切分，因为切点必然落在字符边界上，结果等价。

依赖 lxml + cssselect —— 与 goquery 是同一类东西（DOM + CSS 选择器），
选择器字符串可以原样粘贴。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from lxml import etree, html

from ..clean import (
    LinkCandidates,
    extract_net_disk_links,
    get_link_type,
    is_supported_link,
    normalize_url_for_comparison,
    password_for,
)
from ..types import Link, SearchResult

# parser_util.go:146
PARSE_STATUS_OK = "ok"
PARSE_STATUS_NO_MESSAGES = "no_messages"
PARSE_STATUS_STRUCTURE_CHANGED = "structure_changed"


class TgParseError(Exception):
    """parser_util.go:172 —— 建 DOM 就失败，等价于 goquery.NewDocumentFromReader 报错。"""


def _parse_doc(page: str) -> html.HtmlElement:
    try:
        return html.fromstring(page)
    except (etree.ParserError, ValueError) as exc:  # 空文档、纯空白
        raise TgParseError(str(exc)) from exc


def _css(elem, selector: str) -> list:
    """goquery 的 Find(sel)。cssselect 是后代匹配，语义一致。"""
    return elem.cssselect(selector)


# ---------------------------------------------------------------- 入口


def parse_search_results(page: str, channel: str) -> list[SearchResult]:
    """parser_util.go:161 `ParseSearchResults`。

    第二个返回值在 Go 里是"翻页参数占位，恒为空串、从未实现"，这里直接不返回。
    """
    results, _status = parse_search_results_with_status(page, channel)
    return results


def parse_search_results_with_status(page: str, channel: str) -> tuple[list[SearchResult], str]:
    """parser_util.go:169 `ParseSearchResultsWithStatus`。"""
    doc = _parse_doc(page)

    results: list[SearchResult] = []
    # 统计"能被识别为一条消息"的块数（有 data-post + 能拆 id + 有可解析时间）
    recognized = 0

    for wrap in doc.cssselect(".tgme_widget_message_wrap"):
        result, ok, has_result = parse_tg_message(wrap, channel)
        if not ok:
            continue
        recognized += 1
        if has_result:
            results.append(result)

    return results, judge_parse_status(doc, recognized, len(results))


def judge_parse_status(doc, recognized: int, result_count: int) -> str:
    """parser_util.go:197 `judgeParseStatus`。

    不能用 `result_count == 0` 判失效：消息识别成功但整条不含受支持的网盘链接时
    同样得到 0 条，那是正常页面。只有"页面里明明有消息块，却一个都识别不出来"
    才说明结构变了。
    """
    if result_count != 0:
        return PARSE_STATUS_OK
    if doc.cssselect(".tme_no_messages_found"):
        return PARSE_STATUS_NO_MESSAGES
    if recognized == 0 and doc.cssselect(".tgme_widget_message_wrap"):
        return PARSE_STATUS_STRUCTURE_CHANGED
    return PARSE_STATUS_OK


def parse_tg_message(wrap, channel: str) -> tuple[SearchResult, bool, bool]:
    """parser_util.go:215 `parseTgMessage`。

    ok        = 能识别为一条消息（有 data-post、拆得出 id、有可解析时间）
    has_result= 这条消息含受支持的网盘链接
    两者是两件事：识别成功但无链接的消息正常丢弃，不能据此判断页面结构变了。
    """
    message_div = _first(wrap, ".tgme_widget_message")
    if message_div is None:
        return SearchResult(), False, False

    # 提取消息ID
    if "data-post" not in message_div.attrib:
        return SearchResult(), False, False
    data_post = message_div.attrib.get("data-post", "")

    parts = data_post.split("/")
    if len(parts) != 2:
        return SearchResult(), False, False

    message_id = parts[1]
    unique_id = channel + "_" + message_id

    # 提取时间
    date_node = _first(message_div, ".tgme_widget_message_date time")
    if date_node is None or "datetime" not in date_node.attrib:
        return SearchResult(), False, False
    time_str = date_node.attrib.get("datetime", "")

    try:
        dt = datetime.fromisoformat(time_str)
    except ValueError:
        return SearchResult(), False, False

    # 获取消息文本元素
    message_text_elem = _first(message_div, ".tgme_widget_message_text")

    # 直接从 DOM 拼出带换行的正文（<br> 记为换行）
    text_with_breaks = message_text_with_breaks(message_text_elem)

    # 纯文本内容
    message_text = _text_of(message_text_elem)

    title = extract_title(text_with_breaks, message_text)

    links = collect_message_links(wrap, message_text, text_with_breaks)
    tags = extract_message_tags(message_text_elem)
    images = extract_message_images(message_div)

    # 只有包含链接的消息才添加到结果中
    if not links:
        return SearchResult(), True, False

    links = extract_work_titles_for_links(links, message_text, title)

    return (
        SearchResult(
            unique_id=unique_id,
            message_id=message_id,
            channel=channel,
            datetime=dt.timestamp(),
            title=title,
            content=message_text,
            links=links,
            tags=tags,
            images=images,
        ),
        True,
        True,
    )


def _first(elem, selector: str):
    """goquery 的 Find(sel).First() 语义：取第一个匹配。"""
    if elem is None:
        return None
    found = elem.cssselect(selector)
    return found[0] if found else None


def _text_of(elem) -> str:
    """goquery 的 Text()：拼接所有后代文本节点，不插换行。"""
    if elem is None:
        return ""
    return elem.text_content()


# ---------------------------------------------------------------- 链接


def collect_message_links(wrap, message_text: str, text_with_breaks: str) -> list[Link]:
    """parser_util.go:397 `collectMessageLinks`。

    两个来源缺一不可：正文里的 <a>，以及内联按钮里的 <a href>。
    按钮文字也当密码上下文 —— 有些频道把提取码写在按钮上。
    """
    cand = LinkCandidates()

    anchors = _css(
        wrap,
        ".tgme_widget_message_text a, .tgme_widget_message_inline_keyboard a[href]",
    )
    for a in anchors:
        if "href" not in a.attrib:
            continue
        href = a.attrib.get("href", "")
        if not is_supported_link(href):
            continue

        # 某些频道会把提取码写在按钮文字中，因此同时使用正文和按钮标签作为密码提取上下文
        button_text = _text_of(a).strip()
        password_context = message_text
        if button_text:
            password_context += "\n" + button_text

        # 按钮链接的最近上下文是按钮标签本身；正文里若也写了这个链接，则以正文为准
        cand.add(
            get_link_type(href),
            href,
            password_for(href, button_text + "\n" + text_with_breaks, password_context),
        )

    # 从纯文本里再捞一遍。主上下文用**带换行的**正文：extract_password 拿到的若是
    # 折叠成一行的 Text()，按锚定位就无从谈起。
    for link_url in extract_net_disk_links(message_text):
        cand.add(get_link_type(link_url), link_url, password_for(link_url, text_with_breaks, message_text))

    return cand.finalize()


# ---------------------------------------------------------------- 标签 / 图片


def extract_message_tags(message_text_elem) -> list[str]:
    """parser_util.go:541 `extractMessageTags`。"""
    tags: list[str] = []
    if message_text_elem is None:
        return tags
    for a in message_text_elem.cssselect("a[href^='?q=%23']"):
        tag = _text_of(a)
        if tag.startswith("#"):
            tags.append(tag[1:])
    return tags


def extract_message_images(message_div) -> list[str]:
    """parser_util.go:553 `extractMessageImages`。

    只看 bubble 内部，因此天然排除了 `.tgme_widget_message_user` 里的头像。
    """
    images: list[str] = []
    found: set[str] = set()

    bubble = _first(message_div, ".tgme_widget_message_bubble")
    if bubble is None:
        return images

    # 1) 内容图片包装元素的 style 里的 background-image
    for photo_wrap in bubble.cssselect(".tgme_widget_message_photo_wrap"):
        style = photo_wrap.attrib.get("style")
        if style is not None:
            url = extract_image_url_from_style(style)
            if url and url not in found:
                found.add(url)
                images.append(url)

    # 2) bubble 内其它 <img>（头像不在 bubble 内，不会混进来）
    for img in bubble.cssselect("img"):
        src = img.attrib.get("src")
        if src and src not in found:
            found.add(src)
            images.append(src)

    return images


def extract_image_url_from_style(style: str) -> str:
    """parser_util.go:600 `extractImageURLFromStyle`。三种引号形态依次尝试。"""
    for quote in ("'", '"'):
        start_pattern = "background-image:url(" + quote
        start = style.find(start_pattern)
        if start != -1:
            start += len(start_pattern)
            end = style.find(quote + ")", start)
            if end != -1:
                return style[start:end]

    # 无引号格式
    start_pattern = "background-image:url("
    start = style.find(start_pattern)
    if start != -1:
        start += len(start_pattern)
        end = style.find(")", start)
        if end != -1:
            return style[start:end].strip("'\"")
    return ""


# ---------------------------------------------------------------- 正文 / 标题


def message_text_with_breaks(sel) -> str:
    """parser_util.go:652 `messageTextWithBreaks`。

    <br> 记换行，注释丢弃，其它元素取子文本。lxml 已把字符引用解码进 .text，
    无需再解实体（这一点和 Go 的 html 解析器行为一致）。
    """
    if sel is None:
        return ""
    out: list[str] = []

    def walk(el) -> None:
        if el.text:
            out.append(el.text)
        for child in el:
            tag = child.tag
            if not isinstance(tag, str):
                # 注释 / 处理指令：Go 里是非 ElementNode，直接丢；其后的文本在 Python
                # 里是 tail，仍需写出
                if child.tail:
                    out.append(child.tail)
                continue
            if tag.lower() == "br":
                out.append("\n")
            else:
                walk(child)
            if child.tail:
                out.append(child.tail)

    walk(sel)
    return "".join(out)


def extract_title(text_with_breaks: str, text_content: str) -> str:
    """parser_util.go:682 `extractTitle`。

    有些频道第一行是"📅 9月9日"之类的日期头，真正的作品名在下一行；
    把日期当标题会让服务层的关键词过滤把按钮链接全过滤掉。
    """
    if text_with_breaks:
        for line in text_with_breaks.split("\n"):
            line = line.strip()
            if not line or is_telegram_date_header(line) or is_title_metadata_line(line):
                continue
            if line.startswith("名称："):
                return line[len("名称："):].strip()
            if line.startswith("#") and "名称" not in line:
                continue
            return cut_title_by_keywords(line, ("简介", "描述"))

    # 正文为空时回落到纯文本
    lines = text_content.split("\n")
    if not lines:
        return ""
    first_line = lines[0].strip()

    if first_line.startswith("#"):
        for line in lines:
            line = line.strip()
            if line.startswith("名称："):
                return line[len("名称："):].strip()

        if len(lines) > 1:
            second_line = lines[1].strip()
            if second_line.startswith("名称："):
                return second_line[len("名称："):].strip()
            if second_line and not second_line.startswith("#"):
                return cut_title_by_keywords(second_line, ("简介", "描述"))

    if first_line.startswith("名称："):
        return first_line[len("名称："):].strip()

    return cut_title_by_keywords(first_line, ("简介", "描述"))


def cut_title_by_keywords(title: str, keywords: Iterable[str]) -> str:
    """parser_util.go:586 `CutTitleByKeywords`。取最靠前的关键词前那一段。"""
    min_idx = -1
    for kw in keywords:
        idx = title.find(kw)
        if idx >= 0 and (min_idx == -1 or idx < min_idx):
            min_idx = idx
    if min_idx > 0:
        return title[:min_idx].strip()
    return title.strip()


# parser_util.go:748
_TELEGRAM_DATE_HEADER = re.compile(
    r"^📅?\s*\d{1,4}(?:年\d{1,2}月\d{1,2}日|[-/.]\d{1,2}[-/.]\d{1,2})$|^📅?\s*\d{1,2}月\d{1,2}日$"
)


def is_telegram_date_header(line: str) -> bool:
    return bool(_TELEGRAM_DATE_HEADER.match(line.strip()))


_TITLE_METADATA_PREFIXES = ("类型：", "分享：", "网盘：", "简介：", "描述：", "更多资源：")


def is_title_metadata_line(line: str) -> bool:
    """parser_util.go:754。"""
    line = line.strip()
    return any(line.startswith(p) for p in _TITLE_METADATA_PREFIXES)


# ---------------------------------------------------------------- 作品标题


def extract_work_titles_for_links(links: list[Link], message_text: str, default_title: str) -> list[Link]:
    """parser_util.go:765。≤4 条链接视为同一作品的不同网盘。"""
    if not links:
        return links

    if len(links) <= 4:
        for link in links:
            link.work_title = default_title
        return links

    lines = message_text.split("\n")
    if is_single_line_format(lines):
        return extract_work_titles_from_single_line_format(links, lines, default_title)
    return extract_work_titles_from_context(links, message_text, default_title)


def is_single_line_format(lines: list[str]) -> bool:
    """parser_util.go:791。"作品名丨网盘：链接" 超过 1/3 的行即视为单行格式。"""
    single_line_count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "丨" in line and "：" in line and ("http://" in line or "https://" in line):
            single_line_count += 1
    return single_line_count > len(lines) // 3


_NETDISK_NAMES = (
    "夸克网盘", "夸克云盘", "夸克",
    "百度网盘", "百度云盘", "百度云", "百度",
    "迅雷网盘", "迅雷云盘", "迅雷",
    "阿里云盘", "阿里网盘", "阿里云", "阿里",
    "天翼云盘", "天翼网盘", "天翼云", "天翼",
    "UC网盘", "UC云盘", "UC",
    "移动云盘", "移动云", "移动",
    "115网盘", "115云盘", "115",
    "123网盘", "123云盘", "123",
    "PikPak网盘", "PikPak",
    "网盘", "云盘",
)


def extract_work_titles_from_single_line_format(
    links: list[Link], lines: list[str], default_title: str
) -> list[Link]:
    """parser_util.go:810。"""
    url_to_work_title: dict[str, str] = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue

        work_title = ""
        link_url = ""

        if "丨" in line:
            parts = line.split("丨")
            if len(parts) >= 2:
                work_title = parts[0].strip()
                rest_part = parts[1]
                idx = rest_part.find("http")
                if idx >= 0:
                    link_url = extract_first_url(rest_part[idx:])
        elif "：" in line:
            colon_idx = line.find("：")
            if colon_idx > 0:
                before_colon = line[:colon_idx]
                after_colon = line[colon_idx + len("："):]
                work_title = extract_work_title_before_colon(before_colon)
                idx = after_colon.find("http")
                if idx >= 0:
                    link_url = extract_first_url(after_colon[idx:])

        if work_title and link_url:
            url_to_work_title[normalize_url_for_comparison(link_url)] = work_title

    for link in links:
        key = normalize_url_for_comparison(link.url)
        link.work_title = url_to_work_title.get(key, default_title)
    return links


def extract_first_url(text: str) -> str:
    """parser_util.go:875 `extractFirstURL`。

    注意三处都是 `idx > 0`，不是 `>= 0`：开头就是分隔符时不截断。
    """
    end_idx = len(text)
    idx = text.find(" ")
    if idx > 0 and idx < end_idx:
        end_idx = idx
    idx = text.find("\n")
    if idx > 0 and idx < end_idx:
        end_idx = idx
    idx = text.find("\r")
    if idx > 0 and idx < end_idx:
        end_idx = idx
    return text[:end_idx].strip()


def extract_work_title_before_colon(text: str) -> str:
    """parser_util.go:892。从右向左剥掉一次网盘名（只剥一次，和 Go 的 break 一致）。"""
    text = text.strip()
    for name in _NETDISK_NAMES:
        if text.endswith(name):
            return text[: len(text) - len(name)].strip()
    return text


def extract_work_titles_from_context(links: list[Link], message_text: str, default_title: str) -> list[Link]:
    """parser_util.go:922。匹配不上就全用默认标题 —— 上游就是这么写的。"""
    for link in links:
        link.work_title = default_title
    return links
