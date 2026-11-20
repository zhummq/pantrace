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
# 「没有提取码」的常见写法。这些不是码，但**也不该算进分母**：
# 真实收集表里它们占比很高（"无"能占一半以上），算进去整列比例就跌破阈值，
# 结果是链接和标题都对、只有码整列丢了，最难被发现的一种残缺。
_NO_CODE = frozenset({
    "无", "没有", "不需", "不需要", "空", "暂无", "-", "—", "－", "/", "\\",
    "n/a", "na", "none", "no", "null",
})


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
    # 判据是「至少两列认出来，且其中必须有链接列或码列」。
    #
    # 早先写的是「≥2 列 **或者** 有标题列」，后者太松：首行常是
    # 「2026年11月资源合集」这种大标题，命中「资源」就被当表头整行吃掉，
    # 后面的真表头反而成了数据行。表头的本质特征是"有一列放链接"，
    # 光有"标题"两个字不算。
    if len(hits) >= 2 and any(k in ("link", "code") for k in hits.values()):
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
        values = [v for v in values if v.lower() not in _NO_CODE]
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


def check_link_column(rows: list[list[str]], link_col: int) -> str:
    """人工指定的链接列指对了吗？返回警告文案，空串表示没问题。

    指错的代价不是报错，是整表 0 条入库或者标题全变成备注，而定时任务
    会安静地跑好几天。所以登记时就要拦。

    两个必须算失败、但很容易被"放过"的形态：
    - **这一列全是空的**。指到不存在的列（表只有 2 列却指了第 3 列）就是这样，
      空集合会让"按比例算"的校验整条绕过去。
    - 有内容但不到一半是链接。指到了备注列、分类列。
    """
    if link_col < 1:
        return ""
    hit = total = 0
    for row in rows:
        if len(row) < link_col:
            continue
        cell = str(row[link_col - 1]).strip()
        if not cell:
            continue
        total += 1
        if clean.extract_net_disk_links(cell):
            hit += 1
    if total == 0:
        return f"第 {link_col} 列在前 40 行里全是空的，这一列不存在或不是链接列"
    if hit / total < 0.5:
        return f"第 {link_col} 列里只有 {hit}/{total} 行有网盘链接，列号八成不对"
    return ""


def iter_row_batches(
    rows: list[list[str]], size: int = 5000
) -> list[tuple[bool, list[list[str]]]]:
    """把整表切成若干批，只让**第一批**去猜表头。

    为什么不能每批各自猜：`parse_rows` 的自动表头检测是按"这一批的第一行"
    判断的。38 万行的表切 76 批，只要某一批的首行标题里恰好有「资源」「链接」
    这类词（比如一条资源就叫《资源链接大全》），它就被当成表头整行吃掉。
    错误量级是"每批最多吃一行"，散在 76 个批次里，对账时根本看不出来。

    返回 `[(是否第一批, 该批行), ...]`，调用方对非首批传 `has_header=False`。
    """
    if not rows:
        return []
    size = max(1, size)
    return [
        (start == 0, rows[start:start + size])
        for start in range(0, len(rows), size)
    ]


def probe_columns(rows: list[list[str]], sample: int = 3) -> list[dict]:
    """猜每列是干什么的，附样本值。给人看的，不是给解析用的。

    自动识别永远有猜错的时候，而且猜错的表现是"数据看着有、就是搜不到"。
    与其让人盲填列号，不如先把猜测和证据摆出来：看见第 2 列的样本是
    `https://pan.quark.cn/...`，就知道该填 2；看见样本是「无」，就知道
    那列是码列。
    """
    if not rows:
        return []
    header = _detect_header([str(c or "") for c in rows[0]])
    body = rows[1:] if header else rows
    if not body:
        return []

    title_col = _title_column(header)
    code_col = _code_column(body, header)
    cols = max((len(r) for r in body), default=0)

    # 没有表头时也要猜标题列。收集表里"没有表头"比"表头写得不标准"更常见
    # （很多作者直接从第一行开始填数据），探测报告里全标"其他"等于没探测。
    # 判据：那一列有文字、不是链接、不是纯 4 位码，占比过半。
    if title_col < 0:
        best_col, best_ratio = -1, 0.0
        for idx in range(cols):
            values = [str(r[idx]).strip() for r in body if idx < len(r) and str(r[idx]).strip()]
            if len(values) < 2:
                continue
            ratio = sum(1 for v in values if _looks_like_title(v)) / len(values)
            if ratio > best_ratio:
                best_col, best_ratio = idx, ratio
        if best_ratio >= 0.5:
            title_col = best_col

    report: list[dict] = []
    for idx in range(cols):
        values = [str(r[idx]).strip() for r in body if idx < len(r) and str(r[idx]).strip()]
        if not values:
            continue
        link_hits = sum(1 for v in values if clean.extract_net_disk_links(v))
        code_hits = sum(1 for v in values if _CODE_ONLY.match(v))
        if link_hits and link_hits >= len(values) * 0.5:
            kind = "link"
        elif code_col == idx:
            kind = "code"
        elif title_col == idx:
            kind = "title"
        elif link_hits:
            kind = "mixed"          # 链接和别的东西混在一格
        else:
            kind = "other"
        report.append({
            "col": idx + 1,             # 对外一律 1 起，跟人看表格的方式一致
            "kind": kind,
            "filled": len(values),
            "samples": values[:sample],
        })
    return report


def parse_rows(
    rows: list[list[str]],
    *,
    has_header: bool | None = None,
    source_name: str = "doc",
    title_col: int = 0,
    link_col: int = 0,
    code_col: int = 0,
) -> IntakeResult:
    """表格行 → 待入库条目。

    `has_header=None` 时自动判断（第一行认得出 ≥2 个表头词，或认出标题列）。

    `title_col` / `link_col` / `code_col` 是**人工指定**，一律 1 起（跟表格里
    看到的列号一致），0 表示"没指定，自己猜"。自动识别负责九成的表，剩下
    一成结构不常规的（标题在第 3 列、链接在第 5 列、中间夹着分类和备注），
    靠这三个参数一劳永逸——配置存在 `doc_sources` 上，每天定时任务照用。

    指定了就不许猜：宁可报错让人改，也不要"猜一半"——半猜的结果是最难查的
    那种错，链接对但标题错位，搜得到却点开是别的东西。
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

    # 对外 1 起，内部 1 起统一转 0 起下标；0 是哨兵值表示没指定
    t_col = title_col - 1 if title_col > 0 else _title_column(header)
    c_col = code_col - 1 if code_col > 0 else _code_column(rows if header is None else body, header)
    l_col = link_col - 1 if link_col > 0 else -1

    for offset, row in enumerate(body):
        line_no = offset + (2 if header is not None else 1)
        if not row or not any(str(c or "").strip() for c in row):
            continue
        result.rows += 1

        text = " ".join(str(c or "") for c in row)
        if l_col >= 0:
            cell = str(row[l_col]) if l_col < len(row) else ""
            links = clean.extract_net_disk_links(cell)
            if not links:
                # 指定了列却在这列里找不到链接。单个空行是表里本来就有空单元格，
                # 大面积出现才说明作者插了一列、列号错位了——由调用方按比例判断。
                result.skipped.append((line_no, f"第 {l_col + 1} 列没有网盘链接"))
                continue
        else:
            links = clean.extract_net_disk_links(text)
        if not links:
            result.skipped.append((line_no, "没有网盘链接"))
            continue

        title = _row_title([str(c or "") for c in row], t_col, c_col)
        row_code = ""
        if 0 <= c_col < len(row):
            raw = str(row[c_col]).strip()
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
