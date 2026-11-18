"""应用配置。所有敏感信息只从环境变量 / .env 读取，绝不进入前端产物。"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 手写 .env 时最容易混进来的不可见字符
_INVISIBLE = "\ufeff\u200b\u200c\u200d\u00a0"

# backend/ 目录。用绝对路径定位 .env，避免依赖进程的当前工作目录
# （Linux 上用 systemd 启动时 CWD 往往不是 backend/，用相对路径会读不到 apikey）
BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 上游：idatariver 控制台 MAPI（唯一通道）----
    # 鉴权只认 x-idr-sign，cookie 不需要。签名会过期，过期返回 code 1004。
    # 抓法：浏览器打开控制台任意一次搜索，F12 → Network → 该请求的
    #       x-idr-sign / x-idr-time 两个请求头，原样填进来。
    idr_url: str = "https://user.idatariver.com/api/user/app/mapi"
    idr_sign: str = ""
    idr_time: str = ""
    api_key: str = ""

    # 官方要求超时 > 60 秒，否则提前中断仍会计费
    request_timeout: float = 75.0
    # 连接超时单独设。上游走 Cloudflare，实测 TLS 握手就要 ~4 秒，
    # 国内服务器更慢，原来 10 秒会让握手慢的连接直接判失败。
    connect_timeout: float = 25.0

    # ---- 搜索源（pansou 插件层白名单）----
    # 逗号分隔的插件名，**空 = 一个源都不启用**（pansou 的约定，新源必须显式批准）。
    # 默认给 haisou：它是目前唯一的源，留空会让 /api/search 直接返回 504。
    enabled_sources: str = "haisou"

    # 上游**按请求计费**，而一次"全平台搜索"要发 5 平台 × 2 页 = 10 个请求。
    # 所以前端改成"先选网盘再搜"，请求里不带 platforms 时只查这一个盘兜底，
    # 绝不退化成全平台 —— 那等于用户随手点一下搜索就烧掉 10 份额度。
    # 默认夸克：数据量最大（total 封顶 1000），覆盖率最高。
    default_platform: str = "quark"

    # ---- 网络 ----
    # 默认不使用系统环境变量里的代理：本机 http_proxy 反而更慢/不可靠，直连更稳。
    # 如果部署环境必须走代理，把 use_env_proxy 设 true，或在 proxy_url 里写死代理地址。
    use_env_proxy: bool = False
    proxy_url: str = ""

    # ---- TG 通道（pansou 的 searchTG 阶段）----
    # 逗号分隔的频道名。默认值与 pansou `config/config.go:164` 一致（tgsearchers7）。
    # 频道多不代表更好：每个频道一次请求，且都受可达性门控；线上直连 t.me 再启用。
    tg_channels: str = "tgsearchers7"

    @property
    def tg_channel_list(self) -> list[str]:
        return [c.strip().lstrip("@") for c in self.tg_channels.split(",") if c.strip()]

    # ---- 本地保护策略 ----
    cache_ttl: int = 300          # 搜索结果缓存秒数，同 query 不重复请求上游
    cache_max_entries: int = 512
    rate_limit_per_minute: int = 30   # 单 IP 每分钟搜索次数（滑动窗口，含票据接口）
    max_page_size: int = 50

    # ---- 防刷：匿名票据 ----
    # 不收个人信息，票据只按来源 IP 签发，绑定 IP + 短 TTL。
    # 密钥留空则从 API_KEY|IDR_SIGN 派生（重启不变）；换掉它 = 所有在途票据失效。
    ticket_secret: str = ""
    ticket_ttl: int = 300             # 票据有效期秒数，前端会提前自动续

    # ---- 埋点 ----
    # 只存本地 SQLite，不外发。data 目录跟着 backend/ 走，备份时一起打包即可。
    analytics_enabled: bool = True
    analytics_db: str = ""            # 留空 = backend/data/analytics.db
    analytics_tz_offset: int = 8      # 报表按哪个时区切天，默认北京时间
    analytics_retain_days: int = 180  # 保留天数，超期自动删
    analytics_rate_per_minute: int = 120  # 单 IP 每分钟上报事件上限，防刷

    # ---- 反馈 / 求资源 ----
    # 和埋点共用 backend/data/analytics.db，同一个文件备份一次就够。
    # 关掉埋点不影响反馈：一个是统计，一个是工单，别互相绑死。
    feedback_enabled: bool = True
    feedback_rate_per_10min: int = 5   # 单 IP 十分钟提交上限

    # ---- 链接存活检测 ----
    # 搜索返回后异步探活（夸克/UC/阿里/123，百度需带提取码），结果落
    # link_check 表，下次渲染时给卡片标"已失效/需提取码"。不用转存账号，
    # 打的是各网盘的匿名接口。关掉不影响搜索，只是卡片不再有核验标记。
    link_check_enabled: bool = True
    # 并发探活线程数。别调大：匿名接口没有配额说明，打急了会被当爬虫。
    link_check_workers: int = 3
    # 单条检测的超时秒数（连接超时固定 4 秒）
    link_check_timeout: float = 8.0
    # 每次搜索最多检多少条。只有第一页会检，20 条覆盖绝大多数点击
    link_check_per_page: int = 20
    # 确认失效的链接是否从结果里摘掉。关掉只在卡片上标"已失效"，
    # 排查误判时先关这个开关，别直接关掉整个检测。
    link_check_hide_bad: bool = True

    # ---- 群入口（进站 / 获取链接时弹二维码）----
    # 配置存在同一个 SQLite 里，/admin 看板可改，这个开关是总闸。
    group_enabled: bool = True

    # ---- 点击时转存（链接不外露）----
    # 用户点开资源时，用我们自己的网盘账号把上游分享转存一份，
    # 再把我们名下的分享链接给他。关掉则恢复成直接给上游原始链接。
    mirror_enabled: bool = True
    # 夸克 PC 网页版的登录 cookie。失效后所有转存都会失败（走回退），
    # 由于这是最后一跳，失效比签名过期更致命——见 README「点击时转存」。
    mirror_quark_cookie: str = ""
    # 单条资源允许转存的最大体积（GB）。**0 = 不限制**，默认就是 0。
    # 2026-11 取消了这个闸门：原来的 30G 是拍的，对免费号形同虚设、对会员号
    # 过度保守，还正好卡掉 4K 合集这类点击最好的资源。
    # 代价写在 README「容量」一节：账号空间的消耗从此只受"同时在库的副本数"约束，
    # 满了以后转存会集体失败并回退原链接。要重新收紧就填一个正数。
    mirror_max_gb: float = 0.0
    # 转存失败结果缓存秒数：避免每个点击都去打一次接口
    mirror_fail_ttl: int = 600
    # 两次转存之间的最小间隔（秒）。网盘对连续转存有风控，别调小
    mirror_min_interval: float = 3.0
    mirror_timeout: float = 15.0
    # 一次转存最多试几个账号。失败会换下一个，太快换也没意义，3 个够
    mirror_max_attempts: int = 3
    # 冷映射回收：超过这么多天零点击的映射，连文件一起删，把空间还回来
    mirror_cold_days: int = 30
    # 给用户的链接有效分钟数。竞品的做法是缓存 25–30 分钟就删源文件，
    # 逼用户当场转存到自己的网盘（夸克没有"30 分钟"这种分享有效期选项，
    # 只能我们自己删文件实现；分享本身按 1 天建，不是永久——见 quark/const.py）。
    # 到点后旧文件回收，再次点击会重新转存。
    mirror_link_minutes: int = 30
    # 我们建出去的那条分享**本身**的有效期（小时）。这是安全网，不是主闸门：
    # 正常由 mirror_link_minutes 到点删源文件让链接失效；这里只兜住"回收任务
    # 没跑"的情况（进程挂了、数据库丢了、账号被禁用），免得资源永久挂在我们
    # 账号上。各平台支持的最短档位不同，适配器会取最接近的一档（百度 1 天、
    # 夸克/迅雷/139 按天）。
    # **不要填 0**：百度 period=0 就是永久分享，正是这条配置要避免的事。
    # 取值范围见 _share_hours 校验器（24～168 小时，即 1～7 天）。
    mirror_share_hours: int = 24
    # /go/ 跳转 token 的签名密钥。留空则由 API_KEY 派生（重启不变）；
    # 换掉它 = 所有已发出的旧链接失效（会走回退，不会 404）
    mirror_secret: str = ""
    # 转存失败时是否回退到上游原始链接。关掉则给「资源暂不可用」
    mirror_fallback: bool = True

    # ---- 转存后的收尾 ----
    # 广告文件清理：源分享里常夹带"加群领福利"之类的文件，转存后会被一起
    # 分享给用户。默认**关闭**（竞品默认也是关的）——关键词配错会删掉正片，
    # 先在后台「手工验一条链接」跑一次、看过结果再开。
    mirror_ad_filter: bool = False
    # 转存**之前**跳过顶层广告附件。默认开：改的是待转清单，零请求零耗时，
    # 判据是"关键词 + 附件后缀 + 体积小"三条全中（见 sdk/adfilter.py）。
    # 想彻底关掉广告处理，这个和 MIRROR_AD_FILTER 都要关
    mirror_ad_preskip: bool = True
    # 命中即跳过/删除，逗号分隔，只匹配文件名（不匹配路径）。配太宽会误杀：
    # "资源""合集"这种词别放进来。这里刻意没有"广告""推广"——"广告设计素材"
    # 这类正片名会被它们误伤，而真正的引流附件几乎都带"公众号/加群"。
    mirror_ad_keywords: str = "微信,公众号,加群,进群,福利,防失联,更多资源,QQ群"
    # 往下探几层：1 = 只看转存内容的直接子项，2 = 再进一层子目录
    mirror_ad_depth: int = 2
    # 判定广告的体积上限（MB）。**三条全中才动手**：附件型后缀 + 名字命中
    # 关键词 + 体积小于这个值。0 = 不限体积（后缀和关键词仍然要过）。
    # 正片最低也有几十 MB，2MB 足够挡住 txt / 单张图片这类引流附件
    mirror_ad_max_mb: float = 2.0

    # 违规分享巡检：我们转存的内容被判违规后，分享会挂在账号上，代价是
    # 限流甚至封号。删文件撤不掉它，必须调撤销接口。默认开（不撤的损失
    # 比误撤大：这些账号只用来转存，没有"该留着的分享"）。
    mirror_violated_auto: bool = True
    # 巡检间隔（分钟）。竞品是 5 分钟，我们不需要那么勤：违规判定本身就是
    # 滞后的，30 分钟足够把风险压住，也不会把列表接口刷到风控
    mirror_violated_minutes: int = 30

    # ---- 看板口令 ----
    # 留空时 /admin 只允许本机访问（经 Nginx 反代后就是谁都不行），
    # 部署到公网必须设一个，否则看板打不开。
    admin_token: str = ""

    # ---- 服务 ----
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    serve_frontend: bool = True
    # 前端构建产物目录，留空则用 ../frontend/dist
    frontend_dist: str = ""

    @field_validator("api_key", "idr_sign", "idr_time", "proxy_url", mode="after")
    @classmethod
    def _sanitize(cls, value: str) -> str:
        """去掉首尾空白与不可见字符。

        这条不是洁癖：apikey 尾部多一个空格，上游会把它当成无效 key，
        返回 code=1005「Credits not enough」，看起来像余额不足，极难排查。
        实测已复现（正确 key + 尾随空格 → 1005）。
        """
        if not isinstance(value, str):
            return value
        value = value.strip()
        for ch in _INVISIBLE:
            value = value.replace(ch, "")
        return value.strip()

    @field_validator("mirror_share_hours", mode="after")
    @classmethod
    def _share_hours(cls, value: int) -> int:
        """把分享有效期夹到 24～168 小时（1 天～7 天）。

        下限是真的下限：0 传到百度就是 `period=0`＝**永久分享**，回收任务一旦
        没跑，资源就永久挂在账号上，而且没有任何本地记录能追（mirrors 行丢了
        就再也找不到要删什么）。而且这几家网盘最短的分享有效期都是 1 天，
        填比 24 小的数值只会被适配器悄悄抬到 1 天，不如在这里就说清楚。
        上限 168 是因为百度最长只给 7 天，填更大没有平台能兑现。

        **实际只有 24 和 168 两个有效档**：夸克只有 1/7/30 天三档、百度只有
        1/7 天、迅雷和 139 按天向上取整。所以填 25～167 之间的值不会得到"两三天"，
        而是被各家**静默抬到各自的下一档**（夸克/百度直接 7 天），并且因为那是
        首选档、不是降级档，日志里一条告警都不会有。要清清爽爽就只填 24 或 168。
        """
        try:
            hours = int(value)
        except (TypeError, ValueError):
            return 24
        return max(24, min(hours, 168))

    @property
    def api_key_problem(self) -> str:
        """返回 apikey 的可疑之处，空字符串表示正常。只描述问题，不泄露内容。"""
        key = self.api_key
        if not key:
            return "API_KEY 为空"
        if any(ch.isspace() for ch in key):
            return "API_KEY 中间含空白字符"
        if not key.startswith("idr_"):
            return "API_KEY 前缀不是 idr_"
        if len(key) != 36:
            return f"API_KEY 长度为 {len(key)}，正常应为 36"
        return ""

    @property
    def idr_sign_problem(self) -> str:
        """签名格式自检。只能查形状，查不出是否过期（过期要请求才知道）。"""
        sign = self.idr_sign
        if not sign:
            return "IDR_SIGN 为空，搜索会直接失败"
        if not sign.startswith("v1:"):
            return "IDR_SIGN 应以 v1: 开头"
        if len(sign) != 35:
            return f"IDR_SIGN 长度为 {len(sign)}，正常应为 35（v1: + 32 位十六进制）"
        return ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def analytics_path(self) -> str:
        """埋点库路径。默认 backend/data/analytics.db，跟 .env 一样不依赖 CWD。"""
        if self.analytics_db:
            return self.analytics_db
        return str(BACKEND_DIR / "data" / "analytics.db")

    @property
    def search_cache_path(self) -> str:
        """插件层搜索缓存（L2 SQLite）。和埋点同目录，备份一次全带走。

        不复用 main.py 那个 5 分钟的内存缓存：那个存的是"已经拼好的响应体"，
        这里存的是"每个源每次抓取的原始结果"，语义不同（后者带 complete 标记，
        能表达"上一轮没跑完，下次重抓"）。
        """
        return str(BACKEND_DIR / "data" / "search_cache.db")


@lru_cache
def get_settings() -> Settings:
    return Settings()
