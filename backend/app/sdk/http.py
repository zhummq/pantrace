"""统一请求层。

只做五件事，多一件都不做：拼 base_url、注入公共头、回收 Set-Cookie、
网络层重试、把非 2xx 变成语义异常。**业务码校验不在这里**——各网盘的
成功码语义不同（夸克看 code=0，百度看 errno=0，迅雷看 code=0 但字段名
不一样），硬塞进通用层只会写出一个到处 if 的怪物。

两个必须保留的细节，都是踩出来的：

1. `trust_env = False`。本机开着代理时，requests 会读环境变量走代理，
   表现为"读超时"而不是连接失败，排查半天看不出是代理。providers 那边
   早就有这条，别删。
2. Session 用 threading.local 每个线程一个。requests.Session 不是线程安全的
   （cookie jar 会竞争），但每次请求新建 Session 又丢掉连接复用——转存一次
   要打六七次同一个域名，复用很值。折中是每线程一个，既安全又能复用。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter

from . import cookie as cookie_util
from .errors import NetdiskError, UpstreamError, message_of

log = logging.getLogger("pantrace.sdk")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 只对这些状态码重试。4xx 除了 429 都是确定性失败，重试纯属浪费配额
_RETRY_STATUS = {429, 500, 502, 503, 504}


class Requester:
    """一个网盘客户端的请求入口。

    on_cookie 在收到 Set-Cookie 时回调，用来把刷新后的登录态写回账号表。
    夸克的 __puus 就是这么续的，不续就会在半小时后集体 401。
    """

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        default_params: Mapping[str, str] | None = None,
        timeout: float = 15.0,
        on_cookie: Callable[[str], None] | None = None,
        pool_size: int = 20,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        # 夸克/UC 要求每个请求都带 pr + fr=pc，缺了会返回参数错误。
        # 放在这里而不是每个调用点手写：漏一个就是一次难查的 400
        self.default_params = dict(default_params or {})
        self.timeout = timeout
        self.on_cookie = on_cookie
        self.pool_size = max(2, int(pool_size))
        self._local = threading.local()

    # ---- 连接 ----

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        session = requests.Session()
        session.trust_env = False
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=self.pool_size, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        self._local.session = session
        return session

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
            self._local.session = None

    # ---- 地址 ----

    def url_for(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(self.base_url + "/", path.lstrip("/"))

    # ---- 主入口 ----

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        retries: int = 2,
        action: str = "请求",
        gone_error: type[NetdiskError] | None = None,
    ) -> dict[str, Any]:
        """发一次请求，返回解析后的 JSON（dict）；非 JSON 响应返回 {"_text": ...}。

        retries 是**网络层**重试次数，不含业务重试。业务重试由上层决定，
        因为只有上层知道"这个错换账号有没有用"。

        gone_error：404 / 410 该抛什么。分享侧传 ShareInvalidError，
        业务层才能判断"是源链接死了"而不是"我的账号挂了"——
        否则后台验链只会报一句 HTTP 404，看不出该修哪一头。
        """
        url = self.url_for(path)
        merged = {**self.headers, **(headers or {})}
        if self.default_params:
            params = {**self.default_params, **(params or {})}
        attempt = 0
        last_exc: Exception | None = None

        while attempt <= retries:
            attempt += 1
            started = time.monotonic()
            try:
                response = self._session().request(
                    method.upper(),
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=merged,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt > retries:
                    raise UpstreamError(f"{action}失败：网络不通（{exc.__class__.__name__}）") from exc
                time.sleep(min(0.4 * attempt, 1.5))
                continue

            self._absorb_cookie(response)

            if response.status_code in _RETRY_STATUS and attempt <= retries:
                last_exc = UpstreamError(f"{action}失败：HTTP {response.status_code}")
                time.sleep(min(0.4 * attempt, 1.5))
                continue

            cost = int((time.monotonic() - started) * 1000)
            if response.status_code >= 400:
                if response.status_code in (404, 410) and gone_error is not None:
                    raise gone_error(
                        f"{action}失败：资源不存在或已失效",
                        code=response.status_code,
                        raw=response.text[:300],
                    )
                raise UpstreamError(
                    f"{action}失败：HTTP {response.status_code}",
                    code=response.status_code,
                    raw=response.text[:300],
                )
            log.debug("%s %s %sms", method.upper(), _short(url), cost)

            if not response.content:
                return {}
            try:
                payload = response.json()
            except ValueError:
                return {"_text": response.text[:2000]}
            if isinstance(payload, list):
                return {"_list": payload}
            if not isinstance(payload, dict):
                return {"_value": payload}
            return payload

        raise UpstreamError(f"{action}失败：重试 {retries} 次仍未成功") from last_exc

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self.request("POST", path, **kwargs)

    # ---- cookie 回收 ----

    def _absorb_cookie(self, response: requests.Response) -> None:
        if self.on_cookie is None:
            return
        raw = response.raw.headers.getlist("Set-Cookie") if response.raw else None
        fresh = cookie_util.from_set_cookie(raw)
        if not fresh:
            return
        try:
            self.on_cookie(fresh)
        except Exception:  # noqa: BLE001
            # 回调失败不能影响主流程：cookie 没存上是小问题，请求挂了是大问题
            log.warning("cookie 回写失败")


def _short(url: str) -> str:
    """日志里只留路径，去掉 query。query 里经常带 stoken / sign。"""
    base, _, _ = url.partition("?")
    return base[-80:]


def check_code(
    payload: Mapping[str, Any],
    *,
    ok_codes: tuple[object, ...] = (0, "0"),
    action: str = "请求",
) -> Mapping[str, Any]:
    """校验业务成功码并返回 data 段。

    各网盘的成功码和数据结构都不一样，这里只处理最简单的一种：
    `{"code": 0, "data": {...}}`。结构特殊的（百度 errno、迅雷 code+error）
    在自己的 api 类里各写各的，别硬套。
    """
    if not isinstance(payload, Mapping):
        raise UpstreamError(f"{action}失败：响应不是对象")
    code = payload.get("code", payload.get("errno"))
    if str(code) not in {str(c) for c in ok_codes}:
        detail = message_of(payload) or f"code={code}"
        raise UpstreamError(f"{action}失败：{detail}", code=code, raw=payload)
    data = payload.get("data")
    return data if isinstance(data, Mapping) else {}
