"""链接清洗层。逐行翻译 pansou 的 `util/regex_util.go` 与 `util/parser_util.go`。

对照关系（行号指 pansou 仓库）：

| 本文件 | pansou 源码 |
|---|---|
| 正则常量 | `regex_util.go:10-65` |
| `get_link_type` | `regex_util.go:68-128` `GetLinkType` |
| `clean_*_url` | `regex_util.go:131-477` 七个 Clean 函数 |
| `normalize_*_url` | `parser_util.go:80-139` 五个 normalize 函数 |
| `extract_password` | `regex_util.go:490-643` `ExtractPassword` |
| `is_valid_password` | `regex_util.go:646-653` |
| `extract_net_disk_links` | `regex_util.go:656-941` |
| `normalize_url_for_comparison` | `regex_util.go:945-957` |
| `normalize_url` / `is_supported_link` | `parser_util.go:16-77` |
| `anchor_candidates` / `extract_code_near` / `password_for` | `parser_util.go:286-389` |
| `LinkCandidates` | `parser_util.go:433-539` |

三处语言差异（Go 没有对应物，必须换写法，其余一律没动）：

1. **字符串索引**。Go 的 `len`/切片按**字节**，Python 按**字符**。凡是用长度做阈值的地方
   （`extract_code_near` 的 60 字符窗口、`anchor_candidates` 的 5 字符判断、
   完整度打分里的 `len(title)/10`）都改写成 `_blen()`，保证同一个中文标题在两边得分相同。
2. **正则语义**。Go 的 `\\w`/`\\d`/`\\s` 只认 ASCII，Python 默认认 Unicode。
   全部加 `re.ASCII` 才等价 —— 否则 `[\\w.-]+\\.` 会去匹配中文域名段。
3. **`url.QueryUnescape`**。Go 版会把 `+` 还原成空格，Python 对应的是 `unquote_plus`；
   Go 解码失败会报错并回退原串，Python 不报错，此处行为已对齐（保留原串）。

一处**已知的 Go 侧瑕疵**，翻译时没有复制：`ExtractPassword` 里用
`strings.IndexAny(part, ":：")` 取到的是字节下标，再 `part[codeStart+1:]` 只跳 1 个字节。
中文冒号是 3 字节，这样切出来的是**非法 UTF-8**，`isValidPassword` 必然为假，
于是该分支实际失效、最终靠后面的 `PasswordPattern` 兜住。Python 按字符切片直接拿到干净结果，
属于「照文档意图实现」，结果通常一致（见 `test_clean.py` 里对中文冒号的用例）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote_plus

from .types import Link

# Go 的字符类只认 ASCII，且模式里带 (?i) 的都按 ASCII 折大小写。
_A = re.ASCII
_AI = re.ASCII | re.IGNORECASE

# ---------------------------------------------------------------- 正则常量
# regex_util.go:10-65，一条一条抄，顺序不动。

ALL_PAN_LINKS = re.compile(
    r"(?:(?:magnet:\?xt=urn:btih:[a-zA-Z0-9]+)"
    r"|(?:ed2k://\|file\|[^|]+\|\d+\|[A-Fa-f0-9]+\|/?)"
    r"|(?:https?://(?:(?:[\w.-]+\.)?(?:pan\.(?:baidu|quark)\.cn|(?:www\.)?(?:alipan|aliyundrive)\.com"
    r"|drive\.uc\.cn|cloud\.189\.cn|(?:www\.)?(?:yun|caiyun)\.139\.com|caiyun\.feixin\.10086\.cn"
    r"|(?:www\.)?123(?:684|685|912|pan|592)\.(?:com|cn)|115\.com|115cdn\.com|anxia\.com"
    r"|pan\.xunlei\.com|mypikpak\.com|guangyapan\.com))(?:/[^\s'\"<>()]*)?))",
    _AI,
)

BAIDU_PAN = re.compile(r"https?://pan\.baidu\.com/s/[a-zA-Z0-9_-]+(?:\?pwd=[a-zA-Z0-9]{4})?", _AI)
QUARK_PAN = re.compile(r"https?://pan\.quark\.cn/s/[a-zA-Z0-9_-]+", _AI)
XUNLEI_PAN = re.compile(r"https?://pan\.xunlei\.com/s/[a-zA-Z0-9_-]+(?:\?pwd=[a-zA-Z0-9]{4})?(?:#)?", _AI)
TIANYI_PAN = re.compile(
    r"https?://cloud\.189\.cn/t/[a-zA-Z0-9_-]+(?:%[0-9A-Fa-f]{2})*(?:（[^）]*）)?", _AI
)
UC_PAN = re.compile(r"https?://drive\.uc\.cn/s/[a-zA-Z0-9_-]+(?:\?public=\d)?", _AI)
PAN123 = re.compile(
    r"https?://(?:www\.)?123(?:684|865|685|912|pan|592)\.(?:com|cn)/s/[a-zA-Z0-9_-]+"
    r"(?:\?(?:%E6%8F%90%E5%8F%96%E7%A0%81|提取码)[:：][a-zA-Z0-9]+)?",
    _AI,
)
PAN115 = re.compile(
    r"https?://(?:115\.com|115cdn\.com|anxia\.com)/s/[a-zA-Z0-9_-]+(?:\?password=[a-zA-Z0-9]{4})?(?:#)?",
    _AI,
)
# PikPak 没有专属清洗函数，但必须有专属正则：只靠末尾的通用兜底，`[^\s'"<>()]*`
# 会把紧跟在链接后的中文标题一起吞进去（`.../s/VOabc资源合集`），落库后 /go 回跳 404。
PIKPAK_PAN = re.compile(r"https?://(?:www\.)?mypikpak\.com/s/[a-zA-Z0-9_-]+", _AI)
ALIYUN_PAN = re.compile(r"https?://(?:www\.)?(?:alipan|aliyundrive)\.com/s/[a-zA-Z0-9_-]+", _AI)
GUANGYA_PAN = re.compile(r"https?://(?:www\.)?guangyapan\.com/s/[a-zA-Z0-9_-]+", _AI)
MOBILE_PAN = re.compile(
    r"https?://(?:(?:www\.)?yun\.139\.com/shareweb/#/w/i/[a-zA-Z0-9]+"
    r"|(?:www\.)?caiyun\.139\.com/(?:w/i/[a-zA-Z0-9]+|m/i\?[a-zA-Z0-9]+)[^\s<>\"']*"
    r"|caiyun\.feixin\.10086\.cn/[a-zA-Z0-9]+)",
    _AI,
)

PASSWORD = re.compile(r"(?:(?:提取|访问|提取密|密)码|pwd)[：:]\s*([a-zA-Z0-9]{4})(?:[^a-zA-Z0-9]|$)", _AI)
URL_PASSWORD = re.compile(r"[?&]pwd=([a-zA-Z0-9]{4})(?:[^a-zA-Z0-9]|$)", _AI)
NEARBY_PASSWORD = re.compile(r"(?:(?:提取|访问|提取密|密)码|pwd)[：:\s]*([a-zA-Z0-9]{4,6})(?:[^a-zA-Z0-9]|$)", _AI)
BAIDU_PASSWORD = re.compile(r"(?:链接：.*?提取码：|密码：|提取码：|pwd=|pwd:|pwd：)([a-zA-Z0-9]{4})(?:[^a-zA-Z0-9]|$)", _AI)
TIANYI_ACCESS_CODE = re.compile(r"(?:（访问码：|%EF%BC%88%E8%AE%BF%E9%97%AE%E7%A0%81%EF%BC%9A)([a-zA-Z0-9]+)(?:）|%EF%BC%89)", _A)
XUNLEI_PWD = re.compile(r"\?pwd=([a-zA-Z0-9]{4})", _A)
PAN115_PASSWORD = re.compile(r"password=([a-zA-Z0-9]{4})", _A)
PAN123_EXTRACT_CODE = re.compile(r"(?:提取码|%E6%8F%90%E5%8F%96%E7%A0%81)[:：]([a-zA-Z0-9]+)", _A)

# 提取码后面可能紧跟的结束字符。原样抄，含重复项（"；"与";"与"，"各出现两次）。
_END_MARKERS_COMMON = (" ", "\n", "\t", "，", "。", "；", ";", "，", ",")

_END_MARKERS_123 = _END_MARKERS_COMMON + (
    "📁", "🔍", "标签", "🏷", "📎", "🔗", "📌", "📋", "📂", "🗂️", "🔖", "📚", "📒", "📔", "📕",
    "📓", "📗", "📘", "📙", "📄", "📃", "📑", "🧾", "📊", "📈", "📉", "🗒️", "🗓️", "📆", "📅",
    "🗑️", "🔒", "🔓", "🔏", "🔐", "🔑", "🗝️",
)

# regex_util.go:565 与 :594 两处 IndexAny 用的字符集。第二处少了 "📅"，原样保留。
_CODE_STOP_1 = " \t\n\r，。；;,🏷📁🔍📎🔗📌📋📂🗂️🔖📚📒📔📕📓📗📘📙📄📃📑🧾📊📈📉🗒️🗓️📆📅🗑️🔒🔓🔏🔐🔑🗝️"
_CODE_STOP_2 = " \t\n\r，。；;,🏷📁🔍📎🔗📌📋📂🗂️🔖📚📒📔📕📓📗📘📙📄📃📑🧾📊📈📉🗒️🗓️📆🗑️🔒🔓🔏🔐🔑🗝️"


def _blen(s: str) -> int:
    """Go 的 `len(string)` 是字节数。凡是拿长度当阈值的判断都必须走这里。"""
    return len(s.encode("utf-8", "surrogatepass"))


def _unescape(s: str) -> str:
    """对应 Go 的 `url.QueryUnescape`：`+` 还原成空格，失败回退原串。"""
    try:
        return unquote_plus(s)
    except Exception:
        return s


# ---------------------------------------------------------------- 类型判断


def get_link_type(url: str) -> str:
    """regex_util.go:68 `GetLinkType`。"""
    url = url.lower()

    # 处理可能带有"链接："前缀的情况
    if "链接：" in url or "链接:" in url:
        url = url.split("链接")[1]
        if url.startswith("：") or url.startswith(":"):
            url = url[1:]
        url = url.strip()

    if "ed2k:" in url:
        return "ed2k"
    if url.startswith("magnet:"):
        return "magnet"

    if "pan.baidu.com" in url:
        return "baidu"
    if "pan.quark.cn" in url:
        return "quark"
    if "alipan.com" in url or "aliyundrive.com" in url:
        return "aliyun"
    if "guangyapan.com" in url:
        return "guangya"
    if "cloud.189.cn" in url:
        return "tianyi"
    if "drive.uc.cn" in url:
        return "uc"
    if "caiyun.139.com" in url or "yun.139.com" in url or "caiyun.feixin.10086.cn" in url:
        return "mobile"
    if "115.com" in url or "115cdn.com" in url or "anxia.com" in url:
        return "115"
    if "mypikpak.com" in url:
        return "pikpak"
    if "pan.xunlei.com" in url:
        return "xunlei"

    # 123 网盘有多个域名
    if (
        "123684.com" in url
        or "123685.com" in url
        or "123865.com" in url
        or "123912.com" in url
        or "123pan.com" in url
        or "123pan.cn" in url
        or "123592.com" in url
    ):
        return "123"

    return "others"


def is_supported_link(url: str) -> bool:
    """parser_util.go:27 `isSupportedLink`。"""
    lower = url.lower()
    for pat in (
        BAIDU_PAN, TIANYI_PAN, UC_PAN, PAN123, GUANGYA_PAN,
        QUARK_PAN, XUNLEI_PAN, PAN115, PIKPAK_PAN, MOBILE_PAN,
    ):
        if pat.search(lower):
            return True
    return bool(ALL_PAN_LINKS.search(lower))


# ---------------------------------------------------------------- 单类型清洗
# 注意：每个函数都只在自己那个域名出现时动手，否则原样返回 —— 这是调用方
# 「按类型分别清洗」的前提，不要合并成一个通用函数。


def _cut_at_markers(url: str, markers: tuple[str, ...]) -> str:
    """Go 里重复了七遍的「找最早的结束标记并截断」。`idx > 0` 的严格大于要保留。"""
    min_end = len(url)
    for marker in markers:
        idx = url.find(marker)
        if 0 < idx < min_end:
            min_end = idx
    if min_end < len(url):
        return url[:min_end]
    return url


def _ensure_protocol(url: str, start_idx: int) -> str:
    """Clean123/115/Aliyun 共用的「补协议头 / 去掉前缀文本」三段逻辑。"""
    has_protocol = url.startswith("http://") or url.startswith("https://")
    if not has_protocol:
        return "https://" + url[start_idx:]
    if start_idx > 0:
        protocol_idx = url.find("://")
        if protocol_idx >= 0:
            protocol = url[: protocol_idx + 3]
            return protocol + url[start_idx:]
    return url


def clean_baidu_pan_url(url: str) -> str:
    """regex_util.go:131 `CleanBaiduPanURL`。"""
    if "https://pan.baidu.com/s/" in url:
        start_idx = url.find("https://pan.baidu.com/s/")
        if start_idx >= 0:
            url = url[start_idx:]
            # ?pwd= 也是结束标记之一：先截到它前面，再单独处理参数
            url = _cut_at_markers(url, _END_MARKERS_COMMON + ("?pwd=",))

            if "?pwd=" in url:
                pwd_idx = url.find("?pwd=")
                if pwd_idx >= 0 and len(url) > pwd_idx + 5:  # ?pwd= 有 5 个字符
                    pwd_end_idx = pwd_idx + 9  # ?pwd=xxxx 总共 9 个字符
                    if pwd_end_idx <= len(url):
                        return url[:pwd_end_idx]
                    return url
    return url


def clean_tianyi_pan_url(url: str) -> str:
    """regex_util.go:175 `CleanTianyiPanURL`。"""
    if "https://cloud.189.cn/t/" in url:
        start_idx = url.find("https://cloud.189.cn/t/")
        if start_idx >= 0:
            url = url[start_idx:]
            url = _cut_at_markers(url, _END_MARKERS_COMMON + ("实时", "天翼", "更多"))
            # 标准化成中文形态，用于去重
            url = _unescape(url)
    return url


def clean_uc_pan_url(url: str) -> str:
    """regex_util.go:210 `CleanUCPanURL`。"""
    if "https://drive.uc.cn/s/" in url:
        start_idx = url.find("https://drive.uc.cn/s/")
        if start_idx >= 0:
            url = url[start_idx:]
            # 分享码里可能含 "123"，不能把它当分隔符
            cut = _cut_at_markers(url, _END_MARKERS_COMMON + ("网盘", "夸克", "阿里", "百度"))
            if len(cut) < len(url):
                return cut

            if "?public=" in url:
                public_idx = url.find("?public=")
                if public_idx > 0:
                    if public_idx + 9 <= len(url):  # ?public=1 总共 9 个字符
                        return url[: public_idx + 9]
                    return url[: public_idx + 8]
    return url


def clean_123_pan_url(url: str) -> str:
    """regex_util.go:254 `Clean123PanURL`。"""
    domains = ("123684.com", "123685.com", "123865.com", "123912.com",
               "123pan.com", "123pan.cn", "123592.com")

    is_domain_123 = any(d + "/s/" in url for d in domains)
    if is_domain_123:
        has_protocol = url.startswith("http://") or url.startswith("https://")

        start_idx = -1
        for d in domains:
            idx = url.find(d + "/s/")
            if idx >= 0:
                start_idx = idx
                break

        if start_idx >= 0:
            if not has_protocol:
                url = "https://" + url[start_idx:]
            elif start_idx > 0:
                protocol_idx = url.find("://")
                if protocol_idx >= 0:
                    protocol = url[: protocol_idx + 3]
                    url = protocol + url[start_idx:]

            # 这里不把"提取码"当结束标记，它是 URL 的一部分
            cut = _cut_at_markers(url, _END_MARKERS_123)
            if len(cut) < len(url):
                return cut

            if "%E6%8F%90%E5%8F%96%E7%A0%81" in url:
                url = url.replace("%E6%8F%90%E5%8F%96%E7%A0%81", "提取码", 1)
    return url


def clean_115_pan_url(url: str) -> str:
    """regex_util.go:323 `Clean115PanURL`。"""
    if "115.com/s/" in url or "115cdn.com/s/" in url or "anxia.com/s/" in url:
        start_idx = -1
        idx = url.find("115.com/s/")
        if idx >= 0:
            start_idx = idx
        else:
            idx = url.find("115cdn.com/s/")
            if idx >= 0:
                start_idx = idx
            else:
                idx = url.find("anxia.com/s/")
                if idx >= 0:
                    start_idx = idx

        if start_idx >= 0:
            url = _ensure_protocol(url, start_idx)

            if "?password=" in url:
                pwd_idx = url.find("?password=")
                if pwd_idx > 0 and pwd_idx + 14 <= len(url):  # ?password=xxxx 共 14 个字符
                    return url[: pwd_idx + 14]

            hash_idx = url.find("#")
            if hash_idx > 0:
                return url[:hash_idx]
    return url


def clean_aliyun_pan_url(url: str) -> str:
    """regex_util.go:377 `CleanAliyunPanURL`。"""
    if "alipan.com/s/" in url or "aliyundrive.com/s/" in url:
        start_idx = -1
        for candidate in ("www.alipan.com/s/", "alipan.com/s/",
                          "www.aliyundrive.com/s/", "aliyundrive.com/s/"):
            idx = url.find(candidate)
            if idx >= 0:
                start_idx = idx
                break

        if start_idx >= 0:
            url = _ensure_protocol(url, start_idx)
            cut = _cut_at_markers(url, _END_MARKERS_123)
            if len(cut) < len(url):
                return cut
    return url


def clean_mobile_pan_url(url: str) -> str:
    """regex_util.go:433 `CleanMobilePanURL`。"""
    patterns = (
        "https://yun.139.com/shareweb/#/w/i/",
        "http://yun.139.com/shareweb/#/w/i/",
        "https://www.yun.139.com/shareweb/#/w/i/",
        "http://www.yun.139.com/shareweb/#/w/i/",
        "https://caiyun.139.com/w/i/",
        "http://caiyun.139.com/w/i/",
        "https://www.caiyun.139.com/w/i/",
        "http://www.caiyun.139.com/w/i/",
        "https://caiyun.139.com/m/i?",
        "http://caiyun.139.com/m/i?",
        "https://www.caiyun.139.com/m/i?",
        "http://www.caiyun.139.com/m/i?",
        "https://caiyun.feixin.10086.cn/",
        "http://caiyun.feixin.10086.cn/",
    )

    start_idx = -1
    for prefix in patterns:
        idx = url.find(prefix)
        if idx >= 0:
            start_idx = idx
            break

    if start_idx < 0:
        return url

    url = url[start_idx:]
    url = _cut_at_markers(url, _END_MARKERS_COMMON + ("访问码", "提取码", "密码", "链接", "网盘"))
    return url.strip()


# ---------------------------------------------------------------- 输出前归一


def normalize_baidu_pan_url(url: str, password: str) -> str:
    """parser_util.go:80。密码拼回 `?pwd=`，这正是"密码单独存"的收口处。"""
    url = clean_baidu_pan_url(url)

    if "?pwd=" in url:
        return url

    if password:
        if len(password) > 4:
            password = password[:4]
        return url + "?pwd=" + password

    return url


def normalize_tianyi_pan_url(url: str, password: str) -> str:
    """parser_util.go:102。天翼的访问码不进 URL，保持干净链接。"""
    return clean_tianyi_pan_url(url)


def normalize_uc_pan_url(url: str, password: str) -> str:
    """parser_util.go:112。"""
    return clean_uc_pan_url(url)


def normalize_123_pan_url(url: str, password: str) -> str:
    """parser_util.go:122。"""
    return clean_123_pan_url(url)


def normalize_115_pan_url(url: str, password: str) -> str:
    """parser_util.go:132。密码已在 clean 里截进 URL，这里不再追加。"""
    return clean_115_pan_url(url)


def normalize_aliyun_pan_url(url: str, password: str) -> str:
    """parser_util.go:480（文件内小写函数）。"""
    return clean_aliyun_pan_url(url)


def normalize_url(raw_url: str) -> str:
    """parser_util.go:16。解码百分号编码，用于去重。"""
    return _unescape(raw_url)


def normalize_url_for_comparison(url: str) -> str:
    """regex_util.go:945。去掉协议头再比较，保留完整域名。"""
    idx = url.find("://")
    if idx >= 0:
        url = url[idx + 3:]
    if "%E6%8F%90%E5%8F%96%E7%A0%81" in url:
        url = url.replace("%E6%8F%90%E5%8F%96%E7%A0%81", "提取码", 1)
    return url


# ---------------------------------------------------------------- 提取码


def is_valid_password(password: str) -> bool:
    """regex_util.go:646。只允许 ASCII 字母与数字。"""
    for c in password:
        if not (("0" <= c <= "9") or ("a" <= c <= "z") or ("A" <= c <= "Z")):
            return False
    return True


def extract_password(content: str, url: str) -> str:
    """regex_util.go:490 `ExtractPassword`。

    判定顺序就是优先级：URL 内的码 > 123 特例 > 正文"提取码" > 百度特例 > 通用。
    顺序不能调换 —— 先命中 URL 内的码，才不会把别的链接的码按给这条。
    """
    # 天翼云盘 URL 里的访问码
    if "cloud.189.cn" in url:
        m = TIANYI_ACCESS_CODE.search(url)
        if m:
            return m.group(1)

    # 迅雷 URL 里的 pwd
    if "pan.xunlei.com" in url and "?pwd=" in url:
        m = XUNLEI_PWD.search(url)
        if m:
            return m.group(1)

    # 先从 URL 里取
    m = URL_PASSWORD.search(url)
    if m:
        return m.group(1)

    # 115 URL 里的密码
    if ("115.com" in url or "115cdn.com" in url or "anxia.com" in url) and "password=" in url:
        m = PAN115_PASSWORD.search(url)
        if m:
            return m.group(1)

    domains_123 = ("123684.com", "123685.com", "123865.com", "123912.com",
                   "123pan.com", "123pan.cn", "123592.com")
    if any(d in url for d in domains_123) and ("提取码" in url or "%E6%8F%90%E5%8F%96%E7%A0%81" in url):
        m = PAN123_EXTRACT_CODE.search(url)
        if m:
            return m.group(1)

    if any(d in url for d in domains_123) and "提取码" in url:
        parts = url.split("提取码")
        if len(parts) > 1:
            code_start = _index_any(parts[1], ":：")
            if code_start >= 0 and code_start + 1 < len(parts[1]):
                code = parts[1][code_start + 1:].strip()
                end_idx = _index_any(code, _CODE_STOP_1)
                if end_idx > 0:
                    code = code[:end_idx]
                code = code.strip()
                if 0 < len(code) <= 6 and is_valid_password(code):
                    return code

    # 正文里出现"提取码"：逐段扫，取第一个合法码
    if "提取码" in content:
        for part in content.split("提取码"):
            if not part:
                continue
            code_start = _index_any(part, ":：")
            if code_start < 0 or code_start + 1 >= len(part):
                continue
            code = part[code_start + 1:].strip()

            end_idx = _index_any(code, _CODE_STOP_2)
            if end_idx > 0:
                code = code[:end_idx]
            elif len(code) > 6:
                # 没有明显结束标记，试 4~6 位
                for i in range(4, 7):
                    if i > len(code):
                        break
                    if is_valid_password(code[:i]):
                        code = code[:i]
                        break
                if len(code) > 6:
                    code = code[:4]

            code = code.strip()
            if code and is_valid_password(code):
                return code

    # 百度特定格式
    if "pan.baidu.com" in url.lower():
        m = BAIDU_PASSWORD.search(content)
        if m:
            return m.group(1)

    # 通用
    m = PASSWORD.search(content)
    if m:
        return m.group(1)

    return ""


def _index_any(s: str, chars: str) -> int:
    """Go 的 `strings.IndexAny`：返回任一字符首次出现的位置，没有则 -1。"""
    best = -1
    for c in chars:
        idx = s.find(c)
        if idx >= 0 and (best < 0 or idx < best):
            best = idx
    return best


# ---------------------------------------------------------------- 正文整段提取


def extract_net_disk_links(text: str) -> list[str]:
    """regex_util.go:656 `ExtractNetDiskLinks`。

    十段顺序固定：百度 → 天翼 → UC → 123 → 115 → 阿里 → 光鸭 → 移动 → 夸克 → 迅雷 → 通用兜底。
    前面的段负责清洗，最后的通用段负责"其余"，并把已收过的域名全部跳过。
    """
    links: list[str] = []

    def _strip_tail(u: str) -> str:
        # 原实现里每段都写了一遍：末尾误粘 "https" 就切掉
        if u.endswith("https"):
            return u[:-5]
        return u

    def _append(u: str) -> None:
        u = _strip_tail(u)
        if u:
            links.append(u)

    def _append_dedup(u: str) -> None:
        u = _strip_tail(u)
        if not u:
            return
        norm_new = normalize_url_for_comparison(u)
        for existing in links:
            if normalize_url_for_comparison(existing) == norm_new:
                return
        links.append(u)

    for match in BAIDU_PAN.findall(text):
        _append(clean_baidu_pan_url(match))

    for match in TIANYI_PAN.findall(text):
        _append(clean_tianyi_pan_url(match))

    for match in UC_PAN.findall(text):
        _append(clean_uc_pan_url(match))

    for match in PAN123.findall(text):
        _append_dedup(clean_123_pan_url(match))

    for match in PAN115.findall(text):
        _append_dedup(clean_115_pan_url(match))

    for match in PIKPAK_PAN.findall(text):
        _append_dedup(match.strip())

    for match in ALIYUN_PAN.findall(text):
        _append_dedup(clean_aliyun_pan_url(match))

    for match in GUANGYA_PAN.findall(text):
        _append_dedup(match.strip())

    for match in MOBILE_PAN.findall(text):
        _append_dedup(clean_mobile_pan_url(match))

    # 夸克与迅雷原实现用「互相包含」判重，不是相等
    for match in QUARK_PAN.findall(text):
        u = _strip_tail(match)
        if any((existing in u) or (u in existing) for existing in links):
            continue
        links.append(u)

    for match in XUNLEI_PAN.findall(text):
        u = _strip_tail(match)
        if any((existing in u) or (u in existing) for existing in links):
            continue
        links.append(u)

    # 通用兜底：跳过前面已单独处理的域名
    _HANDLED = (
        "pan.baidu.com", "pan.quark.cn", "pan.xunlei.com", "guangyapan.com",
        "cloud.189.cn", "drive.uc.cn", "yun.139.com", "caiyun.139.com",
        "caiyun.feixin.10086.cn", "123684.com", "123685.com", "123865.com",
        "123912.com", "123pan.com", "123pan.cn", "123592.com",
        "115.com", "115cdn.com", "anxia.com", "mypikpak.com",
    )
    for match in ALL_PAN_LINKS.findall(text):
        u = _strip_tail(match)
        if any(d in u for d in _HANDLED):
            continue

        norm_new = normalize_url_for_comparison(u)
        is_dup = False
        for existing in links:
            norm_existing = normalize_url_for_comparison(existing)
            if (
                norm_existing == norm_new
                or norm_new in norm_existing
                or norm_existing in norm_new
            ):
                is_dup = True
                break
        if not is_dup:
            links.append(u)

    return links


# ---------------------------------------------------------------- 就近取码
# parser_util.go:293-389。这一段是"一条消息里多个链接时提取码不串位"的全部机制，
# 不能省：不按锚定位，ExtractPassword 永远返回正文里第一个合法码。

NEARBY_PASSWORD_WINDOW = 60


def anchor_candidates(link_url: str) -> list[str]:
    """parser_util.go:300。同一个链接在正文里可能的几种写法，越精确越先试。"""
    trimmed = link_url
    i = _index_any(trimmed, "?#")
    if i > 0:
        trimmed = trimmed[:i]
    trimmed = trimmed.rstrip("/")

    candidates = [trimmed]

    no_scheme = trimmed
    i = no_scheme.find("//")
    if i >= 0:
        no_scheme = no_scheme[i + 2:]
    no_www = no_scheme[4:] if no_scheme.startswith("www.") else no_scheme

    if no_www != no_scheme:
        candidates.append(no_www)
    if no_scheme != trimmed:
        candidates.append(no_scheme)

    # 最后退到路径末段（如 /s/pan123 里的 pan123）。4 字符以内不试，太容易撞。
    last_slash = no_www.rfind("/")
    if last_slash >= 0 and _blen(no_www[last_slash + 1:]) > 5:
        candidates.append(no_www[last_slash:])

    return candidates


def extract_code_near(link_url: str, context: str) -> str:
    """parser_util.go:342。

    找不到锚点时的两条规则必须保留：
      - context 短（按钮标签）→ 整段就是这条链接的上下文，直接找；
      - context 长却找不到链接 → 无法判断位置，返回空让调用方回退旧逻辑。
    """
    if not context:
        return ""

    window = ""
    for anchor in anchor_candidates(link_url):
        pos = context.find(anchor)
        if pos < 0:
            continue
        rest = context[pos + len(anchor):]
        # 遇到下一个链接就截断，多链接消息里不能越过下一条
        nxt = rest.find("http")
        if nxt >= 0:
            rest = rest[:nxt]
        window = rest
        break

    if not window:
        if _blen(context) <= 2 * NEARBY_PASSWORD_WINDOW:
            window = context
        else:
            return ""

    if _blen(window) > NEARBY_PASSWORD_WINDOW:
        # 按字节截断再退回字符边界 —— 和 Go 的 utf8.ValidString 回退等价
        window = window.encode("utf-8", "surrogatepass")[:NEARBY_PASSWORD_WINDOW].decode("utf-8", "ignore")

    m = NEARBY_PASSWORD.search(window)
    if m and is_valid_password(m.group(1)):
        return m.group(1)
    return ""


def password_for(link_url: str, primary_context: str, fallback_context: str) -> str:
    """parser_util.go:286。附近取不到就退回整段扫描，保证不退化。"""
    pw = extract_code_near(link_url, primary_context)
    if pw:
        return pw
    return extract_password(fallback_context, link_url)


# ---------------------------------------------------------------- 归并


@dataclass
class LinkCandidates:
    """parser_util.go:433 `linkCandidates`。

    为什么要分两段：密码可能离链接很远（"链接：xxx 提取码：abcd"）。
    百度/天翼/UC/123/115/阿里云这六类必须等整段扫完才知道配哪个密码，
    其余类型可以立刻定稿。
    """

    direct: list[Link] = field(default_factory=list)
    found: dict[str, bool] = field(default_factory=dict)
    by_type: dict[str, dict[str, str]] = field(default_factory=dict)

    def add(self, link_type: str, raw_url: str, password: str) -> None:
        """把一条候选链接归位。"""
        bucket = self.by_type.setdefault(link_type, {})

        if link_type == "baidu":
            # 剥掉 ?pwd=，密码单独存，最后拼回完整形态
            base_url = raw_url
            idx = raw_url.find("?pwd=")
            if idx >= 0:
                base_url = raw_url[:idx]
            self.set_password(bucket, base_url, password)
        elif link_type == "tianyi":
            self.set_password(bucket, clean_tianyi_pan_url(raw_url), password)
        elif link_type == "uc":
            self.set_password(bucket, clean_uc_pan_url(raw_url), password)
        elif link_type == "123":
            self.set_password(bucket, clean_123_pan_url(raw_url), password)
        elif link_type == "115":
            self.set_password(bucket, clean_115_pan_url(raw_url), password)
        elif link_type == "aliyun":
            self.set_password(bucket, clean_aliyun_pan_url(raw_url), password)
        else:
            normalized = normalize_url(raw_url)
            if normalized not in self.found:
                self.found[normalized] = True
                self.direct.append(Link(type=link_type, url=normalized, password=password))

    def set_password(self, bucket: dict[str, str], base_url: str, password: str) -> None:
        """记录 baseURL 对应的密码。**没有密码也要留条目** —— 无码链接同样有效。"""
        if password:
            bucket[base_url] = password
            return
        bucket.setdefault(base_url, "")

    def finalize(self) -> list[Link]:
        """先出直接定稿的，再按类型补全密码。顺序：百度→天翼→UC→123→115→阿里。"""
        links = list(self.direct)

        specs = (
            ("baidu", normalize_baidu_pan_url),
            ("tianyi", normalize_tianyi_pan_url),
            ("uc", normalize_uc_pan_url),
            ("123", normalize_123_pan_url),
            ("115", normalize_115_pan_url),
            # 阿里云盘 URL 通常不含密码参数
            ("aliyun", lambda base_url, _pw: clean_aliyun_pan_url(base_url)),
        )

        for typ, normalize in specs:
            for base_url, password in self.by_type.get(typ, {}).items():
                normalized_url = normalize(base_url, password)
                if normalized_url not in self.found:
                    self.found[normalized_url] = True
                    links.append(Link(type=typ, url=normalized_url, password=password))

        return links
