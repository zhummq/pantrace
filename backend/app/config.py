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

    # ---- 网络 ----
    # 默认不使用系统环境变量里的代理：本机 http_proxy 反而更慢/不可靠，直连更稳。
    # 如果部署环境必须走代理，把 use_env_proxy 设 true，或在 proxy_url 里写死代理地址。
    use_env_proxy: bool = False
    proxy_url: str = ""

    # ---- 本地保护策略 ----
    cache_ttl: int = 300          # 搜索结果缓存秒数，同 query 不重复请求上游
    cache_max_entries: int = 512
    rate_limit_per_minute: int = 30   # 单 IP 每分钟搜索次数（预留，未实现）
    max_page_size: int = 50

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

    # ---- 群入口（进站 / 获取链接时弹二维码）----
    # 配置存在同一个 SQLite 里，/admin 看板可改，这个开关是总闸。
    group_enabled: bool = True

    # ---- 自有精选链接 ----
    # 站方维护的网盘资源库：搜索命中时置顶展示，未命中词落日志。
    # 和埋点、反馈共用同一个 SQLite，开关独立——关掉只影响精选卡。
    own_enabled: bool = True

    # ---- 点击时转存（链接不外露）----
    # 用户点开资源时，用我们自己的网盘账号把上游分享转存一份，
    # 再把我们名下的分享链接给他。关掉则恢复成直接给上游原始链接。
    mirror_enabled: bool = True
    # 夸克 PC 网页版的登录 cookie。失效后所有转存都会失败（走回退），
    # 由于这是最后一跳，失效比签名过期更致命——见 README「点击时转存」。
    mirror_quark_cookie: str = ""
    # 单条资源允许转存的最大体积。超过就不转、直接给原链接。
    # 这是容量闸门：转存永久占用账号空间，且分享出去后不能删源文件。
    # 现在由各网盘 adapter 的 max_file_bytes 管，这里只留兼容默认值
    mirror_max_gb: float = 30.0
    # 转存失败结果缓存秒数：避免每个点击都去打一次接口
    mirror_fail_ttl: int = 600
    # 两次转存之间的最小间隔（秒）。网盘对连续转存有风控，别调小
    mirror_min_interval: float = 3.0
    mirror_timeout: float = 15.0
    # 一次转存最多试几个账号。失败会换下一个，太快换也没意义，3 个够
    mirror_max_attempts: int = 3
    # 冷映射回收：超过这么多天零点击的映射，连文件一起删，把空间还回来
    mirror_cold_days: int = 30
    # /go/ 跳转 token 的签名密钥。留空则由 API_KEY 派生（重启不变）；
    # 换掉它 = 所有已发出的旧链接失效（会走回退，不会 404）
    mirror_secret: str = ""
    # 转存失败时是否回退到上游原始链接。关掉则给「资源暂不可用」
    mirror_fallback: bool = True

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
