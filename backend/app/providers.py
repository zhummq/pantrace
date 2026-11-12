"""上游调用：idatariver 控制台 MAPI（唯一通道）。

为什么只留一条：
以前挂过 haisou.cc 直连和 apiok.us 套壳做降级，实际跑下来都不合适——
haisou 匿名按 IP 每日 100 点，用完就废；apiok 按积分计费，耗时 2.5~75 秒波动。
降级链更坏的副作用是：签名过期后请求被悄悄转到慢通道，日志里只剩"慢"，
看不出真正原因是签名失效。单通道反而让故障一眼可见。

鉴权实测结论（拆变量验证过）：
- 只带 sid、去掉 cf_clearance：✅ 通
- 带 sid + cf_clearance、去掉签名：❌ code 1004 illegal request
- 什么 cookie 都不带、只有签名：✅ 通
即 cf_clearance 和 sid 都不需要，唯一有效的是 x-idr-sign。
签名会过期，过期返回 code 1004，需要在浏览器重新抓一份写进 .env。
"""

from __future__ import annotations

import re

import requests

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


_session: requests.Session | None = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        settings = get_settings()
        s.trust_env = settings.use_env_proxy
        s.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
        })
        if settings.proxy_url:
            s.proxies.update({"http": settings.proxy_url, "https": settings.proxy_url})
        _session = s
    return _session


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


def search(query: str, platforms: list[str], page: int, page_size: int, scope: str) -> dict:
    """请求上游并归一化返回。失败抛 UpstreamError。"""
    settings = get_settings()
    if not settings.idr_sign:
        raise UpstreamError("未配置 IDR_SIGN，无法请求上游")

    params = {
        "apikey": settings.api_key,
        "query": query,
        "searchIn": scope if scope in ("title", "all") else "title",
        "page": page,
        "pageSize": page_size,
    }
    # 空数组会被上游判成参数错误，无筛选时整个字段不传
    if platforms:
        params["platforms"] = platforms

    envelope = {"path": UPSTREAM_PATH, "method": "POST", "params": params}
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://www.idatariver.com",
        "referer": "https://www.idatariver.com/",
        "x-idr-locale": "zh-cn",
        "x-idr-sign": settings.idr_sign,
        "x-idr-time": settings.idr_time,
    }

    try:
        resp = get_session().post(
            settings.idr_url, json=envelope, headers=headers,
            timeout=(settings.connect_timeout, settings.request_timeout),
        )
    except requests.exceptions.ConnectTimeout as exc:
        raise UpstreamError("连接上游超时（TLS 握手阶段）") from exc
    except requests.exceptions.Timeout as exc:
        raise UpstreamError("上游处理超时，未在限定时间内返回") from exc
    except requests.exceptions.RequestException as exc:
        raise UpstreamError(f"无法连接上游（{type(exc).__name__}）") from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise UpstreamError(f"上游返回非 JSON（HTTP {resp.status_code}）") from exc

    code = _to_int(data.get("code"), -1)
    if code == 1004:
        raise UpstreamError("IDR 签名已失效（1004），需在浏览器重新抓取 X-IDR-Sign 写入 .env", code=1004)
    if code == 1005:
        raise UpstreamError("上游判定 apikey 无效或余额不足（1005）", code=1005)
    if code != 0:
        msg = _clean(data.get("msg"))
        raise UpstreamError(f"上游失败（{code}）{msg}", code=code)

    return _normalize(data, page, page_size)
