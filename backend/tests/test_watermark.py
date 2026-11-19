"""转存引流水印的接线测试。

adfilter 那套管"广告判据对不对"，这里管"水印接线对不对"：水印 fid 有没有
进 fids（这样才随分享出去、随回收删掉）、关掉时是不是零动作、建失败会不会
拖垮一条本来成功的转存、不支持的盘是不是自动跳过。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_watermark -v``
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import pan
from app.sdk.base import PanClient
from app.sdk.types import File, ShareInfo, ShareLink


class _FakeClient(PanClient):
    """不发网络请求的假网盘，只把 mirror 流程跑完。"""

    type = "fake"
    display = "假盘"

    def __init__(self, *, watermark_name="", watermark_result="wm-fid", make_raises=False):
        self.account = None  # type: ignore[assignment]
        self.timeout = 5.0
        self.on_cookie = None
        self.max_file_bytes = 0
        self.ad_keywords = []
        self.ad_max_bytes = 0
        self.watermark_name = watermark_name
        self._wm_result = watermark_result
        self._make_raises = make_raises
        self.shared_fids: list[str] = []
        self.make_calls: list[tuple[str, str]] = []
        # mirror 里 _with_watermark 读 self.account.temp_dir，给个最小对象
        from app.sdk.types import Account
        self.account = Account(id=1, type="fake", temp_dir="tmpdir")

    def share_info(self, url, pwd="") -> ShareInfo:
        return ShareInfo(
            platform="fake", title="测试", files=[File(fid="f1", name="正片.mp4", size=100)],
            total_size=100,
        )

    def transfer(self, info, to_dir="") -> list[str]:
        return ["new-fid"]

    def create_share(self, fids, title, expire_hours=24) -> ShareLink:
        self.shared_fids = list(fids)
        return ShareLink(url="https://example.com/s/1")

    def list_files(self, parent_id="", page=1, size=50):
        return []

    def delete(self, fids) -> None:
        return None

    def make_watermark(self, name, to_dir="") -> str:
        self.make_calls.append((name, to_dir))
        if self._make_raises:
            raise RuntimeError("上游建目录挂了")
        return self._wm_result


class _NoWatermarkClient(_FakeClient):
    """模拟不支持建目录的盘：make_watermark 走基类默认实现（返回空）。"""

    def make_watermark(self, name, to_dir="") -> str:  # noqa: D102
        return super(PanClient, self).make_watermark(name, to_dir)


class TestWatermarkWiring(unittest.TestCase):
    def test_watermark_fid_joins_fids_and_link(self):
        client = _FakeClient(watermark_name="资源搜索网址panseek.xyz")
        link = client.mirror("https://example.com/s/x")

        # 建水印用的是落点目录
        self.assertEqual(client.make_calls, [("资源搜索网址panseek.xyz", "tmpdir")])
        # 分享时水印 fid 已经在清单里 → 用户能看到
        self.assertEqual(client.shared_fids, ["new-fid", "wm-fid"])
        # link.fids 也带着它 → 30 分钟回收会连它一起删
        self.assertEqual(link.fids, ["new-fid", "wm-fid"])

    def test_empty_name_disables_entirely(self):
        for name in ("", "   "):
            with self.subTest(name=name):
                client = _FakeClient(watermark_name=name)
                link = client.mirror("https://example.com/s/x")
                self.assertEqual(client.make_calls, [])
                self.assertEqual(link.fids, ["new-fid"])

    def test_make_failure_does_not_break_transfer(self):
        """水印是锦上添花：建失败也照样给用户转存好的链接。"""
        client = _FakeClient(watermark_name="水印", make_raises=True)
        link = client.mirror("https://example.com/s/x")
        self.assertEqual(link.fids, ["new-fid"])
        self.assertEqual(client.shared_fids, ["new-fid"])

    def test_empty_fid_result_skips(self):
        """建目录返回空 fid 视为没建，不往 fids 里塞空串。"""
        client = _FakeClient(watermark_name="水印", watermark_result="")
        link = client.mirror("https://example.com/s/x")
        self.assertEqual(link.fids, ["new-fid"])

    def test_unsupported_platform_skips(self):
        """基类默认返回空串：光鸭/139 这些盘转存照常，只是没有水印。"""
        client = _NoWatermarkClient(watermark_name="水印")
        link = client.mirror("https://example.com/s/x")
        self.assertEqual(link.fids, ["new-fid"])


class TestBaiduWatermark(unittest.TestCase):
    """百度的落点算法和返回形态，local 用 mock 验证，不发网络请求。"""

    def _client(self, temp_dir="pantrace"):
        from unittest import mock
        from app.sdk.adapters.baidu.client import BaiduClient
        from app.sdk.types import Account

        with mock.patch.object(BaiduClient, "_build_requester", return_value=mock.Mock()):
            client = BaiduClient(Account(id=1, type="baidu", temp_dir=temp_dir))
        client.openapi = mock.Mock()
        return client

    def test_returns_relative_path_in_temp_dir(self):
        from unittest import mock

        client = self._client("pantrace")
        with mock.patch.object(client, "_dir_exists", return_value=False), \
             mock.patch.object(client, "bdstoken", return_value="tk"), \
             mock.patch.object(client.http, "post", return_value={"errno": 0}) as post:
            result = client.make_watermark("资源搜索网址panseek.xyz")

        # 返回相对路径（不带前导斜杠），和 transfer 的 fids 同形态
        self.assertEqual(result, "pantrace/资源搜索网址panseek.xyz")
        # 建在落点目录里，isdir=1
        self.assertEqual(post.call_args.kwargs["data"]["path"], "/pantrace/资源搜索网址panseek.xyz")
        self.assertEqual(post.call_args.kwargs["data"]["isdir"], 1)

    def test_existing_dir_is_reused_not_recreated(self):
        """create 不幂等（撞名会改名），已存在就必须跳过。"""
        from unittest import mock

        client = self._client("pantrace")
        with mock.patch.object(client, "_dir_exists", return_value=True), \
             mock.patch.object(client.http, "post") as post:
            result = client.make_watermark("资源搜索网址panseek.xyz")
        post.assert_not_called()
        self.assertEqual(result, "pantrace/资源搜索网址panseek.xyz")

    def test_root_temp_dir(self):
        """temp_dir 为空 = 根目录，水印直接落根下。"""
        from unittest import mock

        client = self._client("")
        with mock.patch.object(client, "_dir_exists", return_value=False), \
             mock.patch.object(client, "bdstoken", return_value="tk"), \
             mock.patch.object(client.http, "post", return_value={"errno": 0}) as post:
            result = client.make_watermark("资源搜索网址panseek.xyz")
        self.assertEqual(result, "资源搜索网址panseek.xyz")
        self.assertEqual(post.call_args.kwargs["data"]["path"], "/资源搜索网址panseek.xyz")


class TestWatermarkSettings(unittest.TestCase):
    """后台改水印名的存储层：热更、库覆盖 .env、输入校验。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "settings.db"
        pan.init(self.db, watermark_name="资源搜索网址panseek.xyz")

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_hot_updates(self):
        pan.save_watermark_settings(name="网址水印 A")
        self.assertEqual(pan.watermark_settings()["name"], "网址水印 A")
        self.assertEqual(pan._cfg["watermark_name"], "网址水印 A")

    def test_db_wins_over_env_on_restart(self):
        pan.save_watermark_settings(name="库里的名字")
        # 模拟重启：环境变量又给了旧值
        pan._cfg["watermark_name"] = "env 里的旧名"
        pan._load_settings()
        self.assertEqual(pan.watermark_settings()["name"], "库里的名字")

    def test_empty_disables(self):
        pan.save_watermark_settings(name="")
        self.assertEqual(pan.watermark_settings()["name"], "")

    def test_strips_whitespace(self):
        pan.save_watermark_settings(name="  资源搜索网址panseek.xyz  ")
        self.assertEqual(pan.watermark_settings()["name"], "资源搜索网址panseek.xyz")

    def test_rejects_path_separators(self):
        for bad in ("a/b", "a\\b", "..", "."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    pan.save_watermark_settings(name=bad)

    def test_rejects_control_chars_and_long(self):
        for bad in ("a\nb", "字" * 61):
            with self.subTest(bad=bad[:8]):
                with self.assertRaises(ValueError):
                    pan.save_watermark_settings(name=bad)

    def test_rejected_input_does_not_touch_runtime(self):
        before = pan.watermark_settings()
        with self.assertRaises(ValueError):
            pan.save_watermark_settings(name="a/b")
        self.assertEqual(pan.watermark_settings(), before)


if __name__ == "__main__":
    unittest.main()
