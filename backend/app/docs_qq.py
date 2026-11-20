"""腾讯文档公开表格 → 二维表。

这一层只管"文档 → `list[list[str]]`"，不碰入库、不碰检活。
进来的东西交给 `intake.parse_rows`，那才是"行 → 资源"的地方。

**五条必须知道的事实，都是逆向出来的，改代码前先看这一段。**

1. **数据不是 HTML。** 页面里一个网盘链接都没有，正则扫 HTML 必然是零条。
   真正的数据在 `GET /dop-api/opendoc` 的
   `clientVars.collab_client_vars.initialAttributedText.text[0]` 里：
   `workbook` 和 `block_datas[].related_sheet` 都是 **base64 + zlib 的 protobuf**。
   不带 `wb=1&nowb=0` 和 `needSheetState=1&sliceStates=1` 时，接口只回壳子。

2. **整块请求拿不到行号，单列请求可以——所以现在按列读。**
   请求 `col 0~63` 时，返回的单元格是按「类型」分组的：顶层 field 1 = 纯文字格，
   field 2 = 带超链接的格。两堆都没有行号，谁配谁只能靠数量关系去猜：数量相等
   就按下标配，不相等就把窗口二分到单行。这套猜法翻过两次车：
   一行多一个文字格（作者的广告行）、另一行多一个链接格（一格里写了俩），两堆
   总数刚好持平，判据被骗过去，整块从第一行起错位一行；「热门综艺」那张表里
   B1 的「找资源+v」就被当成了第二行标题，抢走 B3 的链接。
   2026-11-20 拿作者自己的 Excel 截图核对：表里「踢毽子视频教学」右边是
   `…2dQwA1`，我们配成了 `…DxRA1`（那是第 3 行「植物大战僵尸最新版.rar」的）。

   **解法是只请求一列**（`block_start_col = block_end_col = N`）。单列返回里没有
   第二堆可以混淆，顺序直接就是行顺序：实测 200000~201023 连续 1024 行，
   col0/col1/col2 格数严格相等、逐行配对零错位；把窗口挪到 128 起再读一遍，
   重叠部分逐格相同，说明中间的空行也照样占位、不会被跳过。
   现在 `read_block` 走的就是这条路，**行号是数出来的，不是配出来的**。
   代价是每块发 `ncols` 次请求而不是 1 次，换来请求数可预测、不用二分、
   不用锚点复核——总数反而比旧方案少，全量 38 万行约 19 分钟。

3. **窗口尾部的第 0 列会被裁掉。** 这是同一个坑的第二个形状，比错位更隐蔽：
   请求 0~1023 只回来 1013 个标题（1013~1023 行是空的），可单行窗口读第 1015 行
   明明有「12782-剑影江湖（60集）」；把窗口挪到 1000 起再读，同一段 1024 个全在。
   **只丢标题、不丢链接**，所以那一行变成"标题空、链接有"，入库后是个没有名字
   的资源，而表面上一切正常。
   规律是**头部永远完整、尾部不可信**。对策：相邻两块重叠 `COLUMN_OVERLAP` 行，
   让后一块的头部覆盖前一块的尾部；整表最后那十几行没有下一块可覆盖，逐行补读
   （内容本来就齐的行跳过，否则一张 1100 行的小表要白烧 48 次请求）。

4. **越过表尾的请求不会报错，它会把表头那一块给你。**
   这条最阴。`start_row >= max_row` 时接口照样返回完整数据，
   内容是**第 0 行开始的表头区**，跟越界位置毫无关系。2026-11 因此误判过一次：
   看到"从 max_row 往后连读几百块都有数据"，就断定 `max_row` 是过时的快照值、
   真实表有 200 万行，还写了探底逻辑去追——其实那 200 万行是同一段表头被
   重复读了几千遍，`max_row` 一直是对的。
   判别方法：取第 0 列前几个非空文本串成指纹（`_head_signature`），和表头比，
   一样就是越界了。`read_block` 里已经加了这层判断，别绕过它直接调 `_opendoc`。

5. **请求太密不报错，只回空 block。** 2026-11-20 全量重跑时踩到：84 秒内发了
   一千多次请求，之后服务端开始返回空 `block_datas`，几十秒后自己恢复。
   这比限流码危险得多——空块长得跟"这一段没数据"一模一样，上层当成功处理，
   还把 `last_row` 推到表尾，35 万行就这么静默没了；而下一轮增量只回看 512 行，
   永远补不回来。
   现在的处理：请求走全局节流（`_throttle`，1 次/秒，带随机抖动）。单列返回空
   是**合法结果**（整列没填），不能当限流信号；所以判据放在块级——`read_tab`
   整块读空但还没到表尾时，拿单行窗口复核一次，单行有数据就抛 `DocError` 让
   整次同步失败，宁可这一轮不更新，也不写错进度。

   压太狠还会升级成 WAF 人机验证：返回 501 + 一个跳
   `waf-captcha.cloud.tencent.com` 的 HTML 页，封十几分钟。这种情况**不要重试**，
   `_opendoc` 直接把"被拦了、等会儿再跑"抛到面板上。

**只支持"任何人可查看"的文档。** 需要登录的同样回空 block。但现在空块会和
"被限流"混在一起，所以不能一律返回空——`read_tab` 会在整块读空时拿单行复核，
单行有数据就说明是限流/权限问题，直接抛错让人看见。
"""

from __future__ import annotations

import base64
import logging
import random
import re
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("pantrace.docs")

OPENDOC = "https://docs.qq.com/dop-api/opendoc"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 官方前端一次请求的上限。给多了也只回一块，别浪费往返
BLOCK_ROWS = 256
# 只取一列时一次能读多少行。整块请求（col 0~63）受 BLOCK_ROWS 限制，但单列
# 不受：实测一次请求 1024 行，回来正好一个 block、1024 个格子。取 1024 是
# 实测过的整块边界，再往上没验过。
COLUMN_ROWS = 1024
# **窗口尾部的第 0 列会被接口裁掉，头部不会。** 实测表 2：请求 0~1023 只回来
# 1013 个标题（1013~1023 行是空的），可单行窗口读第 1015 行明明有标题
# 「12782-剑影江湖（60集）」；把窗口挪到 1000 起再读一遍，同一段 1024 个全在。
# 只丢标题、不丢链接，所以错得极其隐蔽——那一行的标题变成空字符串。
# 对策不是加大窗口，而是让相邻两块重叠：后一块的头部覆盖前一块的尾部。
COLUMN_OVERLAP = 16
# 一次读几列。表头 `max_col` 不能信——文档里每张表都报 26（模板默认值），
# 实际用到的只有 2~3 列。少读一列就丢数据，多读一列只是多花 1/3 的请求，
# 所以往多了取 3。
COLUMN_SCAN = 3
# 全局请求速率（次/秒）。上限不是带宽而是"会不会被判成爬虫"。
# 这台机器实测的边界：3.5 次/秒能一口气跑完 38 万行不掉块，但同一天再压一轮
# 就撞上 WAF 的 501 人机验证页，封十几分钟。抓取作业最怕的不是慢，是被封之后
# 那一轮静默失败，所以这里按"慢得多但绝不被拦"取值：1 次/秒起步，
# 38 万行那张表 = 378 块 × 3 列 ≈ 1134 次请求，约 19 分钟。日常增量只有
# 2~10 次请求，多花的那几秒看不出来。
REQ_PER_SEC = 1.0
# 在固定间隔上再抖 0~0.4 秒。严格等间隔的请求序列本身就是机器特征，
# 抖动之后平均仍是 1 次/秒，但看起来不像定时器。
REQ_JITTER = 0.4
# 空响应的重试节奏。第一轮先短等（大概率是瞬时抖动），后面成倍拉长。
EMPTY_RETRIES = 3
EMPTY_BACKOFF = (0.4, 1.2, 3.0)
TIMEOUT = 30.0

_PAD_ID = re.compile(r"docs\.qq\.com/(?:sheet|smartsheet)/([A-Za-z0-9]+)")
_URL_IN_TEXT = re.compile(r"^https?://[^\s]{6,400}$")


class DocError(RuntimeError):
    """文档读不到。reason 直接给人看，别让它读起来像栈。"""


# ---------------------------------------------------------------- protobuf


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise IndexError("varint 越界")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def _fields(buf: bytes):
    """按 wire format 切字段，产出 (字段号, 类型, 值)。

    只认得四种 wire type。遇到 group（3/4）直接停——腾讯文档这几张表里没用过，
    而猜错了会把后面全部字节当垃圾，比少解析几个字段危险得多。
    任何异常都当成"这层读完了"：protobuf 里有些嵌套是无 schema 的，
    硬报错会让整块数据丢光。
    """
    i = 0
    size = len(buf)
    while i < size:
        try:
            tag, i = _varint(buf, i)
        except (IndexError, ValueError):
            return
        fn, wt = tag >> 3, tag & 7
        if not fn:
            return
        try:
            if wt == 0:
                value, i = _varint(buf, i)
                yield fn, 0, value
            elif wt == 1:
                if i + 8 > size:
                    return
                yield fn, 1, buf[i:i + 8]
                i += 8
            elif wt == 2:
                length, i = _varint(buf, i)
                if length < 0 or i + length > size:
                    return
                yield fn, 2, buf[i:i + length]
                i += length
            elif wt == 5:
                if i + 4 > size:
                    return
                yield fn, 5, buf[i:i + 4]
                i += 4
            else:
                return
        except (IndexError, ValueError):
            return


def _inflate(blob: str | bytes) -> bytes:
    """base64 → zlib。腾讯文档两段数据都是这么包的。"""
    if isinstance(blob, str):
        raw = base64.b64decode(blob)
    else:
        raw = blob
    try:
        return zlib.decompress(raw)
    except zlib.error:
        # 有少数版本存的是 deflate raw，没有 zlib 头
        return zlib.decompress(raw, -zlib.MAX_WBITS)


def _unwrap_text(payload: bytes) -> str | None:
    """payload 是 `\\n<长度><文本>` 这种标准形态时，返回里面那段文本。

    **`i + ln == len(payload)` 这个严格判断不能放宽。** 腾讯文档把每个单元格
    和每个 URL 都写成 `0x0a + 变长长度 + 内容`，不对齐到末尾就说明这不是一层
    文本包装，而是别的结构；放宽了会把富文本属性块切开，读出一堆长度字节。
    """
    if not payload or payload[0] != 0x0A:
        return None
    try:
        length, start = _varint(payload, 1)
    except (IndexError, ValueError):
        return None
    if start + length != len(payload):
        return None
    try:
        return payload[start:].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _text_of(payload: bytes) -> str:
    """单元格 payload → 纯文本。

    直接 decode 会让标题永远多一个"长度字节"变成的怪字符，
    例如 `9植物大战僵尸合集`、`O10782-彩礼八万八…`。
    """
    text = _unwrap_text(payload)
    if text is not None:
        return text
    for fn, wt, val in _fields(payload):
        if fn == 1 and wt == 2:
            inner = _unwrap_text(val)
            if inner is not None:
                return inner
            try:
                return val.decode("utf-8")
            except UnicodeDecodeError:
                return ""
    return ""


def _strings(payload: bytes, depth: int = 0, out: list[str] | None = None) -> list[str]:
    """递归收集所有文本叶子。

    富文本单元格里 URL 埋在 `field3.field1`，同一个 URL 还会出现两次
    （一次显示文本、一次跳转目标）。这里不区分，上层按值去重就够。
    """
    if out is None:
        out = []
    if depth > 8:
        return out
    for _fn, wt, val in _fields(payload):
        if wt != 2:
            continue
        text = _unwrap_text(val)
        if text is not None:
            out.append(text)
            continue
        try:
            raw = val.decode("utf-8")
        except UnicodeDecodeError:
            raw = ""
        if raw and all(c.isprintable() or c in "\n\t" for c in raw):
            out.append(raw)
            continue
        _strings(val, depth + 1, out)
    return out


def urls_in_cell(payload: bytes) -> list[str]:
    """单元格里挂的分享链接，按出现顺序去重。"""
    seen: list[str] = []
    for text in _strings(payload):
        for token in text.split():
            if _URL_IN_TEXT.match(token) and token not in seen:
                seen.append(token)
    return seen


# ---------------------------------------------------------------- 结构


@dataclass(slots=True)
class Tab:
    tab_id: str
    title: str
    max_row: int = 0
    max_col: int = 0


@dataclass(slots=True)
class DocMeta:
    pad_id: str
    title: str = ""
    tabs: list[Tab] = field(default_factory=list)
    rev: str = ""

    def tab(self, tab_id: str = "") -> Tab | None:
        if not tab_id:
            return self.tabs[0] if self.tabs else None
        for item in self.tabs:
            if item.tab_id == tab_id:
                return item
        return None


def pad_id_of(url: str) -> tuple[str, str]:
    """文档链接 → (pad_id, tab_id)。tab_id 为空表示用第一个工作表。"""
    text = str(url or "").strip()
    match = _PAD_ID.search(text)
    if not match:
        raise DocError("这不是腾讯文档的表格链接（要 docs.qq.com/sheet/... 那种）")
    try:
        query = parse_qs(urlparse(text).query)
    except ValueError:
        query = {}
    tab = (query.get("tab") or [""])[0]
    return match.group(1), tab


def parse_workbook(blob: bytes) -> list[Tab]:
    """workbook protobuf → 工作表清单。

    路径：`field1 → field5(repeated) → field2 → (3.1=tab_id, 5.1=标题, 7.1=行数)`。
    行数读出来是为了增量：表长没变就没必要重拉。
    """
    tabs: list[Tab] = []
    root = [val for fn, wt, val in _fields(blob) if fn == 1 and wt == 2]
    for doc in root:
        for fn, wt, sheet in _fields(doc):
            if fn != 5 or wt != 2:
                continue
            body = None
            for fn2, wt2, val2 in _fields(sheet):
                if fn2 == 2 and wt2 == 2:
                    body = val2
                    break
            if body is None:
                continue
            tab_id = title = ""
            max_row = max_col = 0
            for fn3, wt3, val3 in _fields(body):
                if wt3 != 2:
                    continue
                head = next(
                    (v for f, w, v in _fields(val3) if f == 1 and w == 2), b""
                )
                if fn3 == 3:
                    tab_id = head.decode("utf-8", "ignore")
                elif fn3 == 5:
                    title = head.decode("utf-8", "ignore")
                elif fn3 == 7:
                    num = next((v for f, w, v in _fields(val3) if f == 1 and w == 0), 0)
                    max_row = int(num or 0)
                elif fn3 == 8:
                    num = next((v for f, w, v in _fields(val3) if f == 1 and w == 0), 0)
                    max_col = int(num or 0)
            if tab_id:
                tabs.append(Tab(tab_id, title, max_row, max_col))
    return tabs


# block 的外层是「工作簿 → 工作表 → 数据区 → 行块」四层壳，
# 真正的列数据在最后一层的直接子字段里。写死路径是为了可读：
# 匹配不上时下面有兜底，不会因为腾讯改一层嵌套就整块读不出来。
_BLOCK_PATH = (1, 5, 19, 5)


def _walk_path(node: bytes, path: tuple[int, ...]) -> list[bytes]:
    """沿字段路径下钻，返回**所有**命中（同名标签会重复出现）。

    不能只取第一个：`field5` 在每个工作表里出现两次，一次是元信息（86B），
    一次才是数据区（74KB）。取第一个的结果是永远读到空表。
    """
    nodes = [node]
    for want in path:
        nxt: list[bytes] = []
        for item in nodes:
            for fn, wt, val in _fields(item):
                if fn == want and wt == 2:
                    nxt.append(val)
        nodes = nxt
        if not nodes:
            return []
    return nodes


def _column_payloads(blob: bytes) -> list[tuple[int, bytes]]:
    """剥到列数据层，返回 [(列号, 单元格payload)]。"""
    best: list[tuple[int, bytes]] = []
    for node in _walk_path(blob, _BLOCK_PATH):
        items = [(fn, val) for fn, wt, val in _fields(node) if wt == 2 and fn >= 1]
        # 列数据层的特征：字段号就是列号，种类少、条目多
        if items and max(fn for fn, _ in items) <= 32 and len(items) > len(best):
            best = items
    if best:
        return best

    # 兜底：结构变了就逐层广搜，挑"字段种类最少、条目最多"的那层当列数据
    frontier = _walk_path(blob, _BLOCK_PATH)
    for _ in range(6):
        deeper: list[bytes] = []
        for item in frontier:
            children = [(fn, val) for fn, wt, val in _fields(item) if wt == 2]
            if children and max(fn for fn, _ in children) <= 32 and len(children) > len(best):
                best = children
            deeper.extend(val for _, val in children)
        frontier = deeper[:400]
        if not frontier:
            break
    return best


def parse_block(blob: bytes) -> list[list[str]]:
    """一个 block protobuf → 二维表（行优先，单元格已转成文本）。

    列式还原：列号 = 顶层字段号。列长不一致时按最长那列补空串——
    空单元格本来就不占位，短列缺的尾部就是空行。
    """
    items = _column_payloads(blob)
    if not items:
        return []

    columns: dict[int, list[str]] = {}
    for fn, val in items:
        # 单元格可能是"标题 + 超链接"两层：文本在 field1，链接藏在富文本里。
        # 两样都要——只取文本会丢链接，只取链接会丢标题
        title = _text_of(val)
        extra = " ".join(urls_in_cell(val))
        cell = f"{title} {extra}".strip() if extra and extra not in title else title
        columns.setdefault(fn, []).append(cell)

    if not columns:
        return []

    width = max(columns)
    height = max(len(col) for col in columns.values())
    rows: list[list[str]] = []
    for r in range(height):
        row = []
        for c in range(1, width + 1):
            col = columns.get(c) or []
            row.append(col[r] if r < len(col) else "")
        if any(cell.strip() for cell in row):
            rows.append(row)
    return rows


# ---------------------------------------------------------------- 取数


_local = threading.local()

# 节流状态。`_next_slot` 是"下一个可以发请求的时刻"（monotonic 秒），
# 多线程一起排队领号——在锁里 sleep 看着粗暴，但它保证了严格速率上限，
# 而且 sleep 期间请求本身已经在锁外发出去了，并发度并没有被压成串行。
_rate_lock = threading.Lock()
_next_slot = 0.0
_req_total = 0


def _throttle() -> None:
    """领一个请求名额，必要时等到轮到自己。"""
    global _next_slot, _req_total
    with _rate_lock:
        now = time.monotonic()
        if _next_slot > now:
            time.sleep(_next_slot - now)
            now = _next_slot
        _next_slot = now + 1.0 / REQ_PER_SEC + random.uniform(0, REQ_JITTER)
        _req_total += 1


def request_count() -> int:
    """累计发出的请求数。上层拿它算"这一轮花了多少次"写进日志。"""
    with _rate_lock:
        return _req_total


def _session() -> requests.Session:
    session = getattr(_local, "s", None)
    if session is not None:
        return session
    session = requests.Session()
    # 和 sdk/http.py 同一个理由：本机开着代理时 requests 会走系统代理，
    # 表现成"读超时"，排查半天看不出是代理
    session.trust_env = False
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    _local.s = session
    return session


def _opendoc(
    pad_id: str,
    tab_id: str = "",
    start_row: int = 0,
    end_row: int = 255,
    col: int = -1,
) -> dict:
    """拉一块数据。`col >= 0` 时只拉这一列（这是唯一能拿到行号的请求方式）。"""
    params = {
        "id": pad_id,
        "tab": tab_id,
        "normal": "1",
        "outformat": "1",
        "noEscape": "1",
        # 这三个少一个就只回壳子，拿不到单元格
        "wb": "1",
        "nowb": "0",
        "needSheetState": "1",
        "sliceStates": "1",
        "enableSmartsheetSplit": "1",
        "startrow": "0",
        "endrow": "60",
        "block_start_row": str(start_row),
        "block_end_row": str(end_row),
        "block_start_col": "0" if col < 0 else str(col),
        "block_end_col": "63" if col < 0 else str(col),
    }
    try:
        _throttle()
        resp = _session().get(
            OPENDOC,
            params=params,
            headers={"User-Agent": UA, "Referer": f"https://docs.qq.com/sheet/{pad_id}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DocError(f"连不上腾讯文档：{exc.__class__.__name__}") from exc
    if resp.status_code != 200:
        # 501 不是"没实现"，是腾讯 WAF 的人机验证页
        # （跳到 waf-captcha.cloud.tencent.com）。请求太密会被拦一段时间，
        # 期间每个请求都失败——这时候重试只会把封禁时间拖得更长，所以直接
        # 抛出去让整轮同步失败、进度停在原地，等人过一阵再跑。
        if resp.status_code in (403, 429, 501, 502, 503):
            raise DocError(
                f"被腾讯文档拦了（HTTP {resp.status_code}，疑似请求太密触发人机验证），"
                "等十几分钟再跑，别连着点"
            )
        raise DocError(f"腾讯文档返回 {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise DocError("腾讯文档返回的不是 JSON（链接可能被重定向到登录页）") from exc


def _block_offsets(payload: dict) -> list[tuple[int, bytes]]:
    try:
        holder = payload["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"][0]
    except (KeyError, IndexError, TypeError):
        return []
    blocks = holder.get("block_datas") or []
    out: list[tuple[int, bytes]] = []
    for block in blocks:
        raw = block.get("related_sheet") or ""
        if not raw:
            continue
        try:
            out.append((int(block.get("end_row_index") or 0), _inflate(raw)))
        except (zlib.error, ValueError):
            continue
    return out


def fetch_meta(url: str) -> DocMeta:
    """只读元信息：标题、工作表清单、每个表多少行。增量判断用它。"""
    pad_id, tab_id = pad_id_of(url)
    payload = _opendoc(pad_id, tab_id, 0, 0)
    meta = DocMeta(pad_id=pad_id)
    try:
        body = payload["bodyData"]
        meta.title = str(body.get("initialTitle") or "")
    except (KeyError, TypeError):
        pass
    try:
        holder = payload["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"][0]
        meta.rev = str(holder.get("rev") or "")
        meta.tabs = parse_workbook(_inflate(holder["workbook"]))
    except (KeyError, IndexError, TypeError, zlib.error) as exc:
        raise DocError("这个文档读不出工作表结构，八成是没开「任何人可查看」") from exc
    return meta


_header_cache: dict[tuple[str, str], str] = {}


def _head_signature(cells: list[tuple[str, list[str]]]) -> str:
    """一组格子的指纹：前几个非空文本用分隔符串起来。

    比"只比首格"稳：作者在第一行前面留了空列、或者首格是空的合并单元格时，
    单独比首格会得到空串，越界保护就等于没写。
    """
    return "\x1f".join(t.strip() for t, _u in cells if t.strip())[:400]


def _header_fingerprint(pad_id: str, tab_id: str) -> str:
    """表头这一列的指纹，用来识破"越界返回表头"这种假数据。

    走和第 0 列其他块完全相同的请求方式（单列、256 行），这样指纹和
    `read_block` 里的 `cols[0]` 是同一个口径，比得才有意义。

    只读一次、按 (pad, tab) 缓存。表头被作者改了也不影响判断：那时越界返回的
    是新表头，指纹跟着变，仍然对得上。
    """
    key = (pad_id, tab_id)
    fp = _header_cache.get(key, "")
    if fp:
        return fp
    fp = _head_signature(_column_cells(pad_id, tab_id, 0, 0, BLOCK_ROWS - 1))
    if fp:
        _header_cache[key] = fp
    return fp


def _window_cells(
    pad_id: str, tab_id: str, start_row: int, end_row: int, *, attempts: int = EMPTY_RETRIES
) -> list[tuple[str, list[str]]]:
    """一次整块请求（col 0~63），按返回顺序取出单元格：(文本, 链接列表)。

    **现在只用它做单行复核**——`read_tab` 拿它判断"整块读空到底是没数据还是
    被限流"。正式读数据走 `_column_cells`：整块返回是按类型分组的、拿不到
    行号，见模块头第 2 条。

    **空响应不等于"这段没数据"。** 请求过密时接口照样返回 200、结构也完整，
    只是 `block_datas` 是空的，几十秒后自己恢复（模块头第 5 条）。所以整块
    空一次就重试，三次都空才抛错。

    单行窗口是例外：那一行本来就可以是空行（表里空白行很常见），空就是空。
    """
    single = end_row - start_row <= 0
    tries = 1 if single else max(1, attempts)
    for i in range(tries):
        payload = _opendoc(pad_id, tab_id, start_row, end_row)
        out: list[tuple[str, list[str]]] = []
        for _end_row, blob in _block_offsets(payload):
            for _fn, val in _column_payloads(blob):
                out.append((_text_of(val), urls_in_cell(val)))
            break                     # 一个请求只带一块
        if out:
            return out
        if i + 1 < tries:
            time.sleep(EMPTY_BACKOFF[min(i, len(EMPTY_BACKOFF) - 1)])
    if single:
        return []
    raise DocError(
        f"腾讯文档第 {start_row}~{end_row} 行连续 {tries} 次返回空块，"
        "多半是请求太密被限流了，稍后重试"
    )


def _column_cells(
    pad_id: str, tab_id: str, col: int, start_row: int, end_row: int
) -> list[tuple[str, list[str]]]:
    """只请求一列，返回按行排好的格子：第 i 个就是第 `start_row + i` 行的这一列。

    **这是整个解析的地基。** 整块请求（col 0~63）返回的单元格是按**类型**分组
    的：纯文字一堆、带超链接一堆，两堆都没有行号，谁配谁只能靠数量关系去猜
    （见模块头第 2 条）。而单列请求里没有第二堆可以混淆，返回顺序直接就是
    行顺序——2026-11-20 实测：200000~201023 连续 1024 行，col0/col1/col2
    的格数严格相等，逐行配对零错位；把窗口挪到 128 起再读一遍，重叠部分
    `big[128:] == small` 完全一致，说明中间的空行也照样占位、不会被跳过。

    同一张表里的「踢毽子视频教学」用它读出来是第 1 行，右边是 `…2dQwA1`，
    和作者 Excel 里看到的一致；旧的对齐逻辑给的是 `…DxRA1`（第 3 行的）。

    **格数可能少于窗口行数，缺的是这一列的列尾连续空白。** 表 2 的 col0 在
    0~511 窗口只回 501 个，缺的是 501~511 行（那几行的第 0 列没填）。裁的
    只在尾部，头部一定从 `start_row` 开始，所以下标 i 永远是第 `start_row+i` 行，
    短了的部分上层补空即可。

    不在这里做空重试：单列返回空是合法结果（整列没填），不是限流信号。
    """
    payload = _opendoc(pad_id, tab_id, start_row, end_row, col=col)
    out: list[tuple[str, list[str]]] = []
    for _end_row, blob in _block_offsets(payload):
        for _fn, val in _column_payloads(blob):
            out.append((_text_of(val), urls_in_cell(val)))
    # 正常只有一个 block。真回来多个就按顺序拼，但别超过窗口行数——
    # 越界的请求会把表头那块塞进来，多出来的格子只会污染对齐。
    limit = end_row - start_row + 1
    return out[:limit]


def _cell_value(cell: tuple[str, list[str]]) -> str:
    """一个格子的值：有文字取文字，纯链接格取链接。"""
    text, urls = cell
    return text if text.strip() else " ".join(urls)


def read_block(
    pad_id: str,
    tab_id: str,
    start_row: int,
    end_row: int | None = None,
    *,
    ncols: int = COLUMN_SCAN,
) -> list[list[str]]:
    """拉一块（最多 `COLUMN_ROWS` 行）。空列表表示这块没数据。

    `end_row` 给最后一块用（夹到表长），不给就是整块。

    **按列读，不按整块读。** 每一列单独发一次请求，拿回来就是该列的逐行序列
    （`_column_cells` 里有实测证据），第 i 个格子就是第 `start_row+i` 行。
    这样行号是数出来的，不是配出来的——不存在"标题配上隔壁行链接"这种事。

    代价是每块要发 `ncols` 次请求而不是 1 次。换来的是：38 万行那张表不用
    再为错位做二分，也不用表头锚点复核，请求总数反而比之前那套（整块 +
    锚点 + 偶尔二分数十次）更少、更可预测。

    **越界不报错，它把表头那块还给你。** `start_row >= max_row` 时接口照样
    返回表头内容（见模块头第 4 条）。所以拿第 0 列的首格和表头指纹比一下，
    撞上就当"没数据"。
    """
    start_row = max(0, int(start_row))
    end = start_row + COLUMN_ROWS - 1 if end_row is None else int(end_row)
    end = min(end, start_row + COLUMN_ROWS - 1)
    if end < start_row:
        return []

    cols = [_column_cells(pad_id, tab_id, c, start_row, end) for c in range(max(1, ncols))]
    if not any(cols):
        return []
    fp = _header_fingerprint(pad_id, tab_id)
    head = _head_signature(cols[0])
    if start_row > 0 and fp and head and head == fp:
        return []

    # 行数取最长的那一列：列尾连续空白会被裁掉，裁出来的空洞都是空值，
    # 补空即可。不能取最短的，否则每个块都会从尾部丢掉几行。
    n = min(max(len(c) for c in cols), end - start_row + 1)
    rows: list[list[str]] = []
    for i in range(n):
        rows.append([_cell_value(c[i]) if i < len(c) else "" for c in cols])
    return rows


def read_tab(
    url: str,
    *,
    tab_id: str = "",
    start_row: int = 0,
    end_row: int | None = None,
    workers: int = 3,
    ncols: int = COLUMN_SCAN,
    on_progress=None,
) -> tuple[list[list[str]], DocMeta]:
    """把一个工作表读成二维表。

    `start_row` 给增量用：只拉这之后的行。`end_row` 默认按元信息里的 max_row。
    `ncols` 是读几列（见 `COLUMN_SCAN`）。

    **瓶颈是节流，不是并发。** `_throttle` 把总速率钉在 1 次/秒，`workers`
    再大也只是让几个线程排队领号，不会真的同时压出去。默认 3 是够用的：
    同一时刻最多 3 个请求在飞，剩下的排队。
    """
    meta = fetch_meta(url)
    tab = meta.tab(tab_id)
    if tab is None:
        raise DocError(f"文档里没有这张工作表：{tab_id or '(默认)'}")
    if not tab.max_row:
        raise DocError("工作表是空的")

    # `end_row` 不能超过表长：传大了腾讯文档照样回空块，白花几百次请求，
    # 而且进度会被写成 5013 这种比表长还大的数（已踩过）
    last = min(end_row, tab.max_row) if end_row is not None else tab.max_row
    start = max(0, int(start_row))
    if start >= last:
        return [], meta

    # 步长比窗口短 COLUMN_OVERLAP 行：后一块的头部会盖住前一块被裁掉的尾部
    step = max(1, COLUMN_ROWS - COLUMN_OVERLAP)
    starts = list(range(start, last, step))
    rows: list[list[str]] = []
    req_before = request_count()
    t0 = time.monotonic()

    def _one(offset: int) -> tuple[int, list[list[str]]]:
        # 最后一块要夹到表长：`max_row` 不是块长的整数倍，不夹的话窗口会越过
        # 表尾，而越界部分返回的是**表头**内容，会混进这一块。
        end = min(offset + COLUMN_ROWS - 1, last - 1)
        chunk = read_block(meta.pad_id, tab.tab_id, offset, end, ncols=ncols)
        if chunk or end >= last - 1:
            return offset, chunk
        # 整块读空但还没到表尾：正常表里不会出现。拿单行复核一次——单行有格子
        # 就说明是限流把整块吞了。这时候必须抛错让整次同步失败，因为上层一旦
        # 认为"读完了"，就会把 last_row 推到表尾，这一块永远补不回来。
        if _window_cells(meta.pad_id, tab.tab_id, offset, offset):
            raise DocError(
                f"第 {offset} 行整块读空、单行却有数据，疑似被限流；这一轮不写进度"
            )
        return offset, []

    def _merge(offset: int, chunk: list[list[str]]) -> None:
        """按绝对行号落位，后到的覆盖先到的（重叠区就是这么修的）。"""
        for i, row in enumerate(chunk):
            idx = offset + i - start
            if idx < 0:
                continue
            while len(rows) < idx:      # 中间整段读空时不能把后面的行往前挤
                rows.append([""] * ncols)
            if idx < len(rows):
                rows[idx] = row
            else:
                rows.append(row)

    if workers <= 1:
        for offset in starts:
            _merge(*_one(offset))
            if on_progress:
                on_progress(offset, last)
    else:
        # `pool.map` 保序返回，但落位本身按行号走，不依赖顺序
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for idx, (offset, chunk) in enumerate(pool.map(_one, starts), 1):
                _merge(offset, chunk)
                if on_progress and idx % 20 == 0:
                    on_progress(starts[idx - 1], last)

    # 整表最后 COLUMN_OVERLAP 行没有下一块可以覆盖，只能一行一行补。
    # 单行窗口不受尾部裁剪影响（实测第 1015 行的标题在单行窗里是完整的）。
    # 内容本来就齐的行不补——表的最后十几行通常都是正常资源，无条件补读会让
    # 一张 1100 行的小表烧掉 48 次请求（实测）。
    need = min(2, ncols)
    for r in range(max(start, last - COLUMN_OVERLAP), last):
        idx = r - start
        if idx < len(rows) and sum(1 for v in rows[idx] if v.strip()) >= need:
            continue
        _merge(r, read_block(meta.pad_id, tab.tab_id, r, r, ncols=ncols))

    rows = rows[: last - start]
    while len(rows) < last - start:
        rows.append([""] * ncols)

    if on_progress:
        on_progress(last, last)
    log.info(
        "读表 %s[%s~%s]：%s 块 × %s 列 / %s 次请求 / %s 行（%.1f 秒）",
        tab.tab_id, start, last - 1, len(starts), ncols,
        request_count() - req_before, len(rows), time.monotonic() - t0,
    )
    return rows, meta
