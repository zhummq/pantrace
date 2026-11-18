"""上游协议层：请求体拼装 + 鉴权头 + 返回归一化。

**这里不发请求。** 取数已经统一走 `app/search/plugins/`（pansou 插件机制），
本模块只提供"怎么跟这条上游说话"的三件事，插件和探针共用同一份口径：

| 函数 | 作用 |
|---|---|
| `build_envelope()` | 拼 path / method / params（含 apikey、searchIn、分页、平台筛选） |
| `upstream_headers()` | 鉴权头，唯一有效的是 `x-idr-sign` |
| `normalize_payload()` | 校验 code（1004/1005 分人话）+ 字段归一化 |

为什么不把这些留在插件里：pansou 的插件是"一个源一个文件"，而这条上游的
参数与错误码是所有源共用的（以后接别的上游也是同样三件事）。写在插件里，
加第二个源时就会出现第二份 envelope 拼装，迟早漂移成两套行为。

鉴权实测结论（拆变量验证过）：
- 只带 sid、去掉 cf_clearance：✅ 通
- 带 sid + cf_clearance、去掉签名：❌ code 1004 illegal request
- 什么 cookie 都不带、只有签名：✅ 通
即 cf_clearance 和 sid 都不需要，唯一有效的是 x-idr-sign。
签名会过期，过期返回 code 1004，需要在浏览器重新抓一份写进 .env。
"""

from __future__ import annotations

import re

from .config import get_settings

TAG_RE = re.compile(r"<[^>]+>")

PLATFORM_NAMES = {
    "quark": "夸克网盘", "baidu": "百度网盘", "ali": "阿里云盘", "uc": "UC网盘",
    "xunlei": "迅雷网盘", "115": "115网盘", "123": "123云盘", "tianyi": "天翼云盘",
    "yidong": "移动云盘", "wo": "联通云盘", "guangya": "光鸭云盘", "lanzou": "蓝奏云",
    "weiyun": "腾讯微云", "ctfile": "城通网盘", "pikpak": "PikPak", "magnet": "磁力链接",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)

# 上游 body 里的固定路径，与 apikey 无关，改动需同步控制台
UPSTREAM_PATH = "/api/b9d1/search"


class UpstreamError(RuntimeError):
    """上游失败。code 保留上游原始返回码，便于上层判断是不是签名过期。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _clean(value) -> str:
    if not isinstance(value, str):
        return ""
    return TAG_RE.sub("", value).strip()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _normalize(data: dict, page: int, page_size: int) -> dict:
    result = data.get("result") or {}
    items = []
    for raw in result.get("items") or []:
        if not isinstance(raw, dict):
            continue
        platform = _clean(raw.get("platform")).lower()
        items.append({
            "hsid": _clean(raw.get("hsid")),
            "platform": platform,
            "platformName": _clean(raw.get("platformName")) or PLATFORM_NAMES.get(platform, "其他"),
            "title": _clean(raw.get("title")),
            "shareUrl": _clean(raw.get("shareUrl")),
            "shareCode": _clean(raw.get("shareCode")),
            "sharePwd": _clean(raw.get("sharePwd")),
            "fileCount": _to_int(raw.get("fileCount")),
            "sizeBytes": _to_int(raw.get("sizeBytes")),
            "indexedTime": "",
        })

    pg = result.get("pagination") or {}
    return {
        "items": items,
        "pagination": {
            "page": _to_int(pg.get("page"), page),
            "pageSize": _to_int(pg.get("pageSize"), page_size),
            "total": _to_int(pg.get("total")),
            "totalPages": _to_int(pg.get("totalPages")),
        },
        # 耗时由 main.py 自己掐表（tookMs），这里只透传上游计费字段
        "_meta": {"credits": _to_int(data.get("credits"))},
    }


def build_envelope(query: str, platforms: list[str], page: int, page_size: int, scope: str) -> dict:
    """拼上游请求体。

    插件层（`app/search/plugins/haisou.py`）与探针共用这一份参数拼装，
    避免"某处少传一个字段就搜出另一批结果"这种漂移。
    """
    params = {
        "apikey": get_settings().api_key,
        "query": query,
        "searchIn": scope if scope in ("title", "all") else "title",
        "page": page,
        "pageSize": page_size,
    }
    # 空数组会被上游判成参数错误，无筛选时整个字段不传
    if platforms:
        params["platforms"] = platforms

    return {"path": UPSTREAM_PATH, "method": "POST", "params": params}


def upstream_headers() -> dict:
    """上游鉴权头。唯一有效的是 `x-idr-sign`（拆变量实测过，见模块 docstring）。"""
    settings = get_settings()
    if not settings.idr_sign:
        raise UpstreamError("未配置 IDR_SIGN，无法请求上游")
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://www.idatariver.com",
        "referer": "https://www.idatariver.com/",
        "x-idr-locale": "zh-cn",
        "x-idr-sign": settings.idr_sign,
        "x-idr-time": settings.idr_time,
    }


def normalize_payload(data: dict, page: int, page_size: int) -> dict:
    """校验上游返回码 + 归一化。

    错误码在这里统一分出人话，插件层直接复用：1004 签名过期、1005 apikey
    无效/余额不足，两者都不是"重试能好"的错误。
    """
    code = _to_int(data.get("code"), -1)
    if code == 1004:
        raise UpstreamError("IDR 签名已失效（1004），需在浏览器重新抓取 X-IDR-Sign 写入 .env", code=1004)
    if code == 1005:
        raise UpstreamError("上游判定 apikey 无效或余额不足（1005）", code=1005)
    if code != 0:
        msg = _clean(data.get("msg"))
        raise UpstreamError(f"上游失败（{code}）{msg}", code=code)

    return _normalize(data, page, page_size)

