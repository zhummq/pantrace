"""盘迹 PanTrace —— 后端入口。

职责：收前端请求 → 走插件层取数 → 结果洗干净还给前端。apikey 与签名只存在后端。
取数在 `app/search/`（pansou 插件机制），上游协议细节在 `providers.py`。
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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, check, feedback, group, guard, library_sync, pan, qq_intake, resolver, source_score, store
from .config import get_settings
from .search import engine as search_engine
from .search import library as library_layer
from .search.cache import SearchCache
from .search.pool import WorkerPool
from .search.registry import close_clients as close_source_clients, load_all as load_sources
from .search.singleflight import SingleFlight
from .search.observe import liveness
from .search.pool import stats as source_stats
from .search.tg import TG as TG_GATE, search_channel as tg_search_channel
from .search.tg import config_store as tg_channels
from .sdk import adfilter, registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pantrace")

settings = get_settings()

# TG 频道：库里改过就用库里的，没改过沿用 .env。必须在加载插件之前初始化，
# 否则插件第一次搜索读到的还是旧值（插件本身每次实时取，不会缓存这个列表）。
tg_channels.init(settings.analytics_path, default_channels=settings.tg_channel_list)

# 防刷：IP 滑动窗口 + 匿名票据。放在所有接口前面，后面 /api/ticket 和
# /api/search 都用它。密钥留空时跟转存签名一样从 apikey|签名派生。
guard.init(
    secret=settings.ticket_secret or f"{settings.api_key}|{settings.idr_sign}",
    ttl=settings.ticket_ttl,
    rate_limit=settings.rate_limit_per_minute,
    app_token=settings.app_token,
    app_rate_limit=settings.app_rate_limit_per_minute,
    app_global_limit=settings.app_global_per_minute,
)
log.info("防刷已启用：单 IP %s 次/分钟，票据 %s 秒", settings.rate_limit_per_minute, settings.ticket_ttl)
if settings.app_token:
    # 只报长度和前缀，令牌本身不进日志——日志会被人拷来拷去
    log.info(
        "小程序通道已开启：令牌 %s…（%s 位），单客户端 %s 次/分钟，总闸 %s 次/分钟",
        settings.app_token[:10],
        len(settings.app_token),
        settings.app_rate_limit_per_minute,
        settings.app_global_per_minute,
    )
else:
    log.info("小程序通道未配置（APP_TOKEN 为空）：云函数会走票据，出口 IP 漂移时会 403")

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

# ---- 搜索源（pansou 插件层）----
# 白名单加载：ENABLED_SOURCES 为空 = 一个源都不启用。插件自带 4~6 秒窗口和
# `complete` 语义缓存，这里不再套第二层超时。
_search_cache = SearchCache(settings.search_cache_path)
_sources = load_sources(
    settings.enabled_sources,
    cache=_search_cache,
    pool=WorkerPool(),
    proxy=settings.proxy_url or None,
)
if _sources:
    log.info("搜索源就绪：%s（L2 缓存 %s）", [p.name for p in _sources], settings.search_cache_path)
else:
    log.error("没有启用任何搜索源（ENABLED_SOURCES=%r），/api/search 会返回 504", settings.enabled_sources)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # TG 可达性由后台探测循环维护（pansou 的 `tgProbeLoop`）：搜索路径只读结论、
    # 绝不阻塞在探测上。只在 TG 源被启用时启动，否则白打一次 t.me。
    if any(p.name == "tg_searchers" for p in _sources):
        TG_GATE.start()
    # 文档订阅的每日增量放在这里而不是模块顶层：模块顶层是"被 import 就起线程"，
    # 任何脚本、任何测试只要 import 到 app.main 都会顺手拉起一条调度，还会真去
    # 连库。服务启动才起，语义才对，也便于测试隔离。
    if LIBRARY_ON and settings.library_daily_sync:
        library_sync.start_daily(settings.library_daily_hour)
    yield
    # 退出时关连接池，否则 uvicorn --reload 会留一堆 TIME_WAIT
    await close_source_clients()
    _search_cache.close()


app = FastAPI(title="盘迹 PanTrace API", version="0.4.0", lifespan=lifespan)

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

# ---- 资源库（本地索引） ----
# 影子回填：搜索已经拿到的结果顺手落库，不额外打上游、不改任何现有行为。
# 初始化失败就关闭，搜索照旧——这条链路是纯旁路，任何时候都不许拖垮主流程。
LIBRARY_ON = settings.library_enabled
if LIBRARY_ON:
    try:
        store.init(settings.library_path)
        log.info("资源库已启用：%s", settings.library_path)
    except Exception as exc:  # noqa: BLE001
        LIBRARY_ON = False
        log.error("资源库初始化失败，已关闭：%s", exc)
else:
    log.info("资源库已关闭（LIBRARY_ENABLED=false）")

# QQ 群采集的开关单独放。它写的是同一个库，但没有库就无处可写，
# 所以库关掉时它也跟着失效——不在这里联动的话，接口会返回"成功 0 条"，
# 看起来像在正常工作，实际一条都没进来。
QQ_INGEST_ON = settings.qq_ingest_enabled and LIBRARY_ON
if settings.qq_ingest_enabled and not LIBRARY_ON:
    log.warning("QQ 采集已随资源库一起关闭（LIBRARY_ENABLED=false）")

# ---- 来源贡献分 ----
# 和埋点同一个 SQLite 文件，但独立建表、独立计时起点：历史点击不参与排行榜。
# 埋点关掉时计分也关掉（点开来源的埋点本来就不上报），保持一个开关一个语义。
SOURCE_SCORE_ON = ANALYTICS_ON
if SOURCE_SCORE_ON:
    try:
        source_score.init(settings.analytics_path, tz_offset=settings.analytics_tz_offset)
    except Exception as exc:  # noqa: BLE001
        SOURCE_SCORE_ON = False
        log.error("来源计分初始化失败，已关闭：%s", exc)

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

# ---- 点击时转存（链接不外露）----
# 用户点开时不给他上游原始链接，而是当场用我们的账号转存一份、建我们名下的分享。
# 关掉这个开关就退回老行为（直接给原链接），不存在"中间态"。
def _start_maintenance() -> None:
    """回收循环：链接只活 30 分钟，到点删文件。

    竞品就是这套——缓存半小时，逼用户当场转存到自己的网盘。区别在于
    我们不做"25 分钟无脑清空"，而是按每条映射自己的窗口（`MIRROR_LINK_MINUTES`）
    回收，并在删除前留 5 分钟宽限，避免把正在转存的用户打断。

    不用 APScheduler：一个守护线程就够。多进程部署时每个进程都会跑一遍，
    但回收是幂等的（删文件 + 改状态，重复执行只是空转），可以接受。

    违规巡检也挂在这条线程上：它比回收更重（每个账号一次列表请求），
    不值得为它单开线程，更不能塞进点击链路。
    """
    violated_on = bool(settings.mirror_violated_auto)
    violated_every = max(300, int(settings.mirror_violated_minutes) * 60)
    # 启动后先等 90 秒：刚起来那会儿并发最多，别一上来就去刷网盘接口
    next_violated = time.time() + 90

    def _loop() -> None:
        # 下面要改写入睡时刻，不声明 nonlocal 会变成 _loop 的局部变量，
        # 第一次读就 UnboundLocalError（已踩过）
        nonlocal next_violated
        cold_tick = 0
        last_err = ""
        while True:
            try:
                result = pan.recycle_expired(resolver.session)
                if result["rows"] or result["files"]:
                    log.info(
                        "到期回收：%s 条链接 / %s 个文件（窗口 %s 分钟）",
                        result["rows"], result["files"], settings.mirror_link_minutes,
                    )
                # 账号被删/停用时会一直删不掉，同样的错别每 5 分钟刷一遍
                err = "；".join(result["errors"][:2])
                if err and err != last_err:
                    log.warning("到期回收有 %s 条失败：%s", len(result["errors"]), result["errors"][:3])
                last_err = err

                # 重转后剩下的旧副本：没有映射行指向它们，只能走这条队列
                trash = pan.recycle_trash(resolver.session)
                if trash["rows"]:
                    log.info("旧副本清理：%s 批 / %s 个文件", trash["rows"], trash["files"])
                if trash["errors"]:
                    log.warning("旧副本清理有 %s 条失败：%s", len(trash["errors"]), trash["errors"][:3])

                # 违规分享巡检：只撤"分享条目"，不碰文件（文件归上面两条管）
                if violated_on and time.time() >= next_violated:
                    next_violated = time.time() + violated_every
                    v = pan.check_violated(mirrors_session=resolver.session)
                    if v["scanned"] or v["errors"]:
                        log.info(
                            "违规分享巡检：%s 个账号 / 扫 %s 条 / 撤 %s 条 / 失败 %s 条 / "
                            "窗口内跳过 %s 条 / 跳过 %s 个平台",
                            v["accounts"], v["scanned"], v["revoked"], v["failed"],
                            v["protected"], v["skipped"],
                        )
                    for item in v["errors"][:3]:
                        log.warning("违规分享巡检失败：%s", item)
            except Exception:  # noqa: BLE001
                log.exception("到期回收失败，5 分钟后再试")

            # 冷映射兜底（长时间没人点、也没有 fids 的残余行），一小时一次就够
            cold_tick += 1
            if cold_tick % 12 == 0:
                try:
                    cold = pan.cleanup(resolver.session)
                    if cold["rows"] or cold["files"]:
                        log.info("空间回收：清 %s 行 / %s 个文件", cold["rows"], cold["files"])
                except Exception:  # noqa: BLE001
                    log.exception("空间回收失败，一小时后重试")

            time.sleep(300)

    threading.Thread(target=_loop, name="pan-maintenance", daemon=True).start()


MIRROR_ON = settings.mirror_enabled
if MIRROR_ON:
    try:
        resolver.init(
            settings.analytics_path,
            enabled=True,
            secret=settings.mirror_secret or f"{settings.api_key}|{settings.idr_sign}",
            fail_ttl=settings.mirror_fail_ttl,
            link_ttl=settings.mirror_link_minutes * 60,
        )
        pan.init(
            settings.analytics_path,
            timeout=settings.mirror_timeout,
            max_attempts=settings.mirror_max_attempts,
            cold_days=settings.mirror_cold_days,
            max_gb=settings.mirror_max_gb,
            min_interval=settings.mirror_min_interval,
            ad_filter=settings.mirror_ad_filter,
            ad_preskip=settings.mirror_ad_preskip,
            ad_keywords=settings.mirror_ad_keywords,
            ad_depth=settings.mirror_ad_depth,
            ad_max_mb=settings.mirror_ad_max_mb,
            share_hours=settings.mirror_share_hours,
            watermark_name=settings.mirror_watermark_name,
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
    except Exception as exc:  # noqa: BLE001
        MIRROR_ON = False
        log.error("点击转存初始化失败，已关闭：%s", exc)
else:
    log.info("点击转存已关闭（MIRROR_ENABLED=false），搜索结果直接给上游链接")

# ---- 链接存活检测 ----
# 搜索返回后异步探活（BackgroundTasks → 线程池），结果落 link_check 表，
# 下次渲染时读表。**不能挡在搜索链路上**：搜索本身已经要等上游 2~60 秒，
# 再加 20 条外部请求会把它变成不可用。打的是各网盘的匿名接口，不占转存账号。
CHECK_ON = settings.link_check_enabled
if CHECK_ON:
    try:
        check.init(
            settings.analytics_path,
            enabled=True,
            hide_bad=settings.link_check_hide_bad,
            workers=settings.link_check_workers,
            timeout=settings.link_check_timeout,
            per_page=settings.link_check_per_page,
        )
        log.info(
            "链接检测已启用：每页 ≤%s 条，%s 线程，失效%s隐藏",
            settings.link_check_per_page,
            settings.link_check_workers,
            "" if settings.link_check_hide_bad else "不",
        )
    except Exception as exc:  # noqa: BLE001
        CHECK_ON = False
        log.error("链接检测初始化失败，已关闭：%s", exc)
else:
    log.info("链接检测已关闭（LINK_CHECK_ENABLED=false）")

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

# 维护线程必须最后起。它一启动就去开连接做回收，和 group.init 抢同一个
# SQLite：WAL 初始化撞上并发写，会偶发 "attempt to write a readonly database"，
# 结果是群入口被静默关掉（实测两次启动命中一次）。所有建表/连接都做完再放它出来。
if MIRROR_ON:
    _start_maintenance()


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
    url: str = ""
    durationMs: int = 0
    ok: bool = True
    resultCount: int = -1
    isNew: bool = False
    source: str = ""
    # 这条结果来自哪个频道/插件（`tg:xxx` / `plugin:xxx`）。只在 click 事件里有值，
    # 和 `source`（转存缓存状态 cached/fresh/fallback）是两件事，别混用。
    sourceKey: str = ""
    meta: dict = Field(default_factory=dict)


def _real_url_of(raw: str) -> str:
    """/go/ 签名串解回真实网盘链接；本来就是 http(s) 的原样收下。

    为什么在这里解：浏览器上从来只有 /go/ token，真实链接不外露——但管理员
    要看"用户点开的到底是哪条网盘链接"，答案就在 token 里。验签不过的（伪造、
    换过密钥）一律丢掉，宁缺勿错。
    """
    url = (raw or "").strip()
    if not url:
        return ""
    if url.startswith("/go/"):
        info = resolver.read_token(url[4:])
        return info["url"] if info else ""
    return url if url.startswith(("http://", "https://")) else ""


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
        # 来源加分：只认 click + 合法来源Key，机器人不算。
        # 走 BackgroundTasks，和埋点一样不让写库挡住响应。
        if SOURCE_SCORE_ON and event.kind == "click" and event.sourceKey:
            background.add_task(
                source_score.record,
                event.sourceKey,
                visitor=event.visitor[:64] or visitor,
                ip=ip,
                bot=bool(ua and analytics.is_bot(ua)),
            )
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
            url=_real_url_of(event.url),
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


def _can_mirror(platform: str, size_bytes: int = 0) -> bool:
    """这条结果点开以后，有没有可能落到我们自己的链接上。

    三个条件缺一不可：有 adapter、该平台有启用账号、体积没超阈值（配了才查）。
    任一不满足，点击必然回退到上游原链接——那种情况下卡片上必须留着原来的
    提取码，否则用户拿到的是一条要密码的链接，而密码已经被我们抹掉了。
    """
    if not platform or not registry.supported(platform):
        return False
    # 0 = 不限体积。不加这个判断的话，max_gb=0 会让"任何有体积的资源"都
    # 判定成不能转，卡片就会保留上游提取码——用户拿去填我们自己的分享，必错
    limit = int(float(settings.mirror_max_gb) * 1024 ** 3)
    if limit and size_bytes and size_bytes > limit:
        return False
    try:
        return pan.has_account(platform)
    except Exception:  # noqa: BLE001
        # 账号库还没初始化时按"不能转"处理：留着码最多是冗余，抹掉码是事故
        return False


def _public_body(body: dict) -> dict:
    """把上游真实链接换成签名跳转地址，再交给前端。

    这是"链接不外露"的唯一出口，只能在响应时做，**不能写进缓存**：
    缓存里留着原始链接，_apply_hidden 才有东西可比，改密钥/关开关也能立刻生效。

    自有精选卡不动——那条链接本来就是我们自己的。

    提取码按平台分开处理：能转存的才抹（落地页会给我们自己那条分享的码），
    不能转存的必须留着。原来一刀切全抹，结果是天翼、移动云盘、微云这些
    没有 adapter 的平台，卡片上没码、回退链接又不认 URL 传码，用户手上
    彻底没有码——2026-11 发现的一批"只有链接没有码"就是这个。

    `shareCode` 一律清：它就是网盘分享 id（实测夸克是 /s/ 后面那串），
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
            item["shareCode"] = ""
            if _can_mirror(
                item.get("platform") or "", int(item.get("sizeBytes") or 0)
            ):
                item["sharePwd"] = ""
        items.append(item)
    out = dict(body)
    out["items"] = items
    return out


def _export_search(body: dict, background: BackgroundTasks, *, page: int) -> dict:
    """搜索响应的唯一出口：举报隐藏 → 失效过滤/标状态 → 换签名链接。

    顺序不能调换：
    - 隐藏和检测都按 `shareUrl` 比对，必须发生在 `_public_body` 把链接换成
      `/go/<token>` 之前；
    - 检测标注会**复制**条目再写 `checkState`，缓存里那份保持干净（同 `_apply_hidden`）。
    - 探活只在第一页做。深处页面几乎没人翻，检了纯浪费上游请求。
    """
    body = _apply_hidden(body)

    # 影子回填：位置不能挪，三个原因。
    # 1) 此刻 `shareUrl` 还是原始网盘链接，`_public_body` 一执行就换成 /go/<token>，
    #    那之后落库的全是解不开的签名串；
    # 2) `_apply_hidden` 已经剔掉被多人举报的条目，进库的都是干净的；
    # 3) **搭车（shared=True）不许回填**。leader 已经记过这一批，follower 既没打
    #    上游、也没拿到不同结果，再记一次就是把同一份需求数了 N 遍。而搭车恰好
    #    集中在热门词并发上——偏差不是均匀噪声，是系统性地往热榜头部堆：一个词
    #    越热、并发越高，放大倍数越大。单飞防住了钱，不能漏了账。
    # 缓存命中则照旧记（body 里没有 shared）——那是用户真的又搜了一次，热度要认。
    if LIBRARY_ON and not body.get("shared"):
        # 库条目不能再回填库：upsert 会刷新 last_seen_at，而新鲜度是库里打分的
        # 一项。自己搜自己、自己给自己加新，越热的词越显得新鲜——分数立刻失真。
        fresh = [it for it in (body.get("items") or []) if not it.get("fromLibrary")]
        if fresh:
            background.add_task(store.ingest, fresh, body.get("query") or "")

    if not CHECK_ON:
        return _public_body(body)

    items = body.get("items") or []
    # annotate 会把实测失效的直接摘掉。回写库之前必须记住"哪些被摘了"：
    # 摘掉不落库，库里那条就永远停在 unknown，下次搜索再探一遍、再摘一遍，
    # 白烧一轮外部请求，而库里还是不知道它是死的。
    lib_ids = [it["resourceId"] for it in items if it.get("resourceId")]
    kept, dropped = check.annotate(items)
    if dropped or kept is not items:
        body = {**body, "items": kept}
    if dropped:
        # 和 feedback 的 `hidden` 分开计数：一个是"多人举报"，一个是"实测失效"，
        # 排查误判时要能看出是哪一个在摘结果
        body["checkedBad"] = dropped
    if page == 1 and kept:
        # BackgroundTasks 在响应发出后才执行；schedule 内部只做一次 SQLite 读
        # 然后丢给线程池，不会拖住这次响应
        background.add_task(check.schedule, kept)
    # 本轮探活结论写回库。被摘掉的那些必然是 bad（annotate 只在这一种情况下丢弃），
    # 所以补一条 bad 进去，让库自己知道这条死了，而不是每轮都重新发现一次。
    if LIBRARY_ON and lib_ids:
        alive = {it["resourceId"]: it.get("checkState") for it in kept if it.get("resourceId")}
        marks = [(rid, state) for rid, state in alive.items() if state]
        marks += [(rid, check.STATE_BAD) for rid in lib_ids if rid not in alive]
        if marks:
            background.add_task(store.mark_check_many, marks)
    return _public_body(body)

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

# 同键并发去重：同一条缓存键同时未命中时只放一个请求出去，其余搭车。
# 这是**省钱**配置，不是性能配置 —— 见 search/singleflight.py 的说明。
_SINGLEFLIGHT = SingleFlight()

# 这里**不做全局串行排队**。
# 旧链路（providers.search）是"一次请求打全平台"，上游对紧接的第二个请求会挂到超时，
# 所以当年加了 MIN_INTERVAL=2s 的串行间隔。现在取数走插件层：并发控制在插件内部
# （haisou 的 MAX_CONCURRENCY），限流有 1003→429 退避重试兜着，重复查询有 5 分钟
# 响应缓存挡着。再留一道全局等待只会让每次搜索白等 2 秒，并且把多源并发重新串起来。
# 最近一次失败原因，只在 /api/health 暴露，便于远程判断是不是签名过期
_last_error = ""


@app.get("/api/platforms")
async def platforms():
    return {"code": 0, "platforms": PLATFORMS}


def _has_mirror_account() -> bool:
    """有没有启用中的转存账号。

    别改回 `bool(settings.mirror_quark_cookie)`：账号已经搬进 pan_accounts，
    只有第一次启动会从旧变量迁移一次。留着那个判断，后台明明加了百度账号，
    health 还是报 false——排查时会被这条假信息带去翻 .env，白花半小时。
    """
    if not MIRROR_ON or not pan.is_ready():
        return False
    try:
        return int(pan.summary().get("enabled") or 0) > 0
    except Exception:  # noqa: BLE001
        return False


def _library_stats() -> dict:
    """库的体检数据。挂在 health 的 library 字段里，不是独立路由。

    库出问题不许把健康检查带挂，所以整个函数体包在 try 里。
    """
    if not LIBRARY_ON:
        return {"enabled": False}
    try:
        return {"enabled": True, **store.stats()}
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "error": str(exc)}


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
        # 正在跑的"同键去重"任务数。持续大于 0 是正常的（有人在搜）；
        # 只有它和工作池同时打满才说明有人在并发刷同一个词。
        "singleflight": _SINGLEFLIGHT.inflight,
        # ---- 搜索源（pansou 插件层）----
        # enabled 是**实际加载成功**的源，不是 .env 里写的：写错名字或导入失败
        # 时这里会是空的，比去翻日志快。
        "sources": {
            "enabled": [p.name for p in _sources],
            "configured": settings.enabled_sources,
            "pool": source_stats.snapshot(),
            # 只列"有病"的（degraded / failing / zero_yield），健康的源不占体积。
            # zero_yield 是最隐蔽的一档：源可达、从不报错、但一条都抓不到。
            "liveness": liveness.snapshot(),
            # liveness 要攒够 5 轮才下结论，所以另外给一份"刚才那轮"的原始结果：
            # 重启后第一次搜索失败时，靠这个才能知道是谁失败了、什么分类。
            "lastRound": search_engine.last_round(),
        },
        "tgGate": (
            {**TG_GATE.snapshot(), "channels": tg_channels.effective(settings.tg_channel_list)}
            if any(p.name == "tg_searchers" for p in _sources)
            else {"enabled": False}
        ),
        "analytics": ANALYTICS_ON,
        "feedback": FEEDBACK_ON,
        "mirror": MIRROR_ON,
        "mirrorAccount": _has_mirror_account(),
        "linkCheck": check.summary() if CHECK_ON else {"enabled": False},
        "library": _library_stats(),
    }


@app.get("/api/ticket")
async def get_ticket(request: Request):
    """领一张匿名票据。前端进站时静默调一次，缓存复用，过期前再续。

    票据不携带任何身份信息，只是按来源 IP 签发的短期凭证。领票接口本身
    也计入 IP 限流——不然脚本可以无限领票绕过配额。票据校验在 /api/search。
    """
    ip = client_ip(request)
    allowed, wait = guard.allow(ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"code": 429, "msg": f"操作太频繁了，{wait} 秒后再试"},
        )
    return JSONResponse(content={"code": 0, **guard.issue(ip)})


@app.get("/api/search")
async def search(
    request: Request,
    background: BackgroundTasks,
    q: str = Query(..., min_length=1, max_length=100, description="搜索关键词"),
    platforms: str = Query("", description="平台 key，逗号分隔，留空为全部"),
    page: int = Query(1, ge=1, le=100),
    page_size: int = Query(20, ge=1, le=settings.max_page_size),
    search_in: str = Query("title", description="title 或 all"),
    src: str = Query(
        "tg",
        description="tg/plugin/library：tg = 只用 TG（默认）；plugin = 只用海搜接口；"
        "library = 只查本地索引，不烧额度、不自动降级",
    ),
    refresh: bool = Query(
        False,
        description="跳过响应缓存强制回源。**会真实消耗上游额度**，普通查询不要带",
    ),
    force_refresh: bool = Query(
        False, include_in_schema=False, description="refresh 的 pansou 兼容别名"
    ),
):
    global _last_error

    # 两个名字指同一件事：本项目习惯 `refresh`，pansou 的调用方习惯 `force_refresh`。
    force = refresh or force_refresh

    # ---- 防刷：先限流，再验票 ----
    # 顺序不能反：先验票的话，脚本可以无限发无票请求而不占配额。
    ip = client_ip(request)
    cost = 3 if force else 1

    # 小程序通道。两个头要同时齐：
    #   X-App-Token   —— 这是我们自己的端（服务端凭证，不下发到前端）
    #   X-Client-Hash —— 是哪一个用户（openid 的摘要，后端看不到身份本身）
    # 只有令牌没有身份 → 不当小程序处理，退回 IP 限流 + 验票，跟普通浏览器一视同仁。
    # 令牌泄露时这条规则是护栏：光有钥匙不能无限刷，每次还得自报一个身份，
    # 而每个身份都受单客户端配额约束。
    client_hash = (request.headers.get("x-client-hash") or "").strip()[:64]
    via_app = bool(client_hash) and guard.app_token_ok(request.headers.get("x-app-token", ""))

    if via_app:
        # 先单客户端，再总闸。反过来的话，一个正在刷的人也能把总闸吃满，
        # 挤掉的却是后面那些正常用户。
        allowed, wait = guard.allow_client(client_hash, cost)
        if allowed:
            allowed, wait = guard.allow_global(cost)
    else:
        # refresh 绕过缓存直接回源，是真花钱的：按 3 份配额计。默认 30 次/分钟时
        # 等于最多刷 10 次/分钟，普通搜索不受影响。
        allowed, wait = guard.allow(ip, cost)

    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"code": 429, "msg": f"操作太频繁了，{wait} 秒后再试"},
        )

    if not via_app:
        ticket = request.headers.get("x-ticket", "")
        if not guard.verify(ticket, ip):
            # 403 = 票据无效/缺失。前端收到后会自动重领一次再重试，
            # 所以正常用户几乎感知不到；挡掉的是不带票直接打接口的脚本。
            return JSONResponse(
                status_code=403,
                content={"code": 403, "msg": "票据无效，请重试"},
            )

    query = q.strip()
    requested = [p.strip() for p in platforms.split(",") if p.strip()]
    # 过滤掉上游不认识的值：传错一个 key 会让整次搜索失败
    platform_list = [p for p in requested if p in VALID_PLATFORM_KEYS]
    dropped = [p for p in requested if p not in VALID_PLATFORM_KEYS]
    if dropped:
        log.warning("忽略未知平台 key: %s", ",".join(dropped))

    scope = search_in if search_in in ("title", "all") else "title"

    # 来源通道：tg / plugin / library 三档，各跑各的，没有"搜不到自动换源"。
    # 旧的 auto/all 已随降级链路一起移除，传进来回 400 —— 静默当某档处理
    # 会让调用方以为筛了通道，其实没筛，也掩盖了还在用旧参数的客户端。
    source = src
    if source not in ("tg", "plugin", "library"):
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": f"未知的来源类型：{src}（可用 tg/plugin/library）"},
        )
    # 库没开就别往下走：单飞锁会为它建键、占配额，最后抛一个本可以在门口挡掉的错。
    if source == "library" and not LIBRARY_ON:
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": "本地库未启用（LIBRARY_ENABLED=0 或初始化失败）"},
        )

    # 缓存键必须带通道：两档各查各的，不带的话 src=tg 的结果会顶掉
    # src=plugin 的结果，反过来也一样——用户切了来源却拿到另一档的数据。
    cache_key = f"{query}|{','.join(platform_list)}|{page}|{page_size}|{scope}|{source}"
    now = time.time()

    # 埋点参数：真实 IP / UA / 匿名访客号，一次算好，几个出口复用
    visitor, session, is_new = _visitor_of(request)
    track_kw = {
        "query": query,
        "platform": ",".join(platform_list),
        "page": page,
        "visitor": visitor,
        "session": session,
        "ip": ip,
        "ua": request.headers.get("user-agent", ""),
        "referer": request.headers.get("referer", ""),
        "is_new": is_new,
    }

    # refresh=true 时绕过响应缓存，直接回源。**不能**把 refresh 拼进 cache_key：
    # 拼进去等于刷新和普通查询各留一份缓存，用户刷新完再搜拿到的还是老的那份。
    cached = None if force else _cache.get(cache_key)
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
                filtered=int(body.get("filtered") or 0),
            )
        return _export_search(body, background, page=page)

    # 刷新时也不看失败缓存：刚失败就点刷新，还回同一个 504，等于这个参数白给。
    failed = None if force else _fail_cache.get(cache_key)
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

    started = time.perf_counter()

    async def _run_search() -> dict:
        """回源 + 写缓存。抽成函数是为了让单飞锁只执行它一次。

        leader 用自己的返回值，follower 拿到的是**同一个 dict 对象**——所以调用方
        要改它必须先 `dict()` 复制。异常原样往外抛：失败也要广播给搭车的请求，
        否则它们各自重试，等于这把锁白加。
        """
        # 三档各跑各的，不做自动降级。
        # 每个源自己的窗口负责"别让用户干等"，超时未跑完的源会在后台继续抓并写缓存。
        if source == "library":
            # 库检索是同步 SQLite。必须丢线程：80 万行的 FTS + 打分排序跑在事件
            # 循环线程里，会把整个进程卡住，所有并发请求一起排队。
            result = await asyncio.to_thread(
                library_layer.search, query, platform_list, page, page_size
            )
            # 命中记录放在这里而不是 library 层：_run_search 只有单飞的 leader 会
            # 执行，follower 搭车不算"又搜了一次"。channel 单独记成 local，
            # 否则"库到底在兜什么"这件事永远看不出来。
            ids = [it["resourceId"] for it in result["items"] if it.get("resourceId")]
            if ids:
                background.add_task(
                    store.record_hits, ids, query, channel=store.CHANNEL_LOCAL
                )
        else:
            result = await search_engine.search(
                query,
                platform_list,
                page,
                page_size,
                scope,
                source=source,
                force_refresh=force,
            )
        body = {
            "code": 0,
            "msg": "",
            "query": query,
            "items": result["items"],
            "pagination": result["pagination"],
            "credits": result["_meta"].get("credits", 0),
            # 这次跑的通道（= 用户所选），恒为 tg / plugin。前端已不展示它，
            # 保留在响应里是为了运维和老调用方能一眼看出这次查的是哪一档。
            "tier": result["_meta"].get("tier", ""),
            # 标题不含关键词被丢掉的条数。空结果时前端据此提示"换个说法"，
            # 而不是让用户以为站上根本没有这个资源。
            "filtered": result["_meta"].get("filtered", 0),
            "tookMs": int((time.perf_counter() - started) * 1000),
            "cached": False,
        }
        _fail_cache.pop(cache_key, None)
        _cache[cache_key] = (time.time(), body)
        if len(_cache) > settings.cache_max_entries:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[: len(_cache) // 4]
            for k, _ in oldest:
                _cache.pop(k, None)
        return body

    try:
        body, shared = await _SINGLEFLIGHT.run(cache_key, _run_search)
    except search_engine.SourceNotEnabledError as exc:
        # 通道没启用是配置问题，不是上游故障：回 400，且**不写失败缓存**
        # （否则改完配置还得等 20 秒才能重试）。
        log.warning("source not enabled q=%s src=%s: %s", query, src, exc)
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    except search_engine.SearchEngineError as exc:
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

    _last_error = ""
    upstream_ms = int((time.perf_counter() - started) * 1000)

    if shared:
        # 搭车：本轮上游开销算在 leader 头上，这个请求既没打上游也没花钱。
        # body 是 leader 写进缓存的那个对象，动它等于污染其他请求 —— 先复制。
        body = dict(body)
        body["tookMs"] = 0
        body["cached"] = True
        body["shared"] = True
        upstream_ms = 0

    if ANALYTICS_ON:
        background.add_task(
            analytics.record_search,
            **track_kw,
            ok=True,
            duration_ms=upstream_ms,
            result_count=len(body["items"]),
            # 搭车和缓存命中在"没为这个请求打上游"这件事上是一回事
            cached=shared,
            filtered=int(body.get("filtered") or 0),
        )

    log.info(
        "search q=%s page=%s items=%s %sms%s",
        query, page, len(body["items"]), upstream_ms,
        "（同键搭车）" if shared else "",
    )
    return _export_search(dict(body), background, page=page)


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
<title>正在打开 · 盘迹</title>
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
.ttl{margin:14px 0 0;padding:8px 10px;border-radius:8px;background:#fff7e6;color:#9a6700;
  border:1px solid #ffe1a8;font-size:12.5px;line-height:1.5}
.acts{margin-top:20px;display:none;gap:10px;justify-content:center}
a.btn{display:inline-flex;align-items:center;height:34px;padding:0 15px;border-radius:8px;
  font-size:13px;text-decoration:none;color:#1b2430;border:1px solid #dfe3e9}
a.btn.primary{background:#1b2430;color:#fff;border-color:#1b2430}
/* 提取码块：只有平台上不认 URL 传码（天翼/微云/光鸭…）时才会出现 */
.code{display:none;align-items:center;justify-content:center;gap:8px;margin:14px 0 0;
  padding:10px 12px;border-radius:9px;background:#f2fbf9;border:1px solid #c9ece5;
  font-size:13px;color:#0f6b60}
.code b{font:600 15px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.16em;color:#0b5c52}
.code button{border:1px solid #b7dfd7;background:#fff;color:#0b5c52;border-radius:6px;
  font-size:12px;padding:4px 9px;cursor:pointer}
</style></head>
<body>
<div class="box">
  <div class="brand">PanTrace</div>
  <div class="ring" id="ring"></div>
  <h1 id="title">正在为你打开</h1>
  <p id="hint">正在准备这条资源，通常几秒内完成</p>
  <p class="ttl" id="ttl">链接只保留 __TTL__ 分钟，打开后请先转存到自己的网盘</p>
  <div class="code" id="code">
    <span>提取码</span><b id="codev"></b>
    <button type="button" id="copycode">复制</button>
  </div>
  <p class="err" id="err"></p>
  <div class="acts" id="acts">
    <a class="btn" href="/">返回搜索</a>
    <a class="btn primary" id="fb" href="/">提交反馈</a>
  </div>
</div>
<script>
var TOKEN = __TOKEN__;
var START = Date.now();
var MIN_SHOW = 1200;  // 最短停留：给"30 分钟内有效"那行留出可读时间
var RING = document.getElementById('ring');
var TITLE = document.getElementById('title');
var HINT = document.getElementById('hint');
var CODE = document.getElementById('code');
var CODEV = document.getElementById('codev');
var COPY = document.getElementById('copycode');

// 有些平台不认 URL 里的提取码（天翼、微云、光鸭等），后端会把码单独送过来。
// 只展示不写日志、不进链接，用户抄走或点复制即可。
COPY.addEventListener('click', function () {
  var text = CODEV.textContent || '';
  function done() { COPY.textContent = '已复制'; }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () {
      var range = document.createRange();
      range.selectNodeContents(CODEV);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      try { document.execCommand('copy'); } catch (e) {}
      done();
    });
    return;
  }
  var range = document.createRange();
  range.selectNodeContents(CODEV);
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  try { document.execCommand('copy'); } catch (e) {}
  done();
});

// 分阶段给话。原来一句"首次打开需要转存一份"有三个毛病：暴露我们在替用户
// 转存别人的分享、"首次"并不准（缓存命中是秒开，非首次也可能重建）、
// 4 秒就说"资源较大"是假信息。按真实耗时给提示，且只说用户视角的事。
var t1 = setTimeout(function () {
  HINT.textContent = '还在准备，好了会自动跳过去，不用重复点';
}, 5000);
var t2 = setTimeout(function () {
  HINT.textContent = '来源站响应偏慢，请再等一下，别关这个页面';
}, 15000);
var t3 = setTimeout(function () {
  HINT.textContent = '等得有点久了，可以返回换一条，或者告诉我们这条打不开';
}, 30000);
function stopTimers() { clearTimeout(t1); clearTimeout(t2); clearTimeout(t3); }

// 后端的 msg 是写给我们排障看的（"转存未启用""账号没有可用账号"），
// 原样展示等于骂用户。这里翻成人话，认不出来才用兜底那句。
function reasonText(msg) {
  var m = String(msg || '');
  if (/失效|删除|取消|不存在|过期/.test(m)) return '这条分享已经被删除或过期了';
  if (/违规|侵权/.test(m)) return '这条资源被来源站下架了';
  if (/提取码|密码|验证码/.test(m)) return '这条资源需要提取码，暂时打不开';
  if (/容量|空间|账号|登录|cookie/i.test(m)) return '资源准备服务正忙，过一会儿再试';
  if (/体积|太大|阈值/.test(m)) return '这条资源太大，暂时打不开';
  if (/频繁|限流/.test(m)) return '操作太频繁了，过一两分钟再试';
  return '这条资源可能已经失效了';
}

function fail(msg) {
  stopTimers();
  RING.style.display = 'none';
  TITLE.textContent = '这条资源打不开';
  HINT.textContent = reasonText(msg);
  var e = document.getElementById('err');
  e.textContent = '换一个关键词试试；实在找不到，就点下面告诉我们，我们去找。';
  e.style.display = 'block';
  document.getElementById('acts').style.display = 'flex';
  document.getElementById('fb').href = '/?fb=' + encodeURIComponent(location.pathname);
}

// 不给超时的话，后端卡在上游那 75 秒里，用户就对着转圈干等
var ctl = new AbortController();
var hardStop = setTimeout(function () { ctl.abort() }, 75000);
fetch('/api/resolve', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  cache: 'no-store',
  signal: ctl.signal,
  body: JSON.stringify({ t: TOKEN })
}).then(function (r) { return r.json() }).then(function (j) {
  clearTimeout(hardStop);
  if (j && j.url) {
    stopTimers();
    if (j.pwd) {
      CODE.style.display = 'flex';
      CODEV.textContent = j.pwd;
      // 要抄码，从拿到结果起再留 6 秒，不受前面转存耗时影响
      MIN_SHOW = (Date.now() - START) + 6000;
      TITLE.textContent = '这条资源的提取码在下面';
      HINT.textContent = '先记下提取码，稍后会自动打开网盘';
      document.getElementById('ttl').textContent = '打开网盘后如提示输入提取码，粘贴上面的即可';
    } else {
      HINT.textContent = '好了，正在跳转…';
    }
    // 准备太快时会一闪而过，"30 分钟有效"这句等于没说，压到 1.2 秒
    var wait = Math.max(0, MIN_SHOW - (Date.now() - START));
    setTimeout(function () { location.replace(j.url) }, wait);
    return;
  }
  fail(j && j.msg);
}).catch(function () {
  clearTimeout(hardStop);
  fail('网络中断，请返回重试');
});
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
    html = (
        _GO_HTML
        .replace("__TOKEN__", json.dumps(token[:900]))
        .replace("__TTL__", str(max(1, settings.mirror_link_minutes)))
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


class ResolveIn(BaseModel):
    t: str = ""


# 打开频率。正常用户一分钟点不了十次；脚本扫这个接口是在试我们的账号额度。
#
# 三个桶，对应两条身份路径：
#   IP 桶   —— 浏览器、爬虫、没带令牌的一切调用方，维持原来的 60 次/10 分钟。
#   用户桶  —— 小程序里的一个人。云函数出口 IP 是共享的，按 IP 计等于全站用户
#             挤在同一个 60 次里，几十个人就把"取链接"点到 429。
#   通道桶  —— 小程序总量。令牌是全局共享的，用户暴涨或令牌泄露时，
#             不能让转存任务无限堆积。
_resolve_hits: dict[str, tuple[float, int]] = {}
_resolve_user_hits: dict[str, tuple[float, int]] = {}
_resolve_app_hits: dict[str, tuple[float, int]] = {}

RESOLVE_PER_10MIN = 60         # 单 IP
RESOLVE_USER_PER_10MIN = 10    # 单个小程序用户
RESOLVE_APP_PER_10MIN = 300    # 小程序通道总量


def _hit_allowed(store: dict[str, tuple[float, int]], key: str, limit: int) -> bool:
    """固定窗口计数：从第一次命中起算 600 秒，窗口内超过 limit 就拒绝。

    顺手清一次过期键，防止海量 IP / 摘要把字典撑爆。
    """
    now = time.time()
    window, count = store.get(key, (now, 0))
    if now - window > 600:
        window, count = now, 0
    count += 1
    store[key] = (window, count)
    if len(store) > 4096:
        for k, (started, _) in list(store.items()):
            if now - started > 600:
                store.pop(k, None)
    return count <= limit


def _resolve_gate(request: Request) -> bool:
    """取链接的频率闸门。True = 放行。

    身份规则跟 /api/search 完全一致：令牌 + 客户端摘要都齐才算"我们自己的端"。
    只有摘要没有令牌不算——摘要谁都能编，没有令牌背书的分桶等于没分桶。
    """
    client_hash = (request.headers.get("x-client-hash") or "").strip()[:64]
    via_app = bool(client_hash) and guard.app_token_ok(request.headers.get("x-app-token", ""))

    if not via_app:
        return _hit_allowed(_resolve_hits, client_ip(request), RESOLVE_PER_10MIN)

    # 先单用户，再通道总量。反过来的话，一个正在刷的人能把总闸吃满，
    # 被挤掉的却是后面那些正常点链接的人。
    if not _hit_allowed(_resolve_user_hits, client_hash, RESOLVE_USER_PER_10MIN):
        return False
    return _hit_allowed(_resolve_app_hits, "app", RESOLVE_APP_PER_10MIN)


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
    if not _resolve_gate(request):
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
    pwd = ""
    if not result["ok"]:
        if settings.mirror_fallback:
            # 回退要带提取码：用户拿到的是一半链接等于没给。
            # 拼得进 URL 的（夸克/百度/115/蓝奏…）平台会自动填；拼不进的
            # （天翼/微云/光鸭…）把码单独交给页面展示，否则用户对着输入框
            # 手上却没码——这是这套回退机制以前最难看的一处
            url, applied = resolver.embed_pwd(info["url"], info["pwd"])
            fallback = True
            if info["pwd"] and not applied:
                pwd = info["pwd"]
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
            # 只在"码没拼进链接"时才有值，过渡页会显式展示给用户抄
            "pwd": pwd,
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


@app.get("/api/admin/source-rank", include_in_schema=False)
async def admin_source_rank(request: Request, limit: int = Query(20, ge=1, le=200)):
    """来源贡献排行榜：用户点开资源时给对应 TG 频道 / 插件加一分。

    只统计计分功能启用之后的点击——历史数据在另一个表，天然不参与。
    前端有两个开关：按频道看还是按插件看，传 kind 过滤即可。
    """
    if not SOURCE_SCORE_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "来源计分已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    return {"code": 0, "data": source_score.rank(limit)}


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


# ---- 广告过滤配置 ----
#
# 值存在库里的 pan_settings 表，后台改完**立即生效**，不用重启。给运营这个
# 入口的原因很直接：关键词是唯一会造成误杀的东西，得边看日志边调词，
# 改 .env + 重启一圈跟不上。


class AdFilterIn(BaseModel):
    preskip: bool = True
    filter: bool = False
    keywords: str = ""
    depth: int = 2
    max_mb: float = 2.0


class AdProbeIn(BaseModel):
    name: str = ""
    size: int = 0
    is_dir: bool = False


def _ad_payload() -> dict:
    """配置 + 判据里那张后缀白名单。前端要把它摆给人看，不然没人知道
    "为什么我的 .mp4 没被拦"。
    """
    data = pan.ad_settings()
    data["suffixes"] = sorted(adfilter.ATTACHMENT_SUFFIXES)
    return data


@app.get("/api/admin/ad-filter", include_in_schema=False)
async def admin_ad_filter_get(request: Request):
    """当前**生效**的广告配置（不是 .env 里的原值）。"""
    if not MIRROR_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "点击转存已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    return {"code": 0, "data": _ad_payload()}


@app.post("/api/admin/ad-filter", include_in_schema=False)
async def admin_ad_filter_save(payload: AdFilterIn, request: Request):
    """保存并热更。不重启，下一次点击转存就用新词。"""
    if not MIRROR_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "点击转存已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        pan.save_ad_settings(
            preskip=payload.preskip,
            filter=payload.filter,
            keywords=payload.keywords,
            depth=payload.depth,
            max_mb=payload.max_mb,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    return {"code": 0, "data": _ad_payload()}


@app.post("/api/admin/ad-filter/probe", include_in_schema=False)
async def admin_ad_filter_probe(payload: AdProbeIn, request: Request):
    """拿一个真实文件名试会不会被拦。

    走的是线上同一套判断函数（`adfilter.explain`），不是前端复刻一份规则——
    复刻的那种过两个月一定和线上对不上。
    """
    if not MIRROR_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "点击转存已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    return {
        "code": 0,
        "data": adfilter.explain(
            payload.name,
            payload.size,
            pan.ad_keywords(),
            pan.ad_max_bytes(),
            is_dir=payload.is_dir,
        ),
    }


# ---- 转存水印配置 ----
#
# 转存时在落点目录建的广告文件夹名。存库覆盖 .env，保存即热更，不用重启。
# 空名字 = 关闭水印，不产生任何额外请求。


class WatermarkIn(BaseModel):
    name: str = ""


@app.get("/api/admin/watermark", include_in_schema=False)
async def admin_watermark_get(request: Request):
    if not MIRROR_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "点击转存已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    return {"code": 0, "data": pan.watermark_settings()}


@app.post("/api/admin/watermark", include_in_schema=False)
async def admin_watermark_save(payload: WatermarkIn, request: Request):
    """保存并热更。下一次点击转存就用新名字。"""
    if not MIRROR_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "点击转存已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        pan.save_watermark_settings(name=payload.name)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    return {"code": 0, "data": pan.watermark_settings()}


# ---- TG 频道配置 ----
#
# 频道从 .env 的 TG_CHANNELS 搬进了库，后台直接维护，保存即热更。
# .env 仍保留为"恢复默认"的基线，不再需要为了加一个频道去重启服务。


class TgChannelsIn(BaseModel):
    channels: list[str] | str = Field(default_factory=list)


class TgProbeIn(BaseModel):
    channel: str = ""


def _tg_payload() -> dict:
    """把"当前生效值"和"默认值"分开给前端，不然用户分不清改没改过。"""
    override = tg_channels.configured()
    return {
        "channels": override if override is not None else settings.tg_channel_list,
        "source": "admin" if override is not None else "env",
        "defaults": settings.tg_channel_list,
        "max": tg_channels.MAX_CHANNELS,
        # TG 源没启用时配置照样能存，但不会生效——界面必须说清楚
        "enabled": any(p.name == "tg_searchers" for p in _sources),
    }


@app.get("/api/admin/tg-channels", include_in_schema=False)
async def admin_tg_channels_get(request: Request):
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    return {"code": 0, "data": _tg_payload()}


@app.post("/api/admin/tg-channels", include_in_schema=False)
async def admin_tg_channels_save(payload: TgChannelsIn, request: Request):
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        tg_channels.save(payload.channels)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    return {"code": 0, "data": _tg_payload()}


@app.post("/api/admin/tg-channels/reset", include_in_schema=False)
async def admin_tg_channels_reset(request: Request):
    """删掉库里的覆盖，回到 .env。空列表是有效配置，不能当成"没配置"。"""
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    tg_channels.clear()
    return {"code": 0, "data": _tg_payload()}


@app.post("/api/admin/tg-channels/probe", include_in_schema=False)
async def admin_tg_channels_probe(payload: TgProbeIn, request: Request):
    """抓一次频道公开页，确认它存在且能被解析器读懂。

    不走可达性门：门是给搜索路径省时间的，后台排障要的是这个频道的真实结论。
    """
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        names = tg_channels.normalize(payload.channel, allow_empty=False)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    try:
        results = await tg_search_channel("", names[0], timeout=6.0)
    except Exception as exc:  # noqa: BLE001 - 排障接口要把失败原因原样给人看
        return {"code": 0, "data": {"ok": False, "channel": names[0], "error": str(exc) or type(exc).__name__}}
    return {"code": 0, "data": {"ok": True, "channel": names[0], "count": len(results)}}


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
    #: 网盘特有的附加凭据。目前只有百度用：client_id / client_secret。
    #: 单开一个字段而不是拆成两个列，是因为以后别的盘也大概率有这种"应用级"配置。
    extra: dict = Field(default_factory=dict)


def _clean_extra(extra: dict | None) -> dict[str, str]:
    """附加凭据去掉空值。别把空串存进库——读的时候分不清"没配"和"配了个空"。"""
    return {
        str(key): str(value).strip()
        for key, value in (extra or {}).items()
        if str(value).strip()
    }


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
        extra=_clean_extra(payload.extra),
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
    # 附加凭据单独处理：空对象不能当"清空"用，否则前端编辑时没填
    # client_secret 就会把已经存着的 client_id 一起抹掉。这里做合并。
    fresh_extra = _clean_extra(payload.extra)
    if fresh_extra:
        current = next((a for a in pan.list_accounts() if a.id == aid), None)
        merged = dict(current.extra) if current else {}
        merged.update(fresh_extra)
        fields["extra"] = merged
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


@app.post("/api/admin/maintain/recycle", include_in_schema=False)
async def admin_recycle(request: Request, force: bool = True):
    """立刻跑一轮到期回收，不用等后台线程那 5 分钟。

    `force=true` 会先把 retry_after 清零——排查时最常做的动作就是"我改完
    配置了，马上试一次"，否则失败的记录还压着一小时退避，看起来像没生效。
    返回体里带每条的失败原因，和日志里的 `src_key 前缀: 错误` 对得上。
    """
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})

    def _run() -> dict:
        if force:
            with resolver.session() as conn:
                conn.execute(
                    "UPDATE mirrors SET retry_after=0, last_error='' "
                    "WHERE status='ok' AND retry_after > 0"
                )
        return pan.recycle_expired(resolver.session)

    try:
        result = await asyncio.to_thread(_run)
        return {"code": 0, "data": result}
    except Exception as exc:  # noqa: BLE001
        log.exception("手动回收失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"回收失败：{exc}"})


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


@app.post("/api/admin/maintain/violated", include_in_schema=False)
async def admin_violated(request: Request, dry_run: bool = False):
    """跑一轮违规分享巡检（每账号列分享 → 批量撤销）。

    `dry_run=true` 只列不撤，返回前 5 条样本。上线前先用它看一眼要撤的是
    什么，别一上来就整批撤销——这些账号上可能还有你手工建的分享。
    只有实现了撤销接口的网盘会参与（目前是夸克/UC），其余的算「跳过」。
    """
    if not MIRROR_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "点击转存已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    try:
        result = await asyncio.to_thread(
            pan.check_violated, dry_run=dry_run, mirrors_session=resolver.session
        )
        return {"code": 0, "data": result}
    except Exception as exc:  # noqa: BLE001
        log.exception("违规分享巡检失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"巡检失败：{exc}"})


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


@app.get("/api/admin/link-check", include_in_schema=False)
async def admin_link_check(request: Request, state: str = "", limit: int = 30):
    """检测概况 + 最近记录。

    上线后第一天看两件事：`uncertain` 占比高说明接口被限流/改版（检测在空转），
    `bad` 占比高得不正常说明判定映射错了（会误杀结果）。都在这一个接口里。
    """
    if not CHECK_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "链接检测已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    return {
        "code": 0,
        "data": {"summary": check.summary(), "recent": check.recent(state, limit)},
    }


class LinkCheckIn(BaseModel):
    url: str
    pwd: str = ""


@app.post("/api/admin/link-check/test", include_in_schema=False)
async def admin_link_check_test(payload: LinkCheckIn, request: Request):
    """手工检一条链接。上线前用它确认"匿名接口还认我们"，别等用户来反馈。

    走和批量检测完全相同的 checker，结果会覆盖旧结论——这也是误判的复位手段：
    某条好资源被标成失效，跑一次这里就恢复。
    """
    if not CHECK_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "链接检测已关闭"})
    if not _admin_allowed(request):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})
    url = (payload.url or "").strip()[:500]
    if not url.startswith("http"):
        return JSONResponse(status_code=400, content={"code": 400, "msg": "给一条完整分享链接"})
    try:
        result = await asyncio.to_thread(check.probe, url, payload.pwd or "")
    except Exception as exc:  # noqa: BLE001
        log.exception("手工检测失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"检测失败：{exc}"})
    return {"code": 0, "data": result}


# ---------------------------------------------------------------- 资源库：文档导入
#
# 看板「添加数据」面板的全部后端。流程本体在 `library_sync.py`（命令行脚本用的
# 也是同一份），这里只做鉴权、参数校验和"别把事件循环堵住"。
#
# 三个必须异步的地方：探测要读腾讯文档（几秒）、登记要校验链接列（又一次读）、
# 抓取要几分钟。前两个走 `asyncio.to_thread`，最后一个交给 library_sync 的
# 后台线程——HTTP 请求只负责"起个头"，进度靠轮询。


class DocProbeIn(BaseModel):
    url: str
    tab_id: str = ""


class DocSourceIn(BaseModel):
    url: str
    tab_id: str = ""
    title: str = ""
    title_col: int = 0
    link_col: int = 0
    code_col: int = 0
    start_row: int = 0


class DocSourcePatch(BaseModel):
    # 全部 Optional：PATCH 只改传来的字段。用 0 当"清空"是合法的（回到自动识别），
    # 所以不能用默认值 0 判断"没传"——那会把"想清空"和"没提到"混为一谈。
    title_col: int | None = None
    link_col: int | None = None
    code_col: int | None = None
    start_row: int | None = None
    enabled: bool | None = None


def _library_off() -> JSONResponse:
    return JSONResponse(status_code=503, content={"code": 503, "msg": "资源库已关闭"})


def _need_token() -> JSONResponse:
    return JSONResponse(status_code=401, content={"code": 401, "msg": "需要口令"})


@app.get("/api/admin/library/sources", include_in_schema=False)
async def admin_library_sources(request: Request):
    """订阅列表 + 库总量 + 每份表正在跑的作业。一次拿全，面板不用请求两遍。"""
    if not LIBRARY_ON:
        return _library_off()
    if not _admin_allowed(request):
        return _need_token()
    jobs = library_sync.status()
    items = []
    for src in store.list_doc_sources():
        row = dict(src)
        row["job"] = jobs.get(str(src["id"])) or {}
        items.append(row)
    return {
        "code": 0,
        "data": {
            "sources": items,
            "stats": _library_stats(),
            "concurrency": {"running": len(library_sync._running), "max": library_sync.MAX_CONCURRENT},
            # 下次自动跑的时间 + 上次跑的结果。面板上的"每天 04:00 自动同步"
            # 不是写死的文案，是这里给出来的真实值——改配置立刻能看出来。
            "daily": library_sync.daily_info(),
        },
    }


@app.post("/api/admin/library/probe", include_in_schema=False)
async def admin_library_probe(payload: DocProbeIn, request: Request):
    """读一份腾讯文档的前几十行，报出每个工作表的结构。

    这是面板第一屏：先看清表长什么样，再决定点哪个工作表、要不要锁列号。
    错误文案原样透出（"要 docs.qq.com/sheet/… 那种"），前端不用自己翻译。
    """
    if not LIBRARY_ON:
        return _library_off()
    if not _admin_allowed(request):
        return _need_token()
    url = (payload.url or "").strip()
    if not url:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "链接不能为空"})
    try:
        data = await asyncio.to_thread(library_sync.probe, url, payload.tab_id or "")
    except Exception as exc:  # noqa: BLE001
        # 这里绝大多数是"文档没开任何人可查看""不是表格链接"，属于用户输入问题，
        # 不是服务端故障。给 400 + 原文，比 500 更有用。
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    return {"code": 0, "data": data}


@app.post("/api/admin/library/sources", include_in_schema=False)
async def admin_library_add(payload: DocSourceIn, request: Request):
    """登记一份表。已经存在的只补标题/列配置，不动进度。"""
    if not LIBRARY_ON:
        return _library_off()
    if not _admin_allowed(request):
        return _need_token()
    url = (payload.url or "").strip()
    if not url:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "链接不能为空"})
    cols = {
        "title_col": payload.title_col,
        "link_col": payload.link_col,
        "code_col": payload.code_col,
        "start_row": payload.start_row,
    }

    try:
        pad_id, tab_id = await asyncio.to_thread(_pad_and_tab, url, payload.tab_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})

    # 列号指错了不拦，但要说出来：拦死的代价是"前 40 行刚好没链接的表
    # 永远加不进来"，而放过的代价只是标题可能是备注——后者能看出来，前者不能。
    warn = ""
    if cols["link_col"]:
        try:
            warn = await asyncio.to_thread(
                library_sync.check_link_column, url, tab_id, cols["link_col"]
            )
        except Exception:  # noqa: BLE001
            warn = ""
    got = store.add_doc_source(
        pad_id=pad_id, tab_id=tab_id, title=payload.title, url=url.split("?")[0], **cols
    )
    return {"code": 0, "data": {**got, "warning": warn}}


def _pad_and_tab(url: str, tab_id: str) -> tuple[str, str]:
    """链接 → (pad_id, tab_id)。显式传的 tab 优先于链接里 `?tab=` 带的。"""
    from . import docs_qq

    pad_id, from_url = docs_qq.pad_id_of(url)
    return pad_id, tab_id or from_url


@app.patch("/api/admin/library/sources/{sid}", include_in_schema=False)
async def admin_library_patch(sid: int, payload: DocSourcePatch, request: Request):
    """改列配置 / 起始行 / 启停。

    改列配置要校验，指错了直接拒——这是唯一能在写入前拦住的机会。

    **列号或起始行真变了，就把进度归零。** 已入库那批是按旧配置解析的，
    不重读的话错数据会一直躺在库里，而新配置只对以后新增的行生效。
    归零后下一轮从头读一遍（upsert，不会重复），代价是表大的话要重跑几分钟。
    """
    if not LIBRARY_ON:
        return _library_off()
    if not _admin_allowed(request):
        return _need_token()
    src = next((s for s in store.list_doc_sources() if int(s["id"]) == sid), None)
    if not src:
        return JSONResponse(status_code=404, content={"code": 404, "msg": f"没有 id={sid} 的订阅"})

    if payload.enabled is not None:
        store.set_doc_enabled(sid, payload.enabled)

    touched = (payload.title_col, payload.link_col, payload.code_col, payload.start_row)
    if any(v is not None for v in touched):
        cols = {
            "title_col": payload.title_col if payload.title_col is not None else int(src["title_col"]),
            "link_col": payload.link_col if payload.link_col is not None else int(src["link_col"]),
            "code_col": payload.code_col if payload.code_col is not None else int(src["code_col"]),
            "start_row": payload.start_row if payload.start_row is not None else int(src["start_row"]),
        }
        if cols["link_col"]:
            try:
                warn = await asyncio.to_thread(
                    library_sync.check_link_column, src["url"], src["tab_id"], cols["link_col"]
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
            if warn:
                return JSONResponse(status_code=400, content={"code": 400, "msg": warn})
        changed = any(int(src.get(k) or 0) != int(v) for k, v in cols.items())
        if changed and library_sync.is_running(sid):
            return JSONResponse(
                status_code=409,
                content={"code": 409, "msg": "这份表正在抓取，等它跑完再改配置"},
            )
        store.set_doc_columns(sid, reset_progress=changed, **cols)
        return {"code": 0, "data": {"id": sid, "reset": changed}}
    return {"code": 0, "data": {"id": sid, "reset": False}}


@app.delete("/api/admin/library/sources/{sid}", include_in_schema=False)
async def admin_library_drop(sid: int, request: Request):
    """删订阅。**不动已入库的资源**——那些已经是库里的真实数据了。"""
    if not LIBRARY_ON:
        return _library_off()
    if not _admin_allowed(request):
        return _need_token()
    if library_sync.is_running(sid):
        return JSONResponse(status_code=409, content={"code": 409, "msg": "这份表正在抓取，等它跑完再删"})
    ok = store.drop_doc_source(sid)
    if not ok:
        return JSONResponse(status_code=404, content={"code": 404, "msg": f"没有 id={sid} 的订阅"})
    return {"code": 0, "data": {"id": sid}}


@app.post("/api/admin/library/sources/{sid}/sync", include_in_schema=False)
async def admin_library_sync(sid: int, request: Request):
    """起一轮抓取。立即返回，进度靠 `jobs` 轮询。

    38 万行的表要三分钟。同步等的话，任何一个反代都会在 60 秒断掉连接，
    而抓取其实还在后台跑——用户看到报错，重试又起一份，两份同时写同一个库。
    """
    if not LIBRARY_ON:
        return _library_off()
    if not _admin_allowed(request):
        return _need_token()
    src = next((s for s in store.list_doc_sources() if int(s["id"]) == sid), None)
    if not src:
        return JSONResponse(status_code=404, content={"code": 404, "msg": f"没有 id={sid} 的订阅"})
    job = library_sync.start(sid)
    if job.get("state") == "busy":
        return JSONResponse(status_code=429, content={"code": 429, "msg": job["msg"]})
    return {"code": 0, "data": job}


@app.get("/api/admin/library/jobs", include_in_schema=False)
async def admin_library_jobs(request: Request):
    """所有作业状态。前端 3 秒轮询一次，只看 running 的那几条。"""
    if not LIBRARY_ON:
        return _library_off()
    if not _admin_allowed(request):
        return _need_token()
    return {"code": 0, "data": library_sync.status()}


# ---------------------------------------------------------------- 今日更新
#
# 主站「今日更新」页的数据源。排序口径和搜索不同：这里只按入库时间倒序，
# 不做相关性打分——它回答的是"今天进了什么"，不是"帮我找什么"。


def _day_start(now: float, back_days: int = 0) -> int:
    """本地时区某天 0 点的时间戳，再往前退 back_days 天。

    不能拿 `now - n*86400` 当"昨天 0 点"：那是"此刻往前推 N 天"，同一天内
    不同时刻算出来的区间宽度都不一样，页面上会出现"10 点看 320 条、11 点看
    280 条"这种没法解释的漂移。按日历日切才是"今日"。
    """
    y, m, d = time.localtime(now)[:3]
    midnight = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
    return int(midnight - back_days * 86400)


@app.get("/api/today", include_in_schema=False)
async def today_feed(page: int = 1, page_size: int = 30, days: int = 1):
    """今天（或近 N 天）**从 QQ 群采集进来的**资源。

    只放 origin=qq，不做"全来源大杂烩"：
    - 上游回填和表格订阅是机器爬来的，天天几千条，铺在首页上等于把"今天群里
      有人分享了什么"这件事淹掉，那正是这个页面唯一的用处；
    - 上游结果还涉及链接是否外露、是否该被索引，范围越小越不容易出事。

    不带令牌：这些数据本来就会出现在搜索结果里，多加一道锁只是让页面打不开。
    但**真实链接照样不外露**——出口和搜索共用 `_public_body`，链接换成
    `/go/<token>` 再下发。
    """
    if not LIBRARY_ON:
        return _library_off()

    days = max(1, min(30, int(days)))
    page = max(1, min(200, int(page)))
    page_size = max(1, min(60, int(page_size)))
    now = time.time()

    since = _day_start(now, days - 1)
    got = store.recent(
        since=since,
        limit=page_size,
        offset=(page - 1) * page_size,
        origin=store.ORIGIN_QQ,
        now=int(now),
    )
    items = _public_body({"items": got["items"]})["items"]
    summary = store.day_summary(since, origin=store.ORIGIN_QQ, now=int(now))
    # 平铺返回，和 /api/search、/api/platforms 一致——公开 GET 不套 data 壳。
    # 前端 request() 拆完 code 之后直接读 data.items，多包一层就是"接口 200、
    # 页面说没数据"，而且不报错，最难查。
    return {
        "code": 0,
        "msg": "",
        "date": time.strftime("%Y-%m-%d", time.localtime(since)),
        "days": days,
        "origin": store.ORIGIN_QQ,
        "updatedAt": int(now),
        "items": items,
        "pagination": {
            "page": page,
            "pageSize": page_size,
            "total": got["total"],
            "totalPages": (got["total"] + page_size - 1) // page_size,
        },
        "summary": summary,
    }


# ---------------------------------------------------------------- QQ 群采集
#
# AstrBot + NapCat 把指定群里群主/管理员发的分享消息原样 POST 过来，这里解析
# 入库。**解析只在后端做一份**：插件侧能拿到的只有文本，如果让它自己写一套
# 链接正则，迟早和 `search/clean.py` 打架——今天群里能认出来的写法，明天表格
# 导入认不出来，这种不一致查起来极费劲。
#
# 所以插件的职责被压到最小：判定「哪个群、谁发的、有没有链接」，然后转发。


class QqItemIn(BaseModel):
    """AstrBot 侧用大模型结构化出来的单条资源。

    **只有 `title` 是真正被采纳的**：`shareUrl` 要能在原文里对上同一个
    `platform:pwd_id`，`sharePwd` 要能在原文里找到，否则一律退回规则解析。
    模型把短链补全、把 id 记错一个字符，落库就是死链，而用户点开只会看到
    "分享已失效"，没人会回头怀疑是采集时写坏的。
    """

    title: str = ""
    shareUrl: str = ""
    sharePwd: str = ""


class QqMessageIn(BaseModel):
    id: str = ""             # 平台消息 id，只用于回执对账
    groupId: str = ""
    groupName: str = ""
    senderId: str = ""
    senderName: str = ""
    role: str = ""           # owner / admin / member
    text: str = ""
    # 可选。带了这个字段就走「AI 定标题 + 正则兜底校验」，没带就纯正则解析，
    # 老版本插件不升级也能继续上报。
    items: list[QqItemIn] = Field(default_factory=list)


class QqIngestIn(BaseModel):
    messages: list[QqMessageIn] = Field(default_factory=list)


def _ingest_allowed(request: Request) -> bool:
    """采集口鉴权。

    和看板口令分开一个 token：AstrBot 可能跑在另一台机器、另一个容器里，
    不该让它拿着能改后台配置、能删订阅的那个口令。

    `qq_ingest_token` 留空则回退 `admin_token`；两个都空就只允许本机——
    跟看板同一套安全默认值，不会因为忘了配就裸奔。
    """
    token = settings.qq_ingest_token or settings.admin_token
    if token:
        got = request.headers.get("x-ingest-token") or ""
        return hmac.compare_digest(got, token)
    return client_ip(request) in ("127.0.0.1", "::1")


@app.post("/api/ingest/qq", include_in_schema=False)
def ingest_qq(payload: QqIngestIn, request: Request):
    """接收一批 QQ 群消息，解析出网盘链接入库。

    成功只代表「收到并处理了」，不代表每条都进了库：`dropped` 里是没解析出
    链接的原因，`links` 是实际提取到的条数。管理端对账看这两个数。

    刻意写成同步 `def` 而不是 `async def`：解析 + 写库全是同步 sqlite 操作，
    一批上百条消息、上千个链接时能跑几秒。放在 async 函数里会卡住整个事件
    循环（同一时刻的搜索请求全排队），FastAPI 对同步 def 会自动丢线程池。
    """
    if not QQ_INGEST_ON:
        return JSONResponse(status_code=503, content={"code": 503, "msg": "QQ 采集已关闭"})
    if not _ingest_allowed(request):
        return _need_token()

    msgs = payload.messages
    if not msgs:
        return {"code": 0, "data": {"added": 0, "touched": 0, "links": 0, "dropped": []}}
    if settings.qq_ingest_max_batch and len(msgs) > settings.qq_ingest_max_batch:
        # 0 = 不限（默认）。真配了正数才当护栏用——防的是插件那边群白名单
        # 被留空、把整个 QQ 的闲聊都上报上来的事故。
        return JSONResponse(
            status_code=413,
            content={
                "code": 413,
                "msg": f"一次最多 {settings.qq_ingest_max_batch} 条消息，当前 {len(msgs)} 条",
            },
        )

    items, dropped = qq_intake.parse_batch([m.model_dump() for m in msgs])
    ai_msgs = sum(1 for m in msgs if m.items)
    if not items:
        return {"code": 0, "data": {
            "added": 0, "touched": 0, "links": 0, "aiMessages": ai_msgs,
            "dropped": dropped[:20],
        }}

    # store.ingest 自己吞异常（影子回填的约定：旁路不许拖垮主流程），这里不再包 try
    got = store.ingest(items)
    log.info(
        "QQ 采集：%s 条消息（AI 结构化 %s 条）→ %s 条链接，新增 %s / 更新 %s",
        len(msgs), ai_msgs, len(items), got.get("added", 0), got.get("touched", 0),
    )
    return {"code": 0, "data": {
        "added": got.get("added", 0),
        "touched": got.get("touched", 0),
        "links": len(items),
        "aiMessages": ai_msgs,
        "dropped": dropped[:20],
        # 回传前几条标题：管理端/日志里能立刻看出"标题抓对了没有"，
        # 不用等入库再去搜
        "titles": [it["title"] for it in items[:10]],
    }}


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
