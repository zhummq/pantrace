"""分享链接识别与规范化。

竞品原实现是 8 段平铺 if + 两处 listFn 选择，改个规则要同时动几个分支；
这里把「正则 → 类型」抽成规则表，解析退化成一次查表。新增网盘只追加一条。

两个不能省的细节：

- 百度的 `share/init?surl=` 形式拿到的是 surl，真实 fid 要补一个前导 `1`。
  少了这一步，列表接口会返回"文件不存在"，但链接本身是好的。
- 提取码正则必须限定字符集 `[a-zA-Z0-9]{4}`。放宽成 `.` 会把后面整段
  中文一起吞掉，然后就变成"提取码错误"，而用户明明复制对了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

# 可识别的分享域名白名单。不在名单里的链接识别成 unknown，不进展示链路。
#
# 早期这份名单等于"我们能转存的盘"，123 / 天翼被排除在外——后果是它们
# `parse().ok` 为 False，被 `store._prepare` 静默丢掉，一条都进不了库。
# 2026-11 放开：**能识别的盘都要能入库**，转存不了只是 `/go` 回退原链接，
# 那是响应时的事，不该在写入前就判死。
ALLOWED_HOSTS = frozenset({
    "pan.quark.cn",
    "pan.baidu.com",
    "drive.uc.cn",
    "fast.uc.cn",
    "pan.xunlei.com",
    "www.guangyapan.com",
    "guangyapan.com",
    "yun.139.com",
    "caiyun.139.com",
    "www.alipan.com",
    "www.aliyundrive.com",
    # 123 云盘：主域名之外还有一批短域名在轮换，同一个分享会在不同域名下出现
    "www.123pan.com",
    "123pan.com",
    "www.123pan.cn",
    "123pan.cn",
    "www.123684.com",
    "123684.com",
    "www.123865.com",
    "123865.com",
    "www.123912.com",
    "123912.com",
    "www.123592.com",
    "123592.com",
    # 天翼云盘
    "cloud.189.cn",
})

PASSCODE_RE = re.compile(
    r"(?:密|提取|访问|訪問|key=|pwd=|password=|code=|passcode=|:|：|[码碼])\s*([a-zA-Z0-9]{4})",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ParsedShare:
    platform: str = "unknown"
    pwd_id: str = ""
    passcode: str = ""
    url: str = ""

    @property
    def ok(self) -> bool:
        return self.platform != "unknown" and bool(self.pwd_id)


@dataclass(slots=True)
class _Rule:
    platform: str
    pattern: re.Pattern[str]
    fid_prefix: str = ""
    # 是否用 PASSCODE_RE 从整条 URL 里抠提取码。
    # 天翼新版 `/web/share?code=xxx` 必须关掉：`code=` 是分享 id 不是提取码，
    # `PASSCODE_RE` 里正好也有 `code=`，开着会把 `Qn2mYv` 截成 `Qn2m` 当密码。
    scan_passcode: bool = True


_RULES: tuple[_Rule, ...] = (
    _Rule("quark", re.compile(r"pan\.quark\.cn/s/([a-zA-Z0-9_-]+)", re.I)),
    _Rule("uc", re.compile(r"(?:drive|fast)\.uc\.cn/s/([a-zA-Z0-9_-]+)", re.I)),
    _Rule("baidu", re.compile(r"pan\.baidu\.com/s/([a-zA-Z0-9_-]+)", re.I)),
    # share/init?surl=xxx → fid 要补前导 1
    _Rule("baidu", re.compile(r"pan\.baidu\.com/share/init\?surl=([a-zA-Z0-9_-]+)", re.I), "1"),
    _Rule("xunlei", re.compile(r"pan\.xunlei\.com/s/([a-zA-Z0-9_-]+)", re.I)),
    _Rule("ali", re.compile(r"(?:alipan|aliyundrive)\.com/s/([a-zA-Z0-9_-]+)", re.I)),
    # 123：多个短域名轮换，分享页有时带 .html 尾巴，共享 id 本身允许连字符
    _Rule(
        "123",
        re.compile(
            r"123(?:pan|684|865|912|592)\.(?:com|cn)/s/([a-zA-Z0-9_-]+?)(?:\.html)?(?=[?#]|$)",
            re.I,
        ),
    ),
    # 天翼两种形态：老的 /t/<id>，新的 /web/share?code=<id>。
    # 老的 /t/ 后面常直接粘中文"（访问码：xxxx）"，字符集收到中文之前就停。
    _Rule("tianyi", re.compile(r"cloud\.189\.cn/t/([a-zA-Z0-9_-]+)", re.I)),
    _Rule(
        "tianyi",
        re.compile(r"cloud\.189\.cn/web/share\?(?:[^#]*?[&?])?code=([a-zA-Z0-9_-]+)", re.I),
        scan_passcode=False,
    ),
)

# 光鸭的分享 id 常在 query 里，大小写还不统一，单独处理
_GUANGYA_KEYS = ("shareId", "share_id", "id", "sid")


def normalize(raw_url: str) -> str:
    """用户粘贴的分享文案 → 干净的 URL。

    全角问号/和号转半角、去掉所有空白。微信里复制出来的分享几乎全带这些，
    不转的话 `urlparse` 拿不到 query，提取码就丢了。
    """
    text = str(raw_url or "").strip()
    text = text.replace("？", "?").replace("＆", "&")
    return re.sub(r"\s+", "", text)


def extract_passcode(text: str) -> str:
    match = PASSCODE_RE.search(text or "")
    return match.group(1) if match else ""


_QUERY_PWD_KEYS = ("pwd", "passcode", "accessCode", "password")

# 粘在链接尾巴上的括号段。微信里复制出来的分享文案长这样：
#   https://cloud.189.cn/t/AbCdEfGh（访问码：3f2a）
# clean.py 的正则是**故意**把括号段一起提出来让 `extract_passcode` 能取到码的，
# 所以走到这一步 url 一定带着它。原样存库，`/go` 回跳过去的 path 就是个 404。
_GLUED_TAILS = ("（", "(", "%EF%BC%88", "%E3%80%90", "%5B")


def _strip_glued_tail(url: str) -> str:
    cut = len(url)
    for marker in _GLUED_TAILS:
        pos = url.find(marker)
        if pos != -1:
            cut = min(cut, pos)
    if cut == len(url):
        return url
    # 截断点之前可能留了空格或多余标点，一起收掉
    return url[:cut].strip(" \t）)")


def _query_passcode(url: str) -> str:
    """只看 query 里的显式密码字段。

    给"query 本身就含 code= 分享 id"的平台用（天翼 /web/share）。这类链接上
    没有提取码语义，宁可留空——留空最多是多问一次，猜错是直接把用户送进
    "提取码错误"页面。
    """
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return ""
    lowered = {k.lower(): v for k, v in query.items()}
    for key in _QUERY_PWD_KEYS:
        values = query.get(key) or lowered.get(key.lower()) or []
        if values and values[0].strip():
            return values[0].strip()[:8]
    return ""


def _host_ok(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ALLOWED_HOSTS


def _guangya(url: str) -> ParsedShare | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in ("www.guangyapan.com", "guangyapan.com"):
        return None
    query = parse_qs(parsed.query)
    lowered = {k.lower(): v for k, v in query.items()}
    share_id = ""
    for key in _GUANGYA_KEYS:
        values = query.get(key) or lowered.get(key.lower()) or []
        if values and values[0].strip():
            share_id = values[0].strip()
            break
    if not share_id:
        # 路径形态 /s/xxx、/share/xxx、/link/xxx
        match = re.search(r"/(?:s|share|link|download)/([a-zA-Z0-9_-]+)", parsed.path or "")
        if match:
            share_id = match.group(1)
    if not share_id:
        return None
    passcode = ""
    for key in ("pwd", "code", "passcode", "accessCode"):
        values = query.get(key) or lowered.get(key.lower()) or []
        if values and values[0].strip():
            passcode = values[0].strip()
            break
    return ParsedShare("guangya", share_id, passcode or extract_passcode(url), url)


def _c139(url: str) -> ParsedShare | None:
    host = (urlparse(url).hostname or "").lower()
    if host not in ("yun.139.com", "caiyun.139.com"):
        return None
    # 形态：.../shareweb/#/w/i/<linkId>&pwd=<code>
    match = re.search(r"w/i/([^?#]+)", url, re.I)
    if not match:
        return None
    fragment = match.group(1)
    link_id, _, tail = fragment.partition("&")
    passcode = ""
    if tail:
        pwd_match = re.match(r"pwd=(.+)", tail, re.I)
        if pwd_match:
            passcode = pwd_match.group(1).strip()
        else:
            alnum = re.sub(r"[^a-zA-Z0-9]", "", tail)
            passcode = alnum[-4:]
    if not link_id:
        return None
    return ParsedShare("c139", link_id, passcode, url)


def parse(raw_url: str) -> ParsedShare:
    """分享链接 → (网盘类型, 分享 id, 提取码)。识别不出返回 platform=unknown。"""
    url = normalize(raw_url)
    if not url:
        return ParsedShare()
    if not _host_ok(url):
        return ParsedShare(url=url)

    for rule in _RULES:
        match = rule.pattern.search(url)
        if match and match.group(1):
            code = extract_passcode(url) if rule.scan_passcode else _query_passcode(url)
            return ParsedShare(
                rule.platform,
                rule.fid_prefix + match.group(1),
                code,
                # 取码用原文，落库用去尾的：顺序不能反，反了就把码一起截掉了
                _strip_glued_tail(url),
            )

    for parser in (_guangya, _c139):
        parsed = parser(url)
        if parsed is not None:
            return parsed

    return ParsedShare(url=url)


def platform_of(url: str) -> str:
    return parse(url).platform


def supported(url: str) -> bool:
    return parse(url).ok
