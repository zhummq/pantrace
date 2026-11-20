"""表格导入：把一张人工收集的表格变成库里的资源。

**这一层只管"行 → 资源"，不管"表格从哪儿来"。**
腾讯文档、CSV、Excel 导出、手工粘贴，进来都是 `list[list[str]]`，
所以取数方式（连接器 / 导出接口 / 手动下载）换了，这里一行都不用改。

三个必须处理的表格特征（文案提取那套解决不了）：

1. **提取码常常单独占一列**，值就是 `1234` 四个字符，前面没有"提取码："字样。
   `clean.password_for` 靠上下文关键词认码，对独立列是失效的，得先识别出
   "这一列全是 4 位码"，再按行补给它这一行的所有链接。
2. **标题常常只写在第一列一次，后面几列各挂一个盘的链接**。一行多链接共用标题，
   不是每条链接各配一个标题。
3. **表头不是固定的**。"片名/名称/资源/标题"都可能，也可能根本没有表头。

链接提取和提取码就近匹配全部复用 `search.clean`（那是照着 pansou 的
`regex_util.go` 逐行翻译来的，已覆盖九种网盘和十几种脏写法），这里只做
表格语义的补足。**不要在这里另起一套 URL 正则**——两套规则迟早会打架。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sdk import urls
from .search import clean

# 表头关键词。命中即认列，顺序无关。
_TITLE_WORDS = ("标题", "名称", "片名", "剧名", "资源名", "资源", "书名", "文件名", "title", "name")
_LINK_WORDS = ("链接", "地址", "网盘", "分享", "url", "link")
_CODE_WORDS = ("提取码", "访问码", "密码", "提取", "pwd", "passcode", "code")

_CODE_ONLY = re.compile(r"^[a-zA-Z0-9]{4}$")
_CODE_INLINE = re.compile(
    r"(?:提取码|访问码|提取|密码|pwd|code)\s*[:：]?\s*[a-zA-Z0-9]{4}", re.I
)
_URL_INLINE = re.compile(r"https?://\S+", re.I)
# 【更多资源关注xxx】【公众号：xxx】这类尾巴，进标题会污染检索
_NOISE = re.compile(
    r"[【\[（(][^】\]）)]{0,40}(?:更多|关注|公众号|加群|微信|资源站|合集站)[^】\]）)]{0,40}[】\]）)]"
)
_SEP = re.compile(r"[|｜,，;；\t]+")
# 带捕获组的码正则，供"严格就近"用。`_CODE_INLINE` 只用来剥标题，不要捕获。
_CODE_NEAR = re.compile(
    r"(?:提取码|访问码|提取|密码|pwd|code)\s*[:：]?\s*([a-zA-Z0-9]{4})(?![a-zA-Z0-9])", re.I
)

MAX_TITLE = 120
# 一列里超过这个比例是 4 位码，就当提取码列。定 0.6 而不是 0.8：
# 收集表常有人空着或写"无"。
_CODE_COL_RATIO = 0.6


@dataclass(slots=True)
class Candidate:
    """一条待入库的资源。"""

    title: str
    url: str
    platform: str
    pwd_id: str
    passcode: str = ""


@dataclass
class IntakeResult:
    rows: int = 0
    links: int = 0
    untitled: int = 0
    items: list[dict] = field(default_factory=list)
    # (行号, 原因)：宁可报告出来让人去补，也不要静默吞掉
    skipped: list[tuple[int, str]] = field(default_factory=list)

    @property
    def resources(self) -> int:
        return len(self.items)


def clean_title(text: str) -> str:
    """把一行里的杂讯剥掉，只留能当标题的部分。"""
    t = _URL_INLINE.sub(" ", str(text or ""))
    t = _CODE_INLINE.sub(" ", t)
    t = _NOISE.sub(" ", t)
    t = _SEP.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" -_·•:：*#")
    return t[:MAX_TITLE]


def _looks_like_title(text: str) -> bool:
    s = str(text or "").strip()
    if len(s) < 2 or _CODE_ONLY.match(s):
        return False
    if clean.extract_net_disk_links(s):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", s))


def _header_kind(cell: str) -> str:
    """判断表头单元格属于哪一类。空串表示不是表头。"""
    s = str(cell or "").strip().lower()
    if not s or len(s) > 12:
        return ""
    # 先判码：「提取码」里也含「提取」，别被链接词抢走
    if any(w in s for w in _CODE_WORDS):
        return "code"
    if any(w in s for w in _LINK_WORDS):
        return "link"
    if any(w in s for w in _TITLE_WORDS):
        return "title"
    return ""


def _detect_header(row: list[str]) -> dict[int, str] | None:
    """第一行像表头就返回 {列号: 类型}，否则 None。"""
    kinds = {i: _header_kind(c) for i, c in enumerate(row)}
    hits = {i: k for i, k in kinds.items() if k}
    # 至少两列认出来，或者至少认出一个标题列，才当表头。
    # 只认出 1 个链接列就下结论，容易把「https://xxx」这种数据行误判成表头。
    if len(hits) >= 2 or "title" in hits.values():
        return hits
    return None


def _code_column(rows: list[list[str]], header: dict[int, str] | None) -> int:
    """找提取码列。表头认出来就直接用，否则按"整列都是 4 位码"推断。"""
    if header:
        for idx, kind in header.items():
            if kind == "code":
                return idx
    cols = max((len(r) for r in rows), default=0)
    best, best_ratio = -1, 0.0
    for idx in range(cols):
        values = [str(r[idx]).strip() for r in rows if idx < len(r) and str(r[idx]).strip()]
        if len(values) < 3:
            continue
        ratio = sum(1 for v in values if _CODE_ONLY.match(v)) / len(values)
        if ratio > best_ratio:
            best, best_ratio = idx, ratio
    return best if best_ratio >= _CODE_COL_RATIO else -1


def _title_column(header: dict[int, str] | None) -> int:
    if not header:
        return -1
    for idx, kind in header.items():
        if kind == "title":
            return idx
    return -1


def _row_title(row: list[str], title_col: int, code_col: int) -> str:
    """标题：优先专用列，其次行内最长的像标题的片段。

    最后那条兜底很关键：**整行是一段分享文案**（收集表里最常见的填法——
    "三体 全集 https://pan.quark.cn/s/xxx 提取码 1234" 全塞进一个格子）时，
    没有任何单元格是"干净的标题"，只能把整行清洗一遍再取。漏了这条，
    这类表导进来会有大片占位标题。
    """
    if title_col >= 0 and title_col < len(row):
        got = clean_title(row[title_col])
        if got:
            return got
    best = ""
    for idx, cell in enumerate(row):
        if idx in (title_col, code_col):
            continue
        if not _looks_like_title(cell):
            continue
        got = clean_title(cell)
        # 长的更像标题；同等长度取靠前的
        if len(got) > len(best):
            best = got
    if best:
        return best
    whole = clean_title(" ".join(str(c or "") for c in row))
    # 清洗完只剩码或符号的，不如不给标题——让调用方用占位并计入 untitled
    return whole if _looks_like_title(whole) else ""


def assign_codes(text: str, links: list[str]) -> dict[str, str]:
    """把每个提取码分给**离它最近的那个链接**。

    不能反过来做（给每个链接找附近的码）。表格一行里链接常常挨着放：

        "三体 https://pan.quark.cn/s/q1 提取码 1111 https://pan.baidu.com/s/1q2"

    从百度出发去找码，它的左侧窗口里有"夸克的链接 + 1111"，会把 1111 抢走
    ——用户拿这个码去存，得到"提取码错误"，比不给码更糟。从码出发找最近的链接，
    1111 的左边是夸克链接（距离几十字符），右边是百度（更远），归属就唯一了。

    也不复用 `clean.password_for`：它在就近取不到时退回整段扫描，同样会跨链接。

    第三点也是踩过的坑：**一个码只能给一条链接**。等距时（"q1 提取码1111 百度"
    里 1111 到两边都是 5 个字符）按"每个链接各取最近码"会把它同时配给两条。
    所以按 (距离, 方向) 全局排序后贪心，且**码在链接右侧优先**——中文里
    "链接 xxx 提取码 1234" 是绝对主流写法。
    """
    spans: list[tuple[int, int, str]] = []
    for url in links:
        pos = text.find(url)
        if pos >= 0:
            spans.append((pos, pos + len(url), url))
    if not spans:
        return {}

    pairs: list[tuple[int, int, int, str, str]] = []
    for match in _CODE_NEAR.finditer(text):
        cpos, code = match.start(), match.group(1)
        for start, end, url in spans:
            if cpos >= end:
                pairs.append((cpos - end, 0, cpos, url, code))   # 码在链接后
            elif cpos < start:
                pairs.append((start - cpos, 1, cpos, url, code))  # 码在链接前
    pairs.sort()

    out: dict[str, str] = {}
    used: set[int] = set()
    for _, _, cpos, url, code in pairs:
        if cpos in used or url in out:
            continue
        out[url] = code
        used.add(cpos)
    return out


def parse_rows(
    rows: list[list[str]],
    *,
    has_header: bool | None = None,
    source_name: str = "doc",
) -> IntakeResult:
    """表格行 → 待入库条目。

    `has_header=None` 时自动判断（第一行认得出 ≥2 个表头词，或认出标题列）。
    """
    result = IntakeResult()
    if not rows:
        return result

    header: dict[int, str] | None = None
    body = rows
    if has_header is None:
        header = _detect_header([str(c or "") for c in rows[0]])
        if header:
            body = rows[1:]
    elif has_header:
        header = _detect_header([str(c or "") for c in rows[0]]) or {}
        body = rows[1:]

    title_col = _title_column(header)
    code_col = _code_column(rows if header is None else body, header)

    for offset, row in enumerate(body):
        line_no = offset + (2 if header is not None else 1)
        if not row or not any(str(c or "").strip() for c in row):
            continue
        result.rows += 1

        text = " ".join(str(c or "") for c in row)
        links = clean.extract_net_disk_links(text)
        if not links:
            result.skipped.append((line_no, "没有网盘链接"))
            continue

        title = _row_title([str(c or "") for c in row], title_col, code_col)
        row_code = ""
        if 0 <= code_col < len(row):
            raw = str(row[code_col]).strip()
            if _CODE_ONLY.match(raw):
                row_code = raw

        codes = assign_codes(text, links)

        for url in links:
            parsed = urls.parse(url)
            if not parsed.ok:
                # 详情页、站内页、认不出的短链：转存必失败，不入库
                result.skipped.append((line_no, f"识别不出分享 id：{url[:60]}"))
                continue

            # 严格就近（见 assign_codes），取不到再退回独立码列。
            # 两块兜底都在"不跨链接"的前提下工作，宁可给空也不给错码。
            code = codes.get(url) or row_code or parsed.passcode
            display = title or f"{parsed.platform} 分享 {parsed.pwd_id[:8]}"
            if not title:
                result.untitled += 1

            result.items.append({
                "shareUrl": parsed.url or url,
                "title": display,
                "platform": parsed.platform,
                "sharePwd": code,
                "fileCount": 0,
                "sizeBytes": 0,
                "sourceName": source_name,
                "sourceKind": "doc",
            })
            result.links += 1

    return result


def parse_text(text: str, *, source_name: str = "doc") -> IntakeResult:
    """纯文本导入：一行一条。没有列概念，标题从行内最长片段取。"""
    rows = [[line] for line in str(text or "").splitlines()]
    return parse_rows(rows, has_header=False, source_name=source_name)
