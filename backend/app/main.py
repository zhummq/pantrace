"""盘迹 PanTrace —— 后端入口。

职责：收前端请求 → 走上游 → 结果洗干净还给前端。apikey 与签名只存在后端。
上游只有一条通道（见 providers.py）：idatariver 控制台 MAPI。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, feedback, group, providers
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

ADMIN_HTML = Path(__file__).resolve().parent / "admin.html"

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
            meta=event.meta,
        )
    return Response(status_code=204)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        return _apply_hidden(body)

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
    return _apply_hidden(body)


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
    try:
        result = feedback.submit(
            payload.kind,
            message=payload.message,
            query=payload.query,
            platform=payload.platform,
            target=payload.target,
            url=payload.url,
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
    if not ADMIN_HTML.is_file():
        return JSONResponse(status_code=404, content={"code": 404, "msg": "admin.html 缺失"})
    return FileResponse(ADMIN_HTML)


# 生产环境：把前端构建产物一起托管（frontend/dist 存在时生效）
# 如果前端交给 Nginx 直接托管，把 SERVE_FRONTEND 设为 false 即可
DIST = (
    Path(settings.frontend_dist).expanduser()
    if settings.frontend_dist
    else Path(__file__).resolve().parents[2] / "frontend" / "dist"
)
if settings.serve_frontend and DIST.is_dir():
    # 上传的二维码先挂，再挂前端兜底路由，顺序不能反
    app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="web")
    log.info("serving frontend from %s", DIST)
else:
    app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
    log.info("frontend static hosting disabled; API only")
