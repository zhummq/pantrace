"""腾讯文档公开表格 → 二维表。

这一层只管"文档 → `list[list[str]]`"，不碰入库、不碰检活。
进来的东西交给 `intake.parse_rows`，那才是"行 → 资源"的地方。

**三个必须知道的事实，都是逆向出来的，改代码前先看这一段。**

1. **数据不是 HTML。** 页面里一个网盘链接都没有，正则扫 HTML 必然是零条。
   真正的数据在 `GET /dop-api/opendoc` 的
   `clientVars.collab_client_vars.initialAttributedText.text[0]` 里：
   `workbook` 和 `block_datas[].related_sheet` 都是 **base64 + zlib 的 protobuf**。
   不带 `wb=1&nowb=0` 和 `needSheetState=1&sliceStates=1` 时，接口只回壳子。

2. **表格是列式存储，不是行式。** block 顶层 field N 就是第 N-1 列的**全部单元格**，
   行号 = 数组下标。所以"标题列 257 个、链接列 255 个"不是错位，是链接列尾巴上
   两行是空的——空单元格不占位。按索引对齐即可，别去猜。

3. **一次只给 256 行。** 靠 `block_start_row` / `block_end_row` 翻页，
   38 万行的表要 1400+ 次请求。所以增量只能靠 `max_row` 变化判断，
   每次都全量重拉等于每天给人家文档刷 1400 次。

**只支持"任何人可查看"的文档。** 需要登录的会返回空 block，`block_datas` 为空，
这里不抛异常而是返回空——上层据此报"文档没权限"，比抛栈更容易让人看懂。
"""

from __future__ import annotations

import base64
import logging
import re
import threading
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
        resp = _session().get(
            OPENDOC,
            params=params,
            headers={"User-Agent": UA, "Referer": f"https://docs.qq.com/sheet/{pad_id}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DocError(f"连不上腾讯文档：{exc.__class__.__name__}") from exc
    if resp.status_code != 200:
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


def read_block(pad_id: str, tab_id: str, start_row: int) -> list[list[str]]:
    """拉一块（最多 256 行）。空列表表示这块没数据。"""
    end = start_row + BLOCK_ROWS - 1
    payload = _opendoc(pad_id, tab_id, start_row, end)
    for _end_row, blob in _block_offsets(payload):
        rows = parse_block(blob)
        if rows:
            return rows
    return []


def read_tab(
    url: str,
    *,
    tab_id: str = "",
    start_row: int = 0,
    end_row: int | None = None,
    workers: int = 6,
    on_progress=None,
) -> tuple[list[list[str]], DocMeta]:
    """把一个工作表读成二维表。

    `start_row` 给增量用：只拉这之后的行。`end_row` 默认按元信息里的 max_row。
    并发开到 6：38 万行串行要七分钟，6 路压到一分半，再高容易被限流。
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

    def _one(offset: int) -> list[list[str]]:
        try:
            return read_block(meta.pad_id, tab.tab_id, offset)
        except DocError as exc:
            log.warning("腾讯文档第 %s 行读失败：%s", offset, exc)
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
    return rows, meta
