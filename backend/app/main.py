"""盘迹 PanTrace —— 后端入口。

职责：收前端请求 → 走上游 → 结果洗干净还给前端。apikey 与签名只存在后端。
上游只有一条通道（见 providers.py）：idatariver 控制台 MAPI。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, feedback, group, own, pan, providers, resolver
from .sdk import registry
from .config import get_settings
from .providers import UpstreamError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pantrace")

settings = get_settings()

# 启动即自检：配置不对时提前吼一声，别等用户搜一次才发现
_KEY_PROBLEM = settings.api_key_problem
_SIGN_PROBLEM = settings.idr_sign_problem
if _KEY_PROBLEM:
    log.error("配置异常：%s —— 上游会把无效 key 报成 1005「余额不足」，注意区分", _KEY_PROBLEM)
else:
    log.info("apikey 自检通过（%s…，长度 %s）", settings.api_key[:12], len(settings.api_key))
if _SIGN_PROBLEM:
    log.error("配置异常：%s", _SIGN_PROBLEM)
else:
    log.info("签名已配置（%s…）", settings.idr_sign[:11])

app = FastAPI(title="盘迹 PanTrace API", version="0.4.0")

# ---- 埋点 ----
# 数据只落本地 SQLite，不出机器。写库失败不影响搜索。
ANALYTICS_ON = settings.analytics_enabled
if ANALYTICS_ON:
    try:
        analytics.init(
            settings.analytics_path,
            tz_offset=settings.analytics_tz_offset,
            retain_days=settings.analytics_retain_days,
        )
        log.info("埋点已启用：%s（保留 %s 天）", settings.analytics_path, settings.analytics_retain_days)
    except Exception as exc:  # noqa: BLE001
        ANALYTICS_ON = False
        log.error("埋点初始化失败，已关闭：%s", exc)
else:
    log.info("埋点已关闭（ANALYTICS_ENABLED=false）")

# ---- 反馈 / 求资源 ----
# 和埋点共用同一个 SQLite 文件，但开关独立：埋点是统计，反馈是待办工单，
# 关掉统计不该把用户提交的入口一起关掉。
FEEDBACK_ON = settings.feedback_enabled
if FEEDBACK_ON:
    try:
        feedback.init(
            settings.analytics_path,
            tz_offset=settings.analytics_tz_offset,
            rate_limit=settings.feedback_rate_per_10min,
        )
        log.info("反馈工单已启用：%s（每 IP 10 分钟 %s 条）",
                 settings.analytics_path, settings.feedback_rate_per_10min)
    except Exception as exc:  # noqa: BLE001
        FEEDBACK_ON = False
        log.error("反馈工单初始化失败，已关闭：%s", exc)
else:
    log.info("反馈工单已关闭（FEEDBACK_ENABLED=false）")

# 前端构建产物目录。看板页面（Vue 构建的 admin.html）也从这里取。
DIST = (
    Path(settings.frontend_dist).expanduser()
    if settings.frontend_dist
    else Path(__file__).resolve().parents[2] / "frontend" / "dist"
)

# 看板页面：优先用 Vue 构建产物；构建缺失时回退到单文件旧版（admin.legacy.html）
ADMIN_DIST_HTML = DIST / "admin.html"
ADMIN_HTML = Path(__file__).resolve().parent / "admin.legacy.html"

# ---- 自有精选链接 ----
# 站方维护的资源库：搜索命中时置顶展示"本站精选"卡，未命中词落日志。
# 数据和埋点、反馈共用同一个 SQLite，开关独立。
OWN_ON = settings.own_enabled
if OWN_ON:
    try:
        own.init(settings.analytics_path)
        log.info("自有精选链接已启用：%s", settings.analytics_path)
    except Exception as exc:  # noqa: BLE001
        OWN_ON = False
        log.error("自有精选链接初始化失败，已关闭：%s", exc)
else:
    log.info("自有精选链接已关闭（OWN_ENABLED=false）")

# ---- 点击时转存（链接不外露）----
# 用户点开时不给他上游原始链接，而是当场用我们的账号转存一份、建我们名下的分享。
# 关掉这个开关就退回老行为（直接给原链接），不存在"中间态"。
def _start_maintenance() -> None:
    """每 6 小时回收一次冷映射。

    不用 APScheduler：一个守护线程就够。多进程部署时每个进程都会跑一遍，
    但 cleanup 是幂等的（删文件 + 删行，重复执行只是空转），可以接受。
    """
    def _loop() -> None:
        while True:
            time.sleep(6 * 3600)
            try:
                result = pan.cleanup(resolver.session)
                if result["rows"] or result["files"]:
                    log.info(
                        "空间回收：清 %s 行 / %s 个文件（冷映射阈值 %s 天）",
                        result["rows"], result["files"], result["coldDays"],
                    )
                if result["errors"]:
                    log.warning("空间回收有 %s 条失败：%s", len(result["errors"]), result["errors"][:3])
            except Exception:  # noqa: BLE001
                log.exception("空间回收失败，6 小时后再试")

    threading.Thread(target=_loop, name="pan-maintenance", daemon=True).start()


MIRROR_ON = settings.mirror_enabled
if MIRROR_ON:
    try:
        resolver.init(
            settings.analytics_path,
            enabled=True,
            secret=settings.mirror_secret or f"{settings.api_key}|{settings.idr_sign}",
            fail_ttl=settings.mirror_fail_ttl,
        )
        pan.init(
            settings.analytics_path,
            timeout=settings.mirror_timeout,
            max_attempts=settings.mirror_max_attempts,
            cold_days=settings.mirror_cold_days,
            max_gb=settings.mirror_max_gb,
            min_interval=settings.mirror_min_interval,
        )
        # 老部署靠 MIRROR_QUARK_COOKIE 一个变量活着。表里没有夸克账号时
        # 把它导进去，避免升级后"转存突然全挂"——这是唯一的兼容路径，别删
        if settings.mirror_quark_cookie and not pan.list_accounts("quark"):
            pan.add_account(
                "quark",
                name="默认夸克（从旧配置迁移）",
                cookie=settings.mirror_quark_cookie,
            )
            log.info("已把 MIRROR_QUARK_COOKIE 迁移成账号记录，后续在后台维护")
        accounts = pan.summary()
        if accounts["enabled"]:
            log.info(
                "点击转存已启用：%s 个账号（%s）",
                accounts["enabled"],
                "、".join(f"{k}×{v}" for k, v in accounts["byType"].items()),
            )
        else:
            log.warning(
                "点击转存已启用，但没有任何网盘账号——所有点击会回退到上游原始链接。"
                "在 /admin 的「网盘账号」里加一个才会真正生效。"
            )
        _start_maintenance()
    except Exception as exc:  # noqa: BLE001
        MIRROR_ON = False
        log.error("点击转存初始化失败，已关闭：%s", exc)
else:
    log.info("点击转存已关闭（MIRROR_ENABLED=false），搜索结果直接给上游链接")

# ---- 群入口（二维码弹窗）----
# 图片落 backend/data/uploads/，前端通过 /uploads/<文件名> 访问。
# 挂载点必须在下面那个 "/" 静态目录之前注册，否则会被前端路由吃掉。
UPLOADS_DIR = Path(settings.analytics_path).parent / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

GROUP_ON = settings.group_enabled
if GROUP_ON:
    try:
        group.init(settings.analytics_path)
        log.info("群入口已启用：%s（当前 %s 个群）",
                 settings.analytics_path, len(group.store().load().get("groups") or []))
    except Exception as exc:  # noqa: BLE001
        GROUP_ON = False
        log.error("群入口初始化失败，已关闭：%s", exc)
else:
    log.info("群入口已关闭（GROUP_ENABLED=false）")


def client_ip(request: Request) -> str:
    """取真实来源 IP。

    Nginx 会重写 X-Real-IP（客户端伪造不进来），所以优先用它；
    X-Forwarded-For 取最右一项——那是我们自己的代理追加的，最左可以被客户端随便写。
    """
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else ""


def _visitor_of(request: Request) -> tuple[str, str, bool]:
    """前端带来的匿名访客号；没带就按 IP+UA 派生一个，保证 UV 不会全是 0。"""
    visitor = (request.headers.get("x-visitor-id") or "").strip()
    session = (request.headers.get("x-session-id") or "").strip()
    is_new = (request.headers.get("x-visitor-new") or "") == "1"
    if not visitor:
        seed = f"{client_ip(request)}|{(request.headers.get('user-agent') or '')[:200]}"
        visitor = "srv_" + hashlib.sha1(seed.encode()).hexdigest()[:16]
        is_new = False
    return visitor[:64], session[:64], is_new


# 单 IP 上报限流：埋点接口是公开的，不设上限别人能拿它把磁盘写满
_track_hits: dict[str, tuple[float, int]] = {}


def _track_allowed(ip: str) -> bool:
    now = time.time()
    window, count = _track_hits.get(ip, (now, 0))
    if now - window > 60:
        window, count = now, 0
    count += 1
    _track_hits[ip] = (window, count)
    if len(_track_hits) > 4096:  # 防止字典被海量 IP 撑爆
        for key, (started, _) in list(_track_hits.items()):
            if now - started > 60:
                _track_hits.pop(key, None)
    return count <= settings.analytics_rate_per_minute


class TrackEvent(BaseModel):
    kind: str
    visitor: str = ""
    session: str = ""
    path: str = ""
    referer: str = ""
    query: str = ""
    platform: str = ""
    target: str = ""
    durationMs: int = 0
    ok: bool = True
    resultCount: int = -1
    isNew: bool = False
    source: str = ""
    meta: dict = Field(default_factory=dict)


class TrackPayload(BaseModel):
    events: list[TrackEvent] = Field(default_factory=list)


@app.post("/api/track", include_in_schema=False)
async def track(payload: TrackPayload, request: Request, background: BackgroundTasks) -> Response:
    """前端事件上报。永远返回 204，不让埋点把页面搞出红字。

    写库走 BackgroundTasks：SQLite 写虽然快，但埋点接口不该占着事件循环，
    搜索请求的响应时间比统计数字金贵。
    """
    if not ANALYTICS_ON or not payload.events:
        return Response(status_code=204)

    ip = client_ip(request)
    if not _track_allowed(ip):
        return Response(status_code=204)

    ua = request.headers.get("user-agent", "")
    referer_header = request.headers.get("referer", "")
    visitor, session, is_new = _visitor_of(request)
    for event in payload.events[:20]:  # 单次最多 20 条，防止一次塞爆
        background.add_task(
            analytics.record,
            event.kind,
            visitor=event.visitor[:64] or visitor,
            session=event.session[:64] or session,
            ip=ip,
            ua=ua,
            referer=event.referer or referer_header,
            path=event.path,
            query=event.query,
            platform=event.platform,
            target=event.target,
            ok=event.ok,
            duration_ms=event.durationMs,
            result_count=event.resultCount,
            is_new=event.isNew or is_new,
            source=event.source,
            meta=event.meta,
        )
    return Response(status_code=204)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- 缓存策略 ----
# 静态文件由本进程托管（见文件末尾的 StaticFiles 挂载），所以头加在这一层，
# 不依赖 nginx 的 location 匹配——nginx 只做反代，`location = /index.html`
# 对用户实际访问的 "/" 是无效的，写过反而容易让首页 404。
#
# 两条规则：
# - /assets/ 下的文件名带内容 hash，内容永不变，长缓存 + immutable
# - 首页 HTML 每次回源校验（no-cache 走 ETag，没变就 304，不费流量）。
#   这里必须禁掉启发式缓存，否则发版后老访客会继续跑旧 bundle，
#   表现就是"新功能上不了线"——群入口弹窗不出现就是这么踩的。
_LONG_CACHE_PREFIX = "/assets/"
_REVALIDATE_PATHS = ("/", "/index.html")


@app.middleware("http")
async def cache_control(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith(_LONG_CACHE_PREFIX):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path in _REVALIDATE_PATHS:
        response.headers["Cache-Control"] = "no-cache"
    return response


def _apply_hidden(body: dict) -> dict:
    """把被多个独立访客报"失效"的结果从返回里摘掉。

    注意：过滤只发生在出口，缓存里存的仍是上游原始结果，
    否则隐藏状态要等缓存过期才生效（最长 5 分钟）。
    total 保持上游原值——本地过滤和上游分页本来就对不齐，
    为了数字好看去改 total 反而会让翻页逻辑出现空洞。
    """
    if not FEEDBACK_ON:
        return body
    hidden = feedback.hidden_keys()
    urls, titles = hidden["urls"], hidden["titles"]
    if not urls and not titles:
        return body
    items = body.get("items") or []
    kept = [
        it for it in items
        if it.get("shareUrl") not in urls and it.get("title") not in titles
    ]
    if len(kept) == len(items):
        return body
    out = dict(body)
    out["items"] = kept
    out["hidden"] = len(items) - len(kept)
    return out


def _attach_own(body: dict, own_items: list[dict]) -> dict:
    """自有精选卡插到结果集最顶部，不参与任何排序。

    命中即置顶：不存在"匹配上了但被上游结果淹没"的情况。
    上游结果照常列在下面——自有库覆盖率撑不起全站流量，
    砍掉上游等于把没命中的搜索词直接送走。
    """
    if not own_items:
        return body
    body["items"] = own_items + list(body.get("items") or [])
    body["ownCount"] = len(own_items)
    return body


def _public_body(body: dict) -> dict:
    """把上游真实链接换成签名跳转地址，再交给前端。

    这是"链接不外露"的唯一出口，只能在响应时做，**不能写进缓存**：
    缓存里留着原始链接，_apply_hidden 才有东西可比，改密钥/关开关也能立刻生效。

    自有精选卡不动——那条链接本来就是我们自己的。
    提取码一并抹掉：落地页会带我们分享的提取码，卡片上留一个上游的码
    只会让人复制了发现不对。

    `shareCode` 必须一起清：它就是网盘分享 id（实测夸克是 /s/ 后面那串），
    留着等于把真实链接换个字段发出去，前面做的全都白搭。
    """
    if not MIRROR_ON:
        return body
    items = []
    for raw in body.get("items") or []:
        item = dict(raw)
        url = item.get("shareUrl") or ""
        if url and not item.get("isOwn"):
            item["shareUrl"] = "/go/" + resolver.make_token(
                url, item.get("sharePwd") or "", item.get("platform") or "",
                item.get("title") or "",
            )
            item["sharePwd"] = ""
            item["shareCode"] = ""
        items.append(item)
    out = dict(body)
    out["items"] = items
    return out

# 界面用的平台列表。只放上游实测确认过的 key：
# 传一个上游不认识的 key 会让整次搜索报 1001，所以宁缺勿滥。
PLATFORMS = [
    {"key": "quark", "name": "夸克网盘", "color": "#2B6CF6"},
    {"key": "baidu", "name": "百度网盘", "color": "#3A6BF5"},
    {"key": "ali", "name": "阿里云盘", "color": "#7C4DFF"},
    {"key": "uc", "name": "UC网盘", "color": "#FF7A00"},
    {"key": "xunlei", "name": "迅雷网盘", "color": "#00A3FF"},
    {"key": "115", "name": "115网盘", "color": "#00A870"},
    {"key": "123", "name": "123云盘", "color": "#00B4D8"},
    {"key": "tianyi", "name": "天翼云盘", "color": "#1F7AFF"},
    {"key": "yidong", "name": "移动云盘", "color": "#00A5A8"},
    {"key": "wo", "name": "联通云盘", "color": "#E4002B"},
    {"key": "guangya", "name": "光鸭云盘", "color": "#F5A524"},
    {"key": "lanzou", "name": "蓝奏云", "color": "#4C9AFF"},
    {"key": "weiyun", "name": "腾讯微云", "color": "#12B7F5"},
    {"key": "ctfile", "name": "城通网盘", "color": "#8C6D31"},
]
PLATFORM_NAME = {p["key"]: p["name"] for p in PLATFORMS}
VALID_PLATFORM_KEYS = set(PLATFORM_NAME)
# 自有卡的 platformName 展示用这张表；模块导入时机早于平台表定义，
# 所以在这里注入（own.set_platforms 只是赋值，开销为零）
if OWN_ON:
    own.set_platforms(PLATFORM_NAME)

# ---- 缓存 ----
_cache: dict[str, tuple[float, dict]] = {}
# 失败也要短缓存。上游超时时请求会占着串行锁几十秒，用户一重试就把所有人都堵住；
# 记住失败状态 20 秒，直接快速返回，比让人干等更合理。
_fail_cache: dict[str, tuple[float, str]] = {}
FAIL_TTL = 20
CACHE_TTL = settings.cache_ttl

# 上游调用必须串行且留间隔：实测紧接着连发第二个请求，上游会挂住到超时（最长 120 秒）。
# 这不是优化，是可用性底线。
MIN_INTERVAL = 2.0
_last_call_at = 0.0
# 最近一次失败原因，只在 /api/health 暴露，便于远程判断是不是签名过期
_last_error = ""


@app.get("/api/platforms")
async def platforms():
    return {"code": 0, "platforms": PLATFORMS}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "channel": "idr",
        "apiKeyOk": not _KEY_PROBLEM,
        "idrSignConfigured": not _SIGN_PROBLEM,
        "signProblem": _SIGN_PROBLEM,
        "lastError": _last_error,
        "cached": len(_cache),
        "failed": len(_fail_cache),
        "analytics": ANALYTICS_ON,
        "feedback": FEEDBACK_ON,
        "mirror": MIRROR_ON,
        "mirrorAccount": bool(settings.mirror_quark_cookie),
    }


@app.get("/api/search")
async def search(
    request: Request,
    background: BackgroundTasks,
    q: str = Query(..., min_length=1, max_length=100, description="搜索关键词"),
    platforms: str = Query("", description="平台 key，逗号分隔，留空为全部"),
    page: int = Query(1, ge=1, le=100),
    page_size: int = Query(20, ge=1, le=settings.max_page_size),
    search_in: str = Query("title", description="title 或 all"),
):
    global _last_call_at, _last_error

    query = q.strip()
    requested = [p.strip() for p in platforms.split(",") if p.strip()]
    # 过滤掉上游不认识的值：传错一个 key 会让整次搜索失败
    platform_list = [p for p in requested if p in VALID_PLATFORM_KEYS]
    dropped = [p for p in requested if p not in VALID_PLATFORM_KEYS]
    if dropped:
        log.warning("忽略未知平台 key: %s", ",".join(dropped))

    scope = search_in if search_in in ("title", "all") else "title"
    cache_key = f"{query}|{','.join(platform_list)}|{page}|{page_size}|{scope}"
    now = time.time()

    # 埋点参数：真实 IP / UA / 匿名访客号，一次算好，几个出口复用
    visitor, session, is_new = _visitor_of(request)
    track_kw = {
        "query": query,
        "platform": ",".join(platform_list),
        "page": page,
        "visitor": visitor,
        "session": session,
        "ip": client_ip(request),
        "ua": request.headers.get("user-agent", ""),
        "referer": request.headers.get("referer", ""),
        "is_new": is_new,
    }

    # 自有精选卡：命中即在顶部展示。匹配在响应时做、不进缓存——
    # 改链接配置/补别名立刻生效，不用等缓存过期。
    # 第一页没接住的词落未命中日志，翻页不记（同一个词重复计数没有意义）。
    own_items = own.match(query, platform_list or None) if OWN_ON else []
    if OWN_ON and not own_items and page == 1:
        background.add_task(own.record_miss, query)

    cached = _cache.get(cache_key)
    if cached and now - cached[0] < CACHE_TTL:
        body = dict(cached[1])
        body["cached"] = True
        if ANALYTICS_ON:
            background.add_task(
                analytics.record_search,
                **track_kw,
                ok=True,
                duration_ms=0,
                result_count=len(body.get("items") or []),
                cached=True,
            )
        return _public_body(_apply_hidden(_attach_own(body, own_items)))

    failed = _fail_cache.get(cache_key)
    if failed and now - failed[0] < FAIL_TTL:
        wait = int(FAIL_TTL - (now - failed[0])) + 1
        if ANALYTICS_ON:
            background.add_task(
                analytics.record_search,
                **track_kw,
                ok=False,
                duration_ms=0,
                result_count=0,
                cached=True,
                error=failed[1],
            )
        return JSONResponse(
            status_code=504,
            content={"code": 504, "msg": f"{failed[1]}（{wait} 秒后可重试）"},
        )

    gap = MIN_INTERVAL - (time.monotonic() - _last_call_at)
    if gap > 0:
        await asyncio.sleep(gap)

    started = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            providers.search, query, platform_list, page, page_size, scope
        )
    except UpstreamError as exc:
        _last_call_at = time.monotonic()
        _last_error = str(exc)
        log.warning("upstream failed q=%s: %s", query, exc)
        msg = str(exc)
        _fail_cache[cache_key] = (time.time(), msg)
        if len(_fail_cache) > 256:
            for k, _ in sorted(_fail_cache.items(), key=lambda kv: kv[1][0])[:64]:
                _fail_cache.pop(k, None)
        if ANALYTICS_ON:
            background.add_task(
                analytics.record_search,
                **track_kw,
                ok=False,
                duration_ms=int((time.perf_counter() - started) * 1000),
                result_count=0,
                cached=False,
                error=msg,
            )
        return JSONResponse(status_code=504, content={"code": 504, "msg": msg})

    _last_call_at = time.monotonic()
    _last_error = ""
    upstream_ms = int((time.perf_counter() - started) * 1000)

    body = {
        "code": 0,
        "msg": "",
        "query": query,
        "items": result["items"],
        "pagination": result["pagination"],
        "credits": result["_meta"]["credits"],
        "tookMs": upstream_ms,
        "cached": False,
    }

    _fail_cache.pop(cache_key, None)
    _cache[cache_key] = (time.time(), body)
    if len(_cache) > settings.cache_max_entries:
        oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[: len(_cache) // 4]
        for k, _ in oldest:
            _cache.pop(k, None)

    if ANALYTICS_ON:
        background.add_task(
            analytics.record_search,
            **track_kw,
            ok=True,
            duration_ms=upstream_ms,
            result_count=len(body["items"]),
            cached=False,
        )

    log.info(
        "search q=%s page=%s items=%s %sms",
        query, page, len(body["items"]), upstream_ms,
    )
    # dict(body)：body 已进缓存，自有卡只在响应时挂，不能污染缓存本体
    return _public_body(_apply_hidden(_attach_own(dict(body), own_items)))


# ---- 点击跳转：/go/<token> ----
#
# 上游真实链接不再出现在搜索结果里，前端只拿到这里的签名地址。
# 这一步是整套机制的关键：链接一旦下发到浏览器，改什么逻辑都晚了——
# 用户可以直接复制走。签名 token 只解出源链接，解不出我们的账号信息。
#
# 为什么不在 /go 里直接 302：转存要 1–3 秒，直接 302 会把这段时间变成
# 新标签页的白屏，用户以为打不开就关掉了。给一张"正在准备"的过渡页，
# 期间还能把失败变成"返回重试/反馈"的动作。

_GO_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>正在准备资源 · 盘迹</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;
  background:#f7f8fa;color:#1b2430;
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.box{width:min(420px,calc(100vw - 40px));padding:34px 30px;background:#fff;
  border:1px solid #e6e9ef;border-radius:14px;text-align:center;
  box-shadow:0 10px 30px -18px rgba(20,30,45,.35)}
.brand{font:600 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.14em;
  text-transform:uppercase;color:#00a99d;margin-bottom:22px}
.ring{width:30px;height:30px;margin:0 auto 18px;border-radius:50%;
  border:2.5px solid #d9ede9;border-top-color:#00a99d;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
h1{margin:0 0 8px;font-size:17px;font-weight:600;letter-spacing:-.01em}
p{margin:0;color:#7b8794;font-size:13px}
.err{color:#b4462f;margin-top:14px;display:none}
.acts{margin-top:20px;display:none;gap:10px;justify-content:center}
a.btn{display:inline-flex;align-items:center;height:34px;padding:0 15px;border-radius:8px;
  font-size:13px;text-decoration:none;color:#1b2430;border:1px solid #dfe3e9}
a.btn.primary{background:#1b2430;color:#fff;border-color:#1b2430}
</style></head>
<body>
<div class="box">
  <div class="brand">PanTrace</div>
  <div class="ring" id="ring"></div>
  <h1 id="title">正在准备资源</h1>
  <p id="hint">首次打开需要转存一份，通常 1–3 秒</p>
  <p class="err" id="err"></p>
  <div class="acts" id="acts">
    <a class="btn" href="/">返回搜索</a>
    <a class="btn primary" id="fb" href="/">提交反馈</a>
  </div>
</div>
<script>
var TOKEN = __TOKEN__;
var slow = setTimeout(function () {
  document.getElementById('hint').textContent = '资源较大，转存稍慢，请再等一会儿…';
}, 4000);
function fail(msg) {
  clearTimeout(slow);
  document.getElementById('ring').style.display = 'none';
  document.getElementById('title').textContent = '资源暂时打不开';
  document.getElementById('hint').textContent = msg || '这条资源可能已失效';
  var e = document.getElementById('err');
  e.textContent = '换一个关键词，或者告诉我们你要找什么。';
  e.style.display = 'block';
  document.getElementById('acts').style.display = 'flex';
  document.getElementById('fb').href = '/?fb=' + encodeURIComponent(location.pathname);
}
fetch('/api/resolve', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  cache: 'no-store',
  body: JSON.stringify({ t: TOKEN })
}).then(function (r) { return r.json() }).then(function (j) {
  if (j && j.url) { clearTimeout(slow); location.replace(j.url); return }
  fail(j && j.msg);
}).catch(function () { fail('网络中断，请返回重试'); });
</script>
</body></html>"""


@app.get("/go/{token}", include_in_schema=False)
async def go_page(token: str):
    """过渡页。真正的解析在 /api/resolve，页面只负责显示状态并跳转。"""
    if token.lower().startswith("favicon"):
        return Response(status_code=404)
    if not MIRROR_ON:
        # 关掉开关后旧链接不该 404：直接送回首页，别让用户撞墙
        return RedirectResponse("/", status_code=302)
    html = _GO_HTML.replace("__TOKEN__", json.dumps(token[:900]))
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


class ResolveIn(BaseModel):
    t: str = ""


# 单 IP 打开频率。正常用户一分钟点不了十次；脚本扫这个接口是在试我们的账号额度。
_resolve_hits: dict[str, tuple[float, int]] = {}
RESOLVE_PER_10MIN = 60


def _resolve_allowed(ip: str) -> bool:
    now = time.time()
    window, count = _resolve_hits.get(ip, (now, 0))
    if now - window > 600:
        window, count = now, 0
    count += 1
    _resolve_hits[ip] = (window, count)
    if len(_resolve_hits) > 4096:
        for key, (started, _) in list(_resolve_hits.items()):
            if now - started > 600:
                _resolve_hits.pop(key, None)
    return count <= RESOLVE_PER_10MIN


@app.post("/api/resolve", include_in_schema=False)
async def api_resolve(
    payload: ResolveIn, request: Request, background: BackgroundTasks
) -> JSONResponse:
    """把签名 token 换成可跳转的地址。

    成功 → 我们的分享链接；失败 → 按配置回退到上游原链接（带提取码）。
    这里永远返回 200 + url 字段，除非 token 本身是伪造的——
    过渡页只认 url，错误码会让它把"已失效"和"转存失败"混成一句网络错误。
    """
    if not MIRROR_ON:
        return JSONResponse(status_code=404, content={"code": 404, "msg": "功能未启用"})
    if not _resolve_allowed(client_ip(request)):
        return JSONResponse(status_code=429, content={"code": 429, "msg": "打开太频繁，过几分钟再试"})

    info = resolver.read_token(payload.t)
    if not info:
        return JSONResponse(
            status_code=400, content={"code": 400, "msg": "链接已失效，回到首页重新搜索即可"}
        )

    started = time.perf_counter()
    result = await asyncio.to_thread(
        resolver.mirror, info["url"], info["pwd"], info["platform"], info["title"]
    )
    took = int((time.perf_counter() - started) * 1000)

    url, fallback, msg = result["url"], False, result.get("reason") or ""
    if not result["ok"]:
        if settings.mirror_fallback:
            # 回退要带提取码：用户拿到的是一半链接等于没给
            url, fallback = resolver.with_pwd(info["url"], info["pwd"]), True
            msg = msg or "资源准备失败"
        else:
            url = ""

    if ANALYTICS_ON:
        background.add_task(
            analytics.record,
            "mirror",
            visitor=_visitor_of(request)[0],
            ip=client_ip(request),
            ua=request.headers.get("user-agent", ""),
            platform=info["platform"],
            target=info["title"],
            ok=bool(url),
            duration_ms=took,
            source="fallback" if fallback else ("cached" if result.get("cached") else "fresh"),
            meta={"reason": msg} if msg else {},
        )
    if not result["ok"]:
        log.info("mirror miss platform=%s reason=%s fallback=%s", info["platform"], msg, fallback)

    return JSONResponse(
        content={
            "code": 0,
            "url": url,
            "fallback": fallback,
            "cached": bool(result.get("cached")),
            "msg": "" if url else (msg or "资源暂时不可用"),
        },
        headers={"Cache-Control": "no-store"},
    )


class FeedbackIn(BaseModel):
    """反馈 / 求资源的入参。全部是字符串，长度在后端统一裁。"""

    kind: str = ""
    message: str = ""
    query: str = ""
    platform: str = ""
    target: str = ""
    url: str = ""
    contact: str = ""
    page: str = ""


@app.post("/api/feedback")
async def submit_feedback(payload: FeedbackIn, request: Request) -> JSONResponse:
    """提交一条反馈或求资源。

    校验和去重都在 feedback 模块里，这里只负责取 IP/访客号、
    把 ValueError 转成 400。提交量很小，同步写库，不走后台任务——
    用户点完要立刻看到"已收到"。
    """
    if not FEEDBACK_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "反馈功能暂不可用"})

    ip = client_ip(request)
    visitor, _, _ = _visitor_of(request)
    # 前端拿到的是 /go/ 签名地址，不能直接入库：自动隐藏是按真实 URL 比对的，
    # 存成 /go/xxx 的话下次搜到同一条永远匹配不上，报失效就白报了。
    report_url = payload.url
    if report_url.startswith("/go/"):
        info = resolver.read_token(report_url[4:])
        report_url = info["url"] if info else ""
    try:
        result = feedback.submit(
            payload.kind,
            message=payload.message,
            query=payload.query,
            platform=payload.platform,
            target=payload.target,
            url=report_url,
            contact=payload.contact,
            visitor=visitor,
            ip=ip,
            ua=request.headers.get("user-agent", ""),
            page=payload.page,
        )
    except feedback.RateLimited:
        return JSONResponse(
            status_code=429,
            content={"code": 429, "msg": "提交有点频繁，过几分钟再试"},
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    except Exception:  # noqa: BLE001
        log.exception("反馈写入失败")
        return JSONResponse(
            status_code=500, content={"code": 500, "msg": "提交失败，请稍后重试"}
        )

    log.info("feedback kind=%s id=%s dup=%s", payload.kind, result["id"], result["duplicate"])
    return JSONResponse(
        content={
            "code": 0,
            "id": result["id"],
            "duplicate": result["duplicate"],
            "msg": "已收到",
        }
    )


def _admin_allowed(request: Request) -> bool:
    """看板鉴权：配了口令就核对口令；没配口令只允许本机直连。

    没配口令 + 走 Nginx 反代时，所有请求的来源 IP 都是真实用户 IP，
    所以远程一律进不来——这是刻意留的安全默认值，不是 bug。
    """
    if settings.admin_token:
        token = request.headers.get("x-admin-token") or request.query_params.get("token") or ""
        return hmac.compare_digest(token, settings.admin_token)
    return client_ip(request) in ("127.0.0.1", "::1")


@app.get("/api/admin/stats", include_in_schema=False)
async def admin_stats(request: Request, days: int = Query(7, ge=1, le=365)):
    if not ANALYTICS_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "埋点已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        return {"code": 0, "data": analytics.summary(days)}
    except Exception as exc:  # noqa: BLE001
        log.exception("统计查询失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"统计查询失败：{exc}"})


@app.get("/api/admin/feedback", include_in_schema=False)
async def admin_feedback(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    status: str = Query("", description="new / done，留空为全部"),
    limit: int = Query(100, ge=1, le=300),
):
    """看板的反馈列表。待办排前面，这是唯一需要人工介入的区块。"""
    if not FEEDBACK_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "反馈已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        return {"code": 0, "data": feedback.summary(days, status, limit)}
    except Exception as exc:  # noqa: BLE001
        log.exception("反馈查询失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"查询失败：{exc}"})


class FeedbackUpdate(BaseModel):
    status: str = ""
    note: str = ""


@app.post("/api/admin/feedback/{fid}", include_in_schema=False)
async def admin_feedback_update(fid: int, payload: FeedbackUpdate, request: Request):
    if not FEEDBACK_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "反馈已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    if payload.status not in ("new", "done"):
        return JSONResponse(status_code=400, content={"code": 400, "msg": "状态只能是 new 或 done"})
    try:
        ok = feedback.set_status(fid, payload.status, payload.note)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    if not ok:
        return JSONResponse(status_code=404, content={"code": 404, "msg": "没有这条反馈"})
    return {"code": 0, "msg": "已更新"}


@app.get("/api/admin/plan", include_in_schema=False)
async def admin_plan(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    page: int = Query(1, ge=1, le=10000),
    pageSize: int = Query(20, ge=1, le=100),
):
    """上架清单：按搜索人数排序的待上架词，已上架的自动剔除，支持分页。"""
    if not OWN_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "精选链接已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        return {"code": 0, "data": own.plan(days=days, page=page, page_size=pageSize)}
    except Exception as exc:  # noqa: BLE001
        log.exception("上架清单生成失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"生成失败：{exc}"})


@app.get("/api/admin/own", include_in_schema=False)
async def admin_own_list(request: Request, days: int = Query(30, ge=1, le=365)):
    """自有精选链接管理：全量链接 + 未命中词榜 + 小数字。

    未命中榜是"搜过但没接住"的纯待办——补了别名的词自动从榜单消失。
    """
    if not OWN_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "精选链接已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        return {
            "code": 0,
            "data": {
                "links": own.list_links(),
                "misses": own.misses(days=days),
                "stats": own.stats(),
            },
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("精选链接查询失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"查询失败：{exc}"})


class OwnLinkIn(BaseModel):
    """新增 / 更新一条自有链接。id 为空 = 新增。字段校验在 own.save 里做。"""

    id: int | None = None
    keyword: str = ""
    aliases: str = ""
    title: str = ""
    platform: str = ""
    shareUrl: str = ""
    sharePwd: str = ""
    fileCount: int = 0
    sizeBytes: int = 0
    note: str = ""
    enabled: bool = True


@app.post("/api/admin/own", include_in_schema=False)
async def admin_own_save(payload: OwnLinkIn, request: Request):
    if not OWN_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "精选链接已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        saved = own.save(payload.model_dump())
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.exception("精选链接保存失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"保存失败：{exc}"})
    log.info("own link saved id=%s kw=%s", saved["id"], payload.keyword)
    return {"code": 0, "data": saved, "msg": "已保存，下次搜索即生效"}


class ParseIn(BaseModel):
    """补录解析输入：一段分享文案 / 链接（可多行），外加清单里点进来的那个词。"""

    text: str = ""
    keyword: str = ""


class BulkIn(BaseModel):
    items: list[OwnLinkIn] = []


@app.post("/api/admin/own/parse", include_in_schema=False)
async def admin_own_parse(payload: ParseIn, request: Request):
    """把粘贴的分享文案解析成可入库的字段，不写库。

    纯本地正则 + 夸克公开接口补元信息；解析慢的是夸克那两次请求，
    所以丢线程池，别占事件循环。
    """
    if not OWN_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "精选链接已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    text = (payload.text or "")[:8000]
    try:
        items = await asyncio.to_thread(own.parse_share, text, payload.keyword or "")
    except Exception as exc:  # noqa: BLE001
        log.exception("分享文案解析失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"解析失败：{exc}"})
    return {"code": 0, "data": {"items": items}}


@app.post("/api/admin/own/bulk", include_in_schema=False)
async def admin_own_bulk(payload: BulkIn, request: Request):
    """批量补录：逐条保存，返回成功条数与失败原因，不整批回滚。"""
    if not OWN_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "精选链接已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    items = payload.items[:50]
    if not items:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "没有可保存的条目"})
    try:
        result = await asyncio.to_thread(own.save_many, [i.model_dump() for i in items])
    except Exception as exc:  # noqa: BLE001
        log.exception("批量补录失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"保存失败：{exc}"})
    ok, failed = len(result["saved"]), result["errors"]
    log.info("own bulk saved=%s failed=%s", ok, len(failed))
    msg = f"已保存 {ok} 条" + (f"，{len(failed)} 条失败" if failed else "，下次搜索即生效")
    return {"code": 0, "data": result, "msg": msg}


@app.delete("/api/admin/own/{lid}", include_in_schema=False)
async def admin_own_delete(lid: int, request: Request):
    if not OWN_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "精选链接已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    ok = own.delete(lid)
    if not ok:
        return JSONResponse(status_code=404, content={"code": 404, "msg": "没有这条链接"})
    return {"code": 0, "msg": "已删除"}


@app.get("/api/admin/mirror", include_in_schema=False)
async def admin_mirror(request: Request):
    """转存映射表：成功/失败/命中数 + 最近 30 条。

    排障主要看两点：fail 里是不是清一色"登录态已失效"（该换 cookie），
    或者清一色"超出容量阈值"（该调大阈值或换大容量账号）。
    """
    if not MIRROR_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "点击转存已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        return {"code": 0, "data": resolver.stats()}
    except Exception as exc:  # noqa: BLE001
        log.exception("转存映射查询失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"查询失败：{exc}"})


class MirrorTestIn(BaseModel):
    url: str = ""
    pwd: str = ""


# ---- 网盘账号管理 ----
#
# 账号从 .env 搬进了库，后台直接维护。MIRROR_QUARK_COOKIE 只在首次启动时
# 迁移一次，之后以表为准——两边同时写会互相覆盖，别再加回去。

class AccountIn(BaseModel):
    type: str = ""
    name: str = ""
    cookie: str = ""
    access_token: str = ""
    refresh_token: str = ""
    temp_dir: str = ""
    expires_at: int = 0
    status: int = 1


@app.get("/api/admin/accounts", include_in_schema=False)
async def admin_accounts(request: Request):
    """账号概览。cookie / token 一律不下发，只回 hasCookie / hasToken。"""
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        data = pan.summary()
        data["supported"] = registry.available()
        return {"code": 0, "data": data}
    except Exception as exc:  # noqa: BLE001
        log.exception("账号列表查询失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"查询失败：{exc}"})


@app.post("/api/admin/accounts", include_in_schema=False)
async def admin_account_add(payload: AccountIn, request: Request):
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    platform = (payload.type or "").strip().lower()
    if not registry.supported(platform):
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": f"不支持的网盘类型：{platform or '空'}"},
        )
    if not (payload.cookie.strip() or payload.refresh_token.strip() or payload.access_token.strip()):
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": "至少填一项凭据（cookie / refresh_token / access_token）"},
        )
    aid = pan.add_account(
        platform,
        name=payload.name.strip(),
        cookie=payload.cookie.strip(),
        access_token=payload.access_token.strip(),
        refresh_token=payload.refresh_token.strip(),
        expires_at=payload.expires_at,
        temp_dir=payload.temp_dir.strip(),
    )
    return {"code": 0, "id": aid, "msg": "已添加"}


@app.patch("/api/admin/accounts/{aid}", include_in_schema=False)
async def admin_account_update(aid: int, payload: AccountIn, request: Request):
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    fields = {
        key: value
        for key, value in {
            "name": payload.name.strip(),
            "cookie": payload.cookie.strip(),
            "access_token": payload.access_token.strip(),
            "refresh_token": payload.refresh_token.strip(),
            "temp_dir": payload.temp_dir.strip(),
            "expires_at": payload.expires_at,
            "status": payload.status,
        }.items()
        if value not in ("", 0) or key in ("status",)
    }
    if not fields:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "没有要改的字段"})
    pan.update_account(aid, **fields)
    return {"code": 0, "msg": "已保存"}


@app.delete("/api/admin/accounts/{aid}", include_in_schema=False)
async def admin_account_delete(aid: int, request: Request):
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    pan.delete_account(aid)
    return {"code": 0, "msg": "已删除"}


@app.post("/api/admin/accounts/{aid}/test", include_in_schema=False)
async def admin_account_test(aid: int, request: Request):
    """单账号体检。失败会累加 fail_count，影响后续被选中的顺序。"""
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    result = await asyncio.to_thread(pan.probe, aid)
    return {"code": 0, "data": result}


@app.post("/api/admin/maintain/cleanup", include_in_schema=False)
async def admin_cleanup(request: Request, cold_days: int = 0):
    """回收冷映射占用的空间。

    只删两类：失败超过 7 天的行、零点击且超过 cold_days 天的映射（连文件）。
    有热度的映射一律不动——删了就是把用户手上的链接打死。
    """
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        result = await asyncio.to_thread(pan.cleanup, resolver.session, cold_days=cold_days)
        return {"code": 0, "data": result}
    except Exception as exc:  # noqa: BLE001
        log.exception("空间回收失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"回收失败：{exc}"})


@app.post("/api/admin/mirror/test", include_in_schema=False)
async def admin_mirror_test(payload: MirrorTestIn, request: Request):
    """手工验一条链接，不经过前端。填完 cookie 先用它验证，别等真人点击。"""
    if not MIRROR_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "点击转存已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    url = (payload.url or "").strip()[:500]
    if not url.startswith("http"):
        return JSONResponse(status_code=400, content={"code": 400, "msg": "给一条完整分享链接"})
    result = await asyncio.to_thread(resolver.mirror, url, payload.pwd or "", "", "", True)
    return {"code": 0, "data": result}


@app.get("/api/group", include_in_schema=False)
async def group_config():
    """前端进站拉群入口配置。没有敏感字段，结构在 group.py 里定义。"""
    if not GROUP_ON:
        return {"code": 0, "enabled": False, "groups": []}
    return {"code": 0, **group.store().public()}


def _check_image(data: bytes) -> str | None:
    """按魔数认格式，返回扩展名；不认识的直接拒。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


@app.post("/api/admin/group/upload", include_in_schema=False)
async def admin_group_upload(request: Request):
    """上传二维码图片。存 backend/data/uploads/，文件名带时间戳，前端拿到就能用。

    收裸字节流而不是 multipart：省一个 python-multipart 依赖，
    前端直接把文件本体 POST 上来，Content-Type 就是图片自己的。
    """
    if not GROUP_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "群入口已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    data = await request.body()
    if not data:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "没有收到文件"})
    if len(data) > 2 * 1024 * 1024:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "图片超过 2MB"})
    ext = _check_image(data)
    if not ext:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "只支持 jpg / png / gif / webp"})
    name = f"qr-{int(time.time())}-{secrets.token_hex(4)}.{ext}"
    (UPLOADS_DIR / name).write_bytes(data)
    log.info("group qr uploaded: %s (%sKB)", name, len(data) // 1024)
    return {"code": 0, "url": f"/uploads/{name}"}


class GroupConfigIn(BaseModel):
    """整包保存。前端把整个表单序列化后一次提交，不做增量合并。"""

    model_config = {"extra": "allow"}  # 原样透传，字段校验在 group._validate 里做
    enabled: bool = True
    title: str = ""
    desc: str = ""
    tip: str = ""
    popup: dict = Field(default_factory=dict)
    groups: list[dict] = Field(default_factory=list)


@app.get("/api/admin/group", include_in_schema=False)
async def admin_group_get(request: Request):
    if not GROUP_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "群入口已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    return {"code": 0, "data": group.store().load()}


@app.post("/api/admin/group", include_in_schema=False)
async def admin_group_save(payload: GroupConfigIn, request: Request):
    if not GROUP_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "群入口已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        saved = group.store().save(payload.model_dump())
    except Exception as exc:  # noqa: BLE001
        log.exception("群配置保存失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"保存失败：{exc}"})
    return {"code": 0, "data": saved, "msg": "已保存"}


@app.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    """看板页面。数据本身在 /api/admin/stats 后面，页面公开也不泄露东西。"""
    # 没配口令 + 远程访问：当作不存在，不暴露这里有个后台
    if not settings.admin_token and not _admin_allowed(request):
        return Response(status_code=404)
    page = ADMIN_DIST_HTML if ADMIN_DIST_HTML.is_file() else ADMIN_HTML
    if not page.is_file():
        return JSONResponse(status_code=404, content={"code": 404, "msg": "看板页面缺失"})
    # 看板页面必须不缓存：否则改了代码你还在看旧页面（翻页控件"消失"就是这么来的）
    return FileResponse(page, headers={"Cache-Control": "no-store, must-revalidate"})


# 生产环境：把前端构建产物一起托管（frontend/dist 存在时生效）
# 如果前端交给 Nginx 直接托管，把 SERVE_FRONTEND 设为 false 即可
if settings.serve_frontend and DIST.is_dir():
    # 上传的二维码先挂，再挂前端兜底路由，顺序不能反
    app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="web")
    log.info("serving frontend from %s", DIST)
else:
    app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
    log.info("frontend static hosting disabled; API only")
