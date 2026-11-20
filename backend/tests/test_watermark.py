"""转存引流水印的接线测试。

adfilter 那套管"广告判据对不对"，这里管"水印接线对不对"：水印 fid 有没有
进建分享用的清单（用户能看到）、有没有被排除在回收用的 link.fids 之外
（水印是账号级常驻目录，不随转存删）、改名时有没有清旧目录、关掉时是不是
零动作、建失败会不会拖垮一条本来成功的转存、不支持的盘是不是自动跳过。

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
    def test_watermark_in_share_but_not_in_recycle_fids(self):
        client = _FakeClient(watermark_name="资源搜索网址panseek.xyz")
        link = client.mirror("https://example.com/s/x")

        # 建水印用的是落点目录
        self.assertEqual(client.make_calls, [("资源搜索网址panseek.xyz", "tmpdir")])
        # 分享时水印 fid 在清单里 → 用户能看到
        self.assertEqual(client.shared_fids, ["new-fid", "wm-fid"])
        # 但 link.fids（存 mirrors、供三条回收路径删）只有正片 → 水印常驻不被删
        self.assertEqual(link.fids, ["new-fid"])

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
        # 关掉改名清理的后台线程：测试只验证存储层，别让它持有 db 连接
        self._cleanup_before = pan._WATERMARK_CLEANUP_ENABLED
        pan._WATERMARK_CLEANUP_ENABLED = False

    def tearDown(self):
        pan._WATERMARK_CLEANUP_ENABLED = self._cleanup_before
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


class TestWatermarkSameName(unittest.TestCase):
    """同名水印撞名：必须先查到已存在目录复用 fid，别让水印漏进 fids。

    线上实测：夸克第二次转存时同名目录已存在，建目录回 400「同名冲突」，
    fid 没进 fids，分享里就没水印。修法是建前先列目录查同名。
    """

    def _quark(self):
        from unittest import mock
        from app.sdk.adapters.quark.client import QuarkClient
        from app.sdk.types import Account

        with mock.patch.object(QuarkClient, "_build_requester", return_value=mock.Mock()):
            return QuarkClient(Account(id=1, type="quark", temp_dir="pantrace"))

    def test_quark_reuses_existing_dir_without_mkdir(self):
        """同名目录已存在 → 直接返回它的 fid，不新建。"""
        from unittest import mock
        from app.sdk.types import File

        client = self._quark()
        with mock.patch.object(client, "ensure_dir", return_value="parent-fid"), \
             mock.patch.object(client, "list_files", return_value=[
                 File(fid="existing-wm", name="水印", size=0, is_dir=True),
             ]), \
             mock.patch.object(client, "mkdir") as mkdir:
            result = client.make_watermark("水印")
        mkdir.assert_not_called()
        self.assertEqual(result, "existing-wm")

    def test_quark_creates_when_no_same_name(self):
        """没同名 → 才走 mkdir。"""
        from unittest import mock

        client = self._quark()
        with mock.patch.object(client, "ensure_dir", return_value="parent-fid"), \
             mock.patch.object(client, "list_files", return_value=[
                 File(fid="other", name="别的东西", size=0, is_dir=True),
             ]), \
             mock.patch.object(client, "mkdir", return_value="new-wm") as mkdir:
            result = client.make_watermark("水印")
        mkdir.assert_called_once_with("水印", "parent-fid")
        self.assertEqual(result, "new-wm")

    def test_quark_race_recovers_fid_after_conflict(self):
        """查完到建之间被抢先建了：mkdir 抛撞名，再列一次捞回 fid。"""
        from unittest import mock
        from app.sdk.errors import UpstreamError
        from app.sdk.types import File

        client = self._quark()
        # 第一次列目录（查重）空，mkdir 抛撞名，第二次列目录（兜底）有同名
        with mock.patch.object(client, "ensure_dir", return_value="parent-fid"), \
             mock.patch.object(client, "list_files", side_effect=[
                 [],
                 [File(fid="raced-wm", name="水印", size=0, is_dir=True)],
             ]), \
             mock.patch.object(client, "mkdir", side_effect=UpstreamError("同名冲突")):
            result = client.make_watermark("水印")
        self.assertEqual(result, "raced-wm")

    def test_quark_real_conflict_still_skips(self):
        """兜底也没捞到（真失败）→ 返回空，交给基类跳过，不拖垮转存。"""
        from unittest import mock
        from app.sdk.errors import UpstreamError

        client = self._quark()
        with mock.patch.object(client, "ensure_dir", return_value="parent-fid"), \
             mock.patch.object(client, "list_files", return_value=[]), \
             mock.patch.object(client, "mkdir", side_effect=UpstreamError("同名冲突")):
            result = client.make_watermark("水印")
        self.assertEqual(result, "")

    def test_find_child_dir_ignores_files_and_other_names(self):
        """查重只认 is_dir 且名字精确相等的，文件/别的名字不算。"""
        from unittest import mock
        from app.sdk.types import File

        client = self._quark()
        with mock.patch.object(client, "list_files", return_value=[
            File(fid="f", name="水印", size=10, is_dir=False),
            File(fid="d2", name="水印副本", size=0, is_dir=True),
            File(fid="d3", name="水印", size=0, is_dir=True),
        ]):
            self.assertEqual(client.find_child_dir("parent", "水印"), "d3")

    def test_xunlei_reuses_existing_dir(self):
        """迅雷同样：同名已存在就复用，不建、不取 captcha。"""
        from unittest import mock
        from app.sdk.adapters.xunlei.client import XunleiClient
        from app.sdk.types import Account, File

        with mock.patch.object(XunleiClient, "_build_requester", return_value=mock.Mock()):
            client = XunleiClient(Account(id=1, type="xunlei", temp_dir="pantrace",
                                          refresh_token="r"))
        with mock.patch.object(client, "_resolve_transfer_parent", return_value="parent"), \
             mock.patch.object(client, "find_child_dir", return_value="existing-x"), \
             mock.patch.object(client, "_call") as call:
            result = client.make_watermark("水印")
        call.assert_not_called()
        self.assertEqual(result, "existing-x")


class TestWatermarkPersistence(unittest.TestCase):
    """水印常驻 + 改名清理旧目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "settings.db"
        pan.init(self.db, watermark_name="旧水印")
        self._cleanup_before = pan._WATERMARK_CLEANUP_ENABLED
        # 改名清理走后台线程，测试里关掉自动触发，改由显式调用验证
        pan._WATERMARK_CLEANUP_ENABLED = False

    def tearDown(self):
        pan._WATERMARK_CLEANUP_ENABLED = self._cleanup_before
        self._tmp.cleanup()

    def test_rename_queues_cleanup(self):
        """改名时排队清理旧名字，关名也清理。"""
        from unittest import mock

        # 临时打开开关，mock 掉真起线程的函数，只验证"有没有排队"
        pan._WATERMARK_CLEANUP_ENABLED = True
        try:
            with mock.patch.object(pan, "_queue_watermark_cleanup") as queue:
                pan.save_watermark_settings(name="新水印")
                queue.assert_called_once_with("旧水印")
                queue.reset_mock()
                pan.save_watermark_settings(name="")  # 关闭也要清旧目录
                queue.assert_called_once_with("新水印")
        finally:
            pan._WATERMARK_CLEANUP_ENABLED = False

    def test_same_name_no_cleanup(self):
        """名字没变不触发清理。"""
        from unittest import mock

        with mock.patch.object(pan, "_queue_watermark_cleanup") as queue:
            pan.save_watermark_settings(name="旧水印")
            queue.assert_not_called()

    def test_run_cleanup_visits_enabled_accounts(self):
        """后台清理遍历启用账号，对每家调 delete_watermark(old, temp_dir)。"""
        from unittest import mock
        from app.sdk.types import Account

        enabled = Account(id=1, type="quark", temp_dir="tmp1", status=1)
        disabled = Account(id=2, type="baidu", temp_dir="tmp2", status=0)
        client = mock.Mock()
        with mock.patch.object(pan, "list_accounts", return_value=[enabled, disabled]), \
             mock.patch.object(pan, "_client_for", return_value=client):
            pan._run_watermark_cleanup("旧水印")
        client.delete_watermark.assert_called_once_with("旧水印", "tmp1")
        client.close.assert_called_once()

    def test_run_cleanup_survives_one_account_failure(self):
        """一个账号清理抛异常，不影响后面账号。"""
        from unittest import mock
        from app.sdk.types import Account

        a1 = Account(id=1, type="quark", temp_dir="tmp1", status=1)
        a2 = Account(id=2, type="baidu", temp_dir="tmp2", status=1)
        c1, c2 = mock.Mock(), mock.Mock()
        c1.delete_watermark.side_effect = RuntimeError("挂了")
        with mock.patch.object(pan, "list_accounts", return_value=[a1, a2]), \
             mock.patch.object(pan, "_client_for", side_effect=[c1, c2]):
            pan._run_watermark_cleanup("旧水印")
        c1.close.assert_called_once()
        c2.delete_watermark.assert_called_once_with("旧水印", "tmp2")

    def test_quark_delete_watermark(self):
        """夸克：列目录找到 fid 后删。"""
        from unittest import mock

        client = self._quark_helper()
        with mock.patch.object(client, "ensure_dir", return_value="parent"), \
             mock.patch.object(client, "find_child_dir", return_value="old-wm"), \
             mock.patch.object(client, "delete") as delete:
            result = client.delete_watermark("旧水印", "tmpdir")
        delete.assert_called_once_with(["old-wm"])
        self.assertTrue(result)

    def test_quark_delete_watermark_not_found(self):
        """找不到旧目录 → 不删，返回 False。"""
        from unittest import mock

        client = self._quark_helper()
        with mock.patch.object(client, "ensure_dir", return_value="parent"), \
             mock.patch.object(client, "find_child_dir", return_value=""), \
             mock.patch.object(client, "delete") as delete:
            result = client.delete_watermark("旧水印", "tmpdir")
        delete.assert_not_called()
        self.assertFalse(result)

    @staticmethod
    def _quark_helper():
        from unittest import mock
        from app.sdk.adapters.quark.client import QuarkClient
        from app.sdk.types import Account

        with mock.patch.object(QuarkClient, "_build_requester", return_value=mock.Mock()):
            return QuarkClient(Account(id=1, type="quark", temp_dir="tmpdir"))


if __name__ == "__main__":
    unittest.main()
