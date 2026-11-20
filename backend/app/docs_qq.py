"""腾讯文档公开表格 → 二维表。

这一层只管"文档 → `list[list[str]]`"，不碰入库、不碰检活。
进来的东西交给 `intake.parse_rows`，那才是"行 → 资源"的地方。

**三个必须知道的事实，都是逆向出来的，改代码前先看这一段。**

1. **数据不是 HTML。** 页面里一个网盘链接都没有，正则扫 HTML 必然是零条。
   真正的数据在 `GET /dop-api/opendoc` 的
   `clientVars.collab_client_vars.initialAttributedText.text[0]` 里：
   `workbook` 和 `block_datas[].related_sheet` 都是 **base64 + zlib 的 protobuf**。
   不带 `wb=1&nowb=0` 和 `needSheetState=1&sliceStates=1` 时，接口只回壳子。

2. **单元格是按「类型」分组的，不是按列。** 顶层 field 1 = 纯文字格，field 2 =
   带超链接的格。之所以看起来"像列"，是因为这份表的标题都是纯文字、链接都是超链接，
   两堆刚好一一对应。**一旦某行多一个文字格（作者的广告行）或少一格（空标题），
   两堆长度就不再相等，从那个位置往后整体错位**——标题会配到隔壁行的链接上，
   而表面上一切正常，搜得到、点得开，只是内容对不上。
   实测：抽查 40 块，6 块有这种错位。
   所以对齐不能按下标硬配，必须靠"文字格数 = 链接格数"这个条件判定（整行空白
   在两边都不占格子，所以这个条件比"等于行数"更宽松、也更对）；不成立就把窗口
   二分继续缩，缩到一行时格子的顺序就是列的先后（`_align_rows`）。

   这条是 2026-11-20 用作者自己的 Excel 截图核对出来的：表里「踢毽子视频教学」右边
   是 `…dQwA1`，而我们配成了 `…DxRA1`（错两行）。之前那句"field N 就是第 N-1 列"
   是错的，别按它改代码。

3. **一次只给 256 行。** 靠 `block_start_row` / `block_end_row` 翻页，
   38 万行的表要 1400+ 次请求。所以增量只能靠 `max_row` 变化判断，
   每次都全量重拉等于每天给人家文档刷 1400 次。

4. **越过表尾的请求不会报错，它会把表头那一块给你。**
   这条最阴。`start_row >= max_row` 时接口照样返回 257 行完整数据，
   内容是**第 0 行开始的表头区**，跟越界位置毫无关系。2026-11 因此误判过一次：
   看到"从 max_row 往后连读几百块都有数据"，就断定 `max_row` 是过时的快照值、
   真实表有 200 万行，还写了探底逻辑去追——其实那 200 万行是同一段表头被
   重复读了几千遍，`max_row` 一直是对的。
   判别方法：把返回的首行和 `read_block(..., 0)` 的首行比一下，一样就是越界了。
   `read_block` 里已经加了这层判断，别绕过它直接调 `_opendoc`。

5. **请求太密不报错，只回空 block。** 2026-11-20 全量重跑时踩到：84 秒内发了
   一千多次请求，之后服务端开始返回空 `block_datas`，几十秒后自己恢复。
   这比限流码危险得多——空块长得跟"这一段没数据"一模一样，上层当成功处理，
   还把 `last_row` 推到表尾，35 万行就这么静默没了；而下一轮增量只回看 512 行，
   永远补不回来。
   现在的处理：请求走全局节流（`_throttle`，1 次/秒，带随机抖动）；
   空响应重试三次；仍然空就抛 `DocError` 让整次同步失败，宁可这一轮不更新，
   也不写错进度。单行窗口例外（空行本来就没有格子）。

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
# 块内单元格数量对不齐时，最多再花多少次请求去逐层二分。正常块一次都不用；
# 一块里十几个空标题也就是几十次。设上限是防病态数据把一次同步拖成几小时。
MAX_ALIGN_REQUESTS = 256
# 全局请求速率（次/秒）。上限不是带宽而是"会不会被判成爬虫"。
# 这台机器实测的边界：3.5 次/秒能一口气跑完 38 万行不掉块，但同一天再压一轮
# 就撞上 WAF 的 501 人机验证页，封十几分钟。抓取作业最怕的不是慢，是被封之后
# 那一轮静默失败，所以这里按"慢得多但绝不被拦"取值：1 次/秒起步，
# 单块 256 行 → 整张 38 万行约 25 分钟。日常增量只有 2~10 次请求，多花的
# 那几秒看不出来。
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


def _opendoc(pad_id: str, tab_id: str = "", start_row: int = 0, end_row: int = 255) -> dict:
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
        "block_start_col": "0",
        "block_end_col": "63",
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


def _header_fingerprint(pad_id: str, tab_id: str) -> str:
    """表头第一行的文本，用来识破"越界返回表头"这种假数据。

    只取首行：它是作者写在最上面的说明/广告行（"Ctel+F查询文件 最新日期资源
    在下方…"），正常资源不会长这样。

    请求范围必须和 `read_block` 一致（都是 256 行）——同样的表头，用
    `end_row=0` 请求时富文本里的链接不会被解析出来，用 `end_row=255` 才有，
    拿前者当基准会一直比不上，保护等于没写。

    只读一次、按 (pad, tab) 缓存。表头被作者改了也不影响判断：那时越界返回的
    是新表头，指纹跟着变，仍然对得上。
    """
    key = (pad_id, tab_id)
    fp = _header_cache.get(key, "")
    if fp:
        return fp
    cells = _window_cells(pad_id, tab_id, 0, BLOCK_ROWS - 1)
    # 首格可能是空串（作者在第一行前面留了空列），那就退而取第一个有字的格子。
    # **空串绝不能进缓存**：一旦缓存成空，`read_block` 里 `cells[0][0] == ""`
    # 会把后面每一块都判成越界，整张表只剩前几百行——而且不报任何错。
    fp = cells[0][0] if cells and cells[0][0].strip() else next(
        (t for t, _u in cells if t.strip()), ""
    )
    if fp:
        _header_cache[key] = fp
    return fp


def _window_cells(
    pad_id: str, tab_id: str, start_row: int, end_row: int, *, attempts: int = EMPTY_RETRIES
) -> list[tuple[str, list[str]]]:
    """一次请求，按返回顺序取出单元格：(文本, 链接列表)。

    **空响应不等于"这段没数据"。** 请求过密时接口照样返回 200、结构也完整，
    只是 `block_datas` 是空的，几十秒后自己恢复（模块头第 5 条）。所以这里
    空一次就重试，三次都空才抛错——把它当空块放过去，等于整块 256 行静默消失。

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


def _cells_to_row(cells: list[tuple[str, list[str]]]) -> list[str]:
    """单独一行的单元格 → [标题, 链接]。

    缩到一行时，格子的先后就是列的先后；类型信息也还在：带链接的归链接列、
    纯文字归标题列。两格都是文字（作者写的广告行）就按顺序占位——这种行没有
    链接，上层本来就会丢掉。
    """
    row = ["", ""]
    for text, urls in cells:
        value = text or " ".join(urls)
        if not value:
            continue
        slot = 1 if urls else 0
        if row[slot]:             # 该列已有值，占另一列
            slot = 1 - slot
        if not row[slot]:
            row[slot] = value
    return row


def _naive_rows(
    cells: list[tuple[str, list[str]]], n: int
) -> list[list[str]]:
    """兜底：按下标硬配。只在预算耗尽时用，宁可错也不让同步卡死。"""
    texts = [c for c in cells if not c[1] and c[0].strip()]
    links = [c for c in cells if c[1]]
    return [
        [
            texts[i][0] if i < len(texts) else "",
            " ".join(links[i][1]) if i < len(links) else "",
        ]
        for i in range(n)
    ]


def _align_rows(
    pad_id: str,
    tab_id: str,
    start_row: int,
    end_row: int,
    budget: list[int],
    cells: list[tuple[str, list[str]]] | None = None,
) -> list[list[str]]:
    """把 [start_row, end_row] 还原成逐行单元格。

    **为什么不能按下标硬配。** 接口把单元格按类型分组返回：纯文字一堆、带超链接
    一堆（见模块头第 2 条）。只有两堆数量相等时，按下标配才等价于按行配；一旦
    某行多一个文字格（广告行）或少一格（空标题），从那里往后就整体错位，标题会
    配上隔壁行的链接——而且不报错，搜得到、点得开。

    数量对不上就把窗口二分继续缩，缩到只剩一行时，格子顺序就是列顺序，不用再猜。
    正常块仍然是 1 次请求，只有出问题的块才付这个代价。
    """
    n = end_row - start_row + 1
    if cells is None:
        cells = _window_cells(pad_id, tab_id, start_row, end_row)
        budget[0] -= 1
    if not cells:
        return []

    # 空格子不参与计数：表尾那段每行都多带一个空占位格（第 3 列没填），
    # 把它算成文字格的话，每一块都会被判成"对不齐"，然后一路二分到单行，
    # 一块烧掉几百次请求（实测 2 块 517 次）。它不携带任何信息。
    texts = [c for c in cells if not c[1] and c[0].strip()]
    links = [c for c in cells if c[1]]

    # 判据是**两堆数量相等**，不是"等于行数"。整行空白在表里很常见（这一段
    # 256 行里有 128 行是空的），空白行两边都不贡献格子，所以两堆一起变短
    # 并不影响一一对应；只有一边短了才说明从某处起错位了。
    if len(texts) == len(links):
        return [[texts[i][0], " ".join(links[i][1])] for i in range(len(texts))]

    if n <= 1:
        return [_cells_to_row(cells)]

    if budget[0] <= 0:
        log.warning(
            "第 %s~%s 行的单元格数量一直对不齐，预算用完，先按顺序硬配",
            start_row,
            end_row,
        )
        return _naive_rows(cells, n)

    mid = (start_row + end_row) // 2
    return _align_rows(pad_id, tab_id, start_row, mid, budget) + _align_rows(
        pad_id, tab_id, mid + 1, end_row, budget
    )


def read_block(
    pad_id: str, tab_id: str, start_row: int, end_row: int | None = None
) -> list[list[str]]:
    """拉一块（最多 256 行）。空列表表示这块没数据。

    `end_row` 给最后一块用（夹到表长），不给就是整块 256 行。

    **越界不报错，它把表头那块还给你。** `start_row >= max_row` 时接口照样返回
    257 行，内容从第 0 行开始。所以这里拿表头指纹比对，一旦撞上就当"没数据"
    处理——不这么判，上层会以为表比实际长几百上千倍（见模块头第 4 条）。

    指纹比对放在对齐之前：越界时返回的是整块表头，对齐逻辑会以为"数量对不上"
    而一层层二分，白烧几十次请求。
    """
    start_row = max(0, int(start_row))
    end = start_row + BLOCK_ROWS - 1 if end_row is None else int(end_row)
    end = min(end, start_row + BLOCK_ROWS - 1)
    if end < start_row:
        return []
    cells = _window_cells(pad_id, tab_id, start_row, end)
    if not cells:
        return []
    fp = _header_fingerprint(pad_id, tab_id)
    if start_row > 0 and fp and cells[0][0] == fp:
        return []
    return _align_rows(
        pad_id, tab_id, start_row, end, [MAX_ALIGN_REQUESTS], cells=cells
    )


def read_tab(
    url: str,
    *,
    tab_id: str = "",
    start_row: int = 0,
    end_row: int | None = None,
    workers: int = 3,
    on_progress=None,
) -> tuple[list[list[str]], DocMeta]:
    """把一个工作表读成二维表。

    `start_row` 给增量用：只拉这之后的行。`end_row` 默认按元信息里的 max_row。

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

    starts = list(range(start, last, BLOCK_ROWS))
    rows: list[list[str]] = []
    req_before = request_count()
    t0 = time.monotonic()

    def _one(offset: int) -> list[list[str]]:
        # 最后一块要夹到表长：`max_row` 不是 256 的整数倍，不夹的话窗口会越过
        # 表尾一百多行，而越界部分返回的是**表头**内容——它会混进这一块，
        # 让"文字格数 = 链接格数"的对齐判据失效，一路二分到单行（实测一块
        # 烧掉 257 次请求）。夹住之后最后一块就是普通的短块。
        end = min(offset + BLOCK_ROWS - 1, last - 1)
        chunk = read_block(meta.pad_id, tab.tab_id, offset, end)
        if chunk or end >= last - 1:
            return chunk
        # 整块读空但还没到表尾：正常表里不会出现。拿单行复核一次——单行有格子
        # 就说明是限流把整块吞了。这时候必须抛错让整次同步失败，因为上层一旦
        # 认为"读完了"，就会把 last_row 推到表尾，这一块永远补不回来。
        if _window_cells(meta.pad_id, tab.tab_id, offset, offset):
            raise DocError(
                f"第 {offset} 行整块读空、单行却有数据，疑似被限流；这一轮不写进度"
            )
        return []

    if workers <= 1:
        for offset in starts:
            rows.extend(_one(offset))
            if on_progress:
                on_progress(offset, last)
    else:
        # 顺序必须在最后还原：并发返回是无序的，直接 extend 会把整张表打乱
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for idx, chunk in enumerate(pool.map(_one, starts), 1):
                rows.extend(chunk)
                if on_progress and idx % 20 == 0:
                    on_progress(starts[idx - 1], last)

    if on_progress:
        on_progress(last, last)
    log.info(
        "读表 %s[%s~%s]：%s 块 / %s 次请求 / %s 行（%.1f 秒）",
        tab.tab_id, start, last - 1, len(starts), request_count() - req_before,
        len(rows), time.monotonic() - t0,
    )
    return rows, meta
