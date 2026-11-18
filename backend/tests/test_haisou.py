"""haisou 插件（本项目官方 API 通道）的离线测试。

不碰网络：用一个假 client 顶替 httpx，按请求体返回上游那套 JSON 结构。
每条用例都对应一个"能跑通但结果是错的"的失效形态，不是"能不能跑"。

**并发结构是本文件的重点**：上游一次请求只给一个平台（`platforms` 数组实际不生效，
实测传 11 个平台仍全是夸克），所以插件按 pansou `haisou.go:142` 的双层循环
拆成 `平台 × 页` 个并发任务。这里断言的就是这个矩阵——请求数、平台覆盖面、
部分失败时其他平台仍可用。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_haisou -v``
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.providers import UpstreamError
from app.search.cache import SearchCache, _from_json, _to_json
from app.search.observe import HttpStatusError
from app.search.plugins import haisou as H
from app.search.pool import WorkerPool


# ---------------- 假配置 ----------------
#
# 插件和 providers 都是 `from ..config import get_settings` 直接绑定的，
# 所以要分别 patch 两处，只 patch app.config 不会生效。


class FakeSettings:
    idr_url = "https://idr.test/api/user/app/mapi"
    request_timeout = 75.0
    connect_timeout = 25.0
    max_page_size = 50
    api_key = "idr_test_key"
    idr_sign = "v1:0123456789abcdef0123456789abcdef"
    idr_time = "1700000000"
    # 空 platforms 时兜底的那个盘。默认夸克，和生产配置一致。
    default_platform = "quark"


# ---------------- 假客户端 ----------------


class FakeResponse:
    def __init__(self, status: int = 200, payload=None, text: str = ""):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.text = text or ""

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeClient:
    """handler(envelope) -> FakeResponse / Exception。

    `calls` 记录每一次请求，用来断言"有没有重试"「发起了几个并发任务」这类行为 ——
    官方 API 按请求计费，请求数直接和钱相关，结果对不代表没浪费额度。
    """

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        result = self.handler(json)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result

    def params(self, index: int = 0) -> dict:
        return self.calls[index]["json"]["params"]

    def platforms(self) -> list[str]:
        """每个请求各自要的平台（插件保证一次请求只带一个平台）。"""
        return [c["json"]["params"]["platforms"][0] for c in self.calls]

    def pages(self) -> list[int]:
        return [c["json"]["params"]["page"] for c in self.calls]


# ---------------- 假数据 ----------------


def body(items: list[dict], page: int = 1, page_size: int = 50, total: int | None = None,
         code: int = 0, msg: str = "ok") -> dict:
    total = len(items) if total is None else total
    return {
        "code": code,
        "msg": msg,
        "credits": 1,
        "result": {
            "items": items,
            "pagination": {
                "page": page,
                "pageSize": page_size,
                "total": total,
                "totalPages": max(1, (total + page_size - 1) // page_size),
            },
        },
    }


def item(hsid: str = "h1", platform: str = "quark", title: str = "三体 全集 4K",
         size: int = 1024**3, files: int = 3, code: str = "C1", pwd: str | None = "abcd",
         url: str | None = None) -> dict:
    platforms = {
        "quark": "夸克网盘", "baidu": "百度网盘", "ali": "阿里云盘",
        "uc": "UC网盘", "tianyi": "天翼云盘", "xunlei": "迅雷网盘",
    }
    if url is None:
        url = f"https://pan.quark.cn/s/{code}" if platform == "quark" else f"https://example.test/{code}"
    return {
        "hsid": hsid,
        "platform": platform,
        "platformName": platforms.get(platform, "其他"),
        "title": title,
        "shareUrl": url,
        "shareCode": code,
        "sharePwd": pwd,
        "fileCount": files,
        "sizeBytes": size,
        "indexedTime": "",
    }


def fake_upstream(pwd: str | None = "abcd", files: int = 3, size: int = 1024**3):
    """模拟真实上游：**一次请求只返回它被要求的那个平台**的数据。

    这是本插件全部复杂度的来源，mock 必须还原它，否则"结果平台混不混合"测不出来。
    """
    def handler(env):
        params = env["params"]
        platform = params["platforms"][0]
        page = params["page"]
        key = f"{platform}{page}"
        return FakeResponse(200, body(
            [item(key, platform=platform, code=key, pwd=pwd, files=files, size=size,
                  url=f"https://pan.{platform}.test/s/{key}")],
            page=page,
        ))
    return handler


def run(coro):
    return asyncio.run(coro)


class HaisouBase(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            patch("app.search.plugins.haisou.get_settings", return_value=FakeSettings()),
            patch("app.providers.get_settings", return_value=FakeSettings()),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patchers])

    def make(self, handler) -> tuple[H.HaisouPlugin, FakeClient]:
        return H.HaisouPlugin(), FakeClient(handler)


# ---------------- 纯函数 ----------------


class TestPureFunctions(unittest.TestCase):
    def test_link_type_maps_ali_to_aliyun(self):
        self.assertEqual(H.link_type("ali"), "aliyun")
        self.assertEqual(H.link_type("quark"), "quark")
        # 不认识的平台保留原值，不丢信息
        self.assertEqual(H.link_type("somewhere"), "somewhere")
        self.assertEqual(H.link_type(""), "")

    def test_format_size_units(self):
        self.assertEqual(H.format_size(0), "0 B")
        self.assertEqual(H.format_size(1023), "1023 B")
        self.assertEqual(H.format_size(1024), "1.00 KB")
        self.assertEqual(H.format_size(1024**3), "1.00 GB")
        self.assertEqual(H.format_size(int(1.5 * 1024**4)), "1.50 TB")

    def test_as_int_guards_bool_and_clamps(self):
        self.assertEqual(H._as_int(True, 7, 1, 10), 7)      # bool 不是数字
        self.assertEqual(H._as_int("3", 7, 1, 10), 7)       # 字符串不认
        self.assertEqual(H._as_int(3.9, 7, 1, 10), 3)       # JSON 数字可能是 float
        self.assertEqual(H._as_int(0, 7, 1, 10), 1)         # 下限
        self.assertEqual(H._as_int(999, 7, 1, 10), 10)      # 上限

    def test_as_platforms_accepts_list_and_csv(self):
        self.assertEqual(H._as_platforms(["quark", " baidu "]), ["quark", "baidu"])
        self.assertEqual(H._as_platforms("quark,baidu"), ["quark", "baidu"])
        self.assertEqual(H._as_platforms(""), [])
        self.assertEqual(H._as_platforms(None), [])
        self.assertEqual(H._as_platforms({"a": 1}), [])

    def test_supported_platforms_match_pansou(self):
        """照抄 `haisou.go:29 SupportedCloudTypes`，改这个列表等于改覆盖面。"""
        self.assertEqual(H.SUPPORTED_PLATFORMS, ["ali", "baidu", "quark", "xunlei", "tianyi"])
        self.assertEqual(H.DEFAULT_PAGES_PER_PLATFORM, 2)   # DefaultPagesPerType
        self.assertEqual(H.MAX_PAGES_PER_PLATFORM, 3)       # MaxAllowedPagesPerType


# ---------------- 并发矩阵：平台 × 页 ----------------

class TestTaskMatrix(HaisouBase):
    def test_default_is_single_platform(self):
        """空 platforms 只兜默认盘 = 2 个请求。

        这是**额度底线**：以前这里会展开成 5 平台 × 2 页 = 10 个请求，
        用户随手点一下搜索就烧 5 倍额度。改回全平台会让这条直接挂掉。
        """
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {}))

        self.assertEqual(len(client.calls), H.DEFAULT_PAGES_PER_PLATFORM)
        self.assertEqual(set(client.platforms()), {FakeSettings.default_platform})
        self.assertEqual(sorted(client.pages()), [1, 2])

    def test_each_request_carries_exactly_one_platform(self):
        """一次请求只带一个平台：上游对多平台数组只肯返回优先级最高的那个。"""
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {}))
        for call in client.calls:
            self.assertEqual(len(call["json"]["params"]["platforms"]), 1)

    def test_multi_platform_selection_mixes_results(self):
        """用户自己勾了多个盘，才按 `平台 × 页` 矩阵并发查，结果才会混合。

        默认单盘时结果理应只有那一个盘 —— 混合是"用户要多"的代价，不是默认行为。
        """
        plugin, client = self.make(fake_upstream())
        results = run(plugin.fetch(client, "三体", {
            "platforms": H.SUPPORTED_PLATFORMS, "pages_per_type": 1,
        }))

        self.assertEqual(len(client.calls), len(H.SUPPORTED_PLATFORMS))
        self.assertEqual(
            sorted({r.extras["platform"] for r in results}),
            sorted(H.SUPPORTED_PLATFORMS),
        )

    def test_explicit_platforms_only_queried(self):
        """用户勾了平台就只查勾的那些，别偷偷多打请求（每次都是钱）。"""
        plugin, client = self.make(fake_upstream())
        results = run(plugin.fetch(client, "三体", {"platforms": ["baidu"]}))

        self.assertEqual(len(client.calls), 2)              # 1 平台 × 2 页
        self.assertEqual(set(client.platforms()), {"baidu"})
        self.assertEqual({r.extras["platform"] for r in results}, {"baidu"})

    def test_pages_per_type_from_ext(self):
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {"pages_per_type": 1}))
        self.assertEqual(len(client.calls), 1)              # 1 平台 × 1 页
        self.assertEqual(client.pages(), [1])

    def test_pages_per_type_capped(self):
        """pansou 的 MaxAllowedPagesPerType：超过 3 页要夹住，否则一次搜索打穿额度。"""
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {"pages_per_type": 99}))
        self.assertEqual(len(client.calls), H.MAX_PAGES_PER_PLATFORM)


# ---------------- 正常流程 ----------------

class TestFlow(HaisouBase):
    def test_result_fields(self):
        plugin, client = self.make(fake_upstream())
        results = run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        r = results[0]

        self.assertEqual(r.unique_id, "haisou-quark1")
        self.assertEqual(r.title, "三体 全集 4K")
        self.assertEqual(r.links[0].type, "quark")
        self.assertEqual(r.links[0].url, "https://pan.quark.test/s/quark1")
        self.assertEqual(r.links[0].password, "abcd")
        self.assertEqual(r.channel, "")          # 插件结果必须留空
        self.assertGreater(r.datetime, 0)

    def test_ali_link_type_is_aliyun(self):
        """上游叫 ali，pansou 内部叫 aliyun，混用会让前端筛选漏数据。"""
        plugin, client = self.make(fake_upstream())
        results = run(plugin.fetch(client, "三体", {"platforms": ["ali"], "pages_per_type": 1}))
        self.assertEqual(results[0].links[0].type, "aliyun")

    def test_content_shape(self):
        """content 与 pansou 版同口径，两个通道出来的卡片长得一样。"""
        plugin, client = self.make(fake_upstream(files=12, size=2 * 1024**3))
        r = run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))[0]
        self.assertIn("文件数量: 12", r.content)
        self.assertIn("网盘类型: 夸克网盘", r.content)
        self.assertIn("大小: 2.00 GB", r.content)

    def test_extras_carry_upstream_fields(self):
        """前端卡片要 hsid / 文件数 / 体积，这些只能靠 extras 带出去。"""
        plugin, client = self.make(fake_upstream(files=3, size=1024**3))
        r = run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))[0]

        self.assertEqual(r.extras["hsid"], "quark1")
        self.assertEqual(r.extras["platform"], "quark")
        self.assertEqual(r.extras["platformName"], "夸克网盘")
        self.assertEqual(r.extras["shareCode"], "quark1")
        self.assertEqual(r.extras["shareUrl"], "https://pan.quark.test/s/quark1")
        self.assertEqual(r.extras["sharePwd"], "abcd")
        self.assertEqual(r.extras["fileCount"], 3)
        self.assertEqual(r.extras["sizeBytes"], 1024**3)

    def test_empty_password_kept_as_empty_string(self):
        """无码链接必须保留，不能变成 "None"。"""
        plugin, client = self.make(fake_upstream(pwd=None))
        results = run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(results[0].links[0].password, "")

    def test_password_with_leading_zeros_preserved(self):
        plugin, client = self.make(fake_upstream(pwd="0000"))
        results = run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(results[0].links[0].password, "0000")

    def test_item_without_url_is_skipped(self):
        plugin, client = self.make(
            lambda env: FakeResponse(200, body([item("h1"), item("h2", url="")]))
        )
        results = run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual([r.unique_id for r in results], ["haisou-h1"])

    def test_empty_items_returns_empty_not_error(self):
        """200 + code 0 + 空数组 = 真零结果，不是失败。"""
        plugin, client = self.make(lambda env: FakeResponse(200, body([])))
        self.assertEqual(run(plugin.fetch(client, "三体", {})), [])

    def test_missing_items_with_zero_total_is_empty(self):
        """有的实现空结果不返回 items，只有 total=0 —— 按零结果处理，不报结构变化。"""
        payload = {"code": 0, "msg": "ok", "result": {"pagination": {"total": 0}}}
        plugin, client = self.make(lambda env: FakeResponse(200, payload))
        self.assertEqual(run(plugin.fetch(client, "三体", {})), [])


# ---------------- 参数 ----------------

class TestParams(HaisouBase):
    def test_default_params(self):
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))

        params = client.params()
        self.assertEqual(params["query"], "三体")
        self.assertEqual(params["searchIn"], "title")
        self.assertEqual(params["page"], 1)
        self.assertEqual(params["pageSize"], H.DEFAULT_PAGE_SIZE)
        self.assertEqual(params["platforms"], ["quark"])
        self.assertEqual(client.calls[0]["json"]["path"], "/api/b9d1/search")

    def test_ext_overrides(self):
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {
            "page_size": 20, "platforms": ["quark", "baidu"], "scope": "all",
            "pages_per_type": 1,
        }))

        self.assertEqual(client.params()["pageSize"], 20)
        self.assertEqual(client.params()["searchIn"], "all")
        self.assertEqual(sorted(client.platforms()), ["baidu", "quark"])

    def test_page_size_clamped_to_settings_max(self):
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {"page_size": 999, "pages_per_type": 1}))
        self.assertEqual(client.params()["pageSize"], FakeSettings.max_page_size)

    def test_scope_falls_back_to_title(self):
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {"scope": "weird", "pages_per_type": 1}))
        self.assertEqual(client.params()["searchIn"], "title")


# ---------------- 计费相关 ----------------

class TestBilling(HaisouBase):
    def test_timeout_is_request_timeout_not_client_default(self):
        """官方要求 > 60 秒，否则提前中断仍计费。共享客户端默认 30 秒，必须覆盖。"""
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {}))
        self.assertEqual(client.calls[0]["timeout"], FakeSettings.request_timeout)
        self.assertGreater(client.calls[0]["timeout"], 60)

    def test_headers_carry_signature(self):
        plugin, client = self.make(fake_upstream())
        run(plugin.fetch(client, "三体", {}))
        headers = client.calls[0]["headers"]
        self.assertEqual(headers["x-idr-sign"], FakeSettings.idr_sign)
        self.assertEqual(headers["x-idr-time"], FakeSettings.idr_time)
        self.assertIn("User-Agent", headers)


# ---------------- 失败必须与"零结果"分开 ----------------

class TestFailureSemantics(HaisouBase):
    def test_404_not_retried(self):
        """404 是接口已删。重试等于白花两次钱。"""
        plugin, client = self.make(lambda env: FakeResponse(404, {}, "not found"))
        with self.assertRaises(HttpStatusError) as ctx:
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(len(client.calls), 1)

    def test_429_is_retried(self):
        """429 是限流，可恢复，必须重试。"""
        seen = {"n": 0}

        def handler(env):
            seen["n"] += 1
            if seen["n"] == 1:
                return FakeResponse(429, {}, "slow down")
            return FakeResponse(200, body([item()]))

        plugin, client = self.make(handler)
        results = run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(client.calls), 2)

    def test_1003_frequency_limit_is_retried(self):
        """200 + code 1003（i18n:server_too_frequency）是限流，不是"零结果"也不是源故障。

        实测 10 并发时 10 个里有 2 个被打回；当它不可重试时，这两个平台页的数据
        直接消失，用户看到"夸克只有 50 条"却查不出原因。
        """
        seen = {"n": 0}

        def handler(env):
            seen["n"] += 1
            if seen["n"] == 1:
                return FakeResponse(200, {"code": 1003, "msg": "i18n:server_too_frequency"})
            return FakeResponse(200, body([item()]))

        plugin, client = self.make(handler)
        results = run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))

        self.assertEqual(len(results), 1)
        self.assertEqual(len(client.calls), 2)

    def test_1003_exhausted_is_429(self):
        """限流重试耗尽要归到 RATE_LIMITED，不能落到 OTHER。"""
        plugin, client = self.make(
            lambda env: FakeResponse(200, {"code": 1003, "msg": "too_frequency"})
        )
        with self.assertRaises(HttpStatusError) as ctx:
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(len(client.calls), H.MAX_RETRIES)

    def test_500_exhausts_retries_and_keeps_status(self):
        """重试耗尽要抛原始 HttpStatusError，包成 RuntimeError 就丢掉分类能力了。"""
        plugin, client = self.make(lambda env: FakeResponse(500, {}, "boom"))
        with self.assertRaises(HttpStatusError) as ctx:
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(len(client.calls), H.MAX_RETRIES)

    def test_signature_expired_is_403_and_not_retried(self):
        """1004 签名过期：不可恢复，转 403 让它归到 FORBIDDEN 而不是 OTHER。"""
        plugin, client = self.make(
            lambda env: FakeResponse(200, {"code": 1004, "msg": "illegal request"})
        )
        with self.assertRaises(HttpStatusError) as ctx:
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("1004", str(ctx.exception))
        self.assertEqual(len(client.calls), 1)      # 不能重试

    def test_apikey_invalid_is_403_and_not_retried(self):
        plugin, client = self.make(
            lambda env: FakeResponse(200, {"code": 1005, "msg": "Credits not enough"})
        )
        with self.assertRaises(HttpStatusError) as ctx:
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(client.calls), 1)

    def test_one_platform_failing_keeps_other_platforms(self):
        """pansou 的 successTasks>0 语义：百度挂了不该让另外四个盘的结果一起消失。"""
        ok = fake_upstream()

        def handler(env):
            if env["params"]["platforms"][0] == "baidu":
                return FakeResponse(500, {}, "boom")
            return ok(env)

        plugin, client = self.make(handler)
        results = run(plugin.fetch(client, "三体", {
            "platforms": H.SUPPORTED_PLATFORMS, "pages_per_type": 1,
        }))

        self.assertEqual(len(results), 4)
        self.assertNotIn("baidu", {r.extras["platform"] for r in results})

    def test_all_tasks_failed_raises_original(self):
        """全部任务失败才报错（pansou `successTasks == 0`），且抛原始异常。"""
        plugin, client = self.make(lambda env: FakeResponse(403, {}, "forbidden"))
        with self.assertRaises(HttpStatusError) as ctx:
            run(plugin.fetch(client, "三体", {"pages_per_type": 1}))
        self.assertEqual(ctx.exception.status, 403)
        # 403 不可恢复：默认 1 个平台 1 页 = 1 次请求就停，不做无意义的翻页重试
        self.assertEqual(len(client.calls), 1)

    def test_other_code_keeps_original_code(self):
        """1001 之类的业务错误：保留 code，日志里能看出是参数被拒。"""
        plugin, client = self.make(
            lambda env: FakeResponse(200, {"code": 1001, "msg": "bad param"})
        )
        with self.assertRaises(UpstreamError) as ctx:
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(ctx.exception.code, 1001)

    def test_result_not_object_raises(self):
        """200 + code 0 但 result 不是对象 = 结构变了。"""
        plugin, client = self.make(
            lambda env: FakeResponse(200, {"code": 0, "result": "oops"})
        )
        with self.assertRaises(ValueError):
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))

    def test_items_not_array_raises(self):
        """items 变成字符串时不能被 `.get("items") or []` 兜成空列表。"""
        plugin, client = self.make(
            lambda env: FakeResponse(200, {"code": 0, "result": {"items": "oops"}})
        )
        with self.assertRaises(ValueError):
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))

    def test_total_positive_without_items_raises(self):
        payload = {"code": 0, "result": {"pagination": {"total": 10}}}
        plugin, client = self.make(lambda env: FakeResponse(200, payload))
        with self.assertRaises(ValueError):
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))

    def test_all_items_without_url_raises(self):
        """有 2 条但一条 shareUrl 都没有 = 字段改名，不是"没内容"。"""
        plugin, client = self.make(
            lambda env: FakeResponse(200, body([item("h1", url=""), item("h2", url="")]))
        )
        with self.assertRaises(ValueError) as ctx:
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertIn("shareUrl", str(ctx.exception))

    def test_non_json_raises_value_error(self):
        plugin, client = self.make(
            lambda env: FakeResponse(200, ValueError("not json"), "<html>error</html>")
        )
        with self.assertRaises(ValueError):
            run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))
        self.assertEqual(len(client.calls), 1)      # 非 JSON 不重试


# ---------------- 与 BasePlugin / 缓存的集成 ----------------

class TestWithBasePlugin(HaisouBase):
    def test_search_writes_cache_and_second_call_hits_it(self):
        """走完整 search()：缓存写入、outcome 标记、二次命中都要对。"""
        plugin, client = self.make(fake_upstream())
        plugin.cache = SearchCache()          # 纯内存
        plugin.pool = WorkerPool(max_workers=4, max_tasks=8)
        plugin.client_fast = client
        plugin.client_long = client

        async def scenario():
            return await plugin.search("三体"), await plugin.search("三体")

        first, second = run(scenario())

        self.assertTrue(first.is_final)
        self.assertIsNone(first.failure)
        self.assertEqual(first.source, "haisou")
        # 默认单盘 × 2 页 = 2 条，2 个计费请求
        self.assertEqual(len(first.results), 2)
        self.assertEqual(len(second.results), 2)
        # 第二次命中缓存，不再计费
        self.assertEqual(len(client.calls), 2)

    def test_response_timeout_fits_the_whole_batch(self):
        """10 并发实测 6.8 秒，窗口必须容得下整批，否则用户拿到空结果。"""
        self.assertEqual(H.HaisouPlugin.response_timeout, 10.0)

    def test_level_is_primary(self):
        """官方通道是唯一有 apikey + 分页的主源，等级分必须压过匿名源。"""
        self.assertEqual(H.HaisouPlugin.level, 1)

    def test_extras_survive_cache_roundtrip(self):
        """缓存回读丢了 extras，二次命中时卡片就没有 hsid/体积了。"""
        plugin, client = self.make(fake_upstream())
        r = run(plugin.fetch(client, "三体", {"platforms": ["quark"], "pages_per_type": 1}))[0]

        restored = _from_json(_to_json([r]))[0]
        self.assertEqual(restored.extras, r.extras)

    def test_old_cache_without_extras_does_not_crash(self):
        """extras 上线前写进库的条目没有这个键，不能因此整条缓存被判损坏。"""
        payload = (
            '[{"unique_id":"haisou-h1","title":"旧条目","links":[],"tags":[],"images":[],"datetime":1}]'
        )
        restored = _from_json(payload)[0]
        self.assertEqual(restored.extras, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
