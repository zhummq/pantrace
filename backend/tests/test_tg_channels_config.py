"""TG 频道动态配置的离线测试。

盯三件事：
1. 库里的覆盖值优先于 .env，且**空列表也是覆盖**（代表明确禁用）；
2. 保存后下一次搜索就读到新频道，缓存键跟着变；
3. 非法频道名在落库前就被挡住，不污染现有配置。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest tests.test_tg_channels_config -v``
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.search.plugins import tg_searchers
from app.search.tg import config_store, tg_cache_key


class FakeSettings:
    tg_channel_list = ["env_default"]


def run(coro):
    return asyncio.run(coro)


class NormalizeTest(unittest.TestCase):
    def test_strip_at_sign_and_dedupe_case_insensitively(self):
        self.assertEqual(
            config_store.normalize([" @Alpha ", "ALPHA", "beta", "", None]),
            ["Alpha", "beta"],
        )

    def test_comma_and_newline_separators(self):
        self.assertEqual(
            config_store.normalize("aa1, @bb2\ncc3"),
            ["aa1", "bb2", "cc3"],
        )

    def test_rejects_paths_and_urls(self):
        for bad in ("https://t.me/s/foo", "foo/bar", ["bad name"], ["a-b"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    config_store.normalize(bad, allow_empty=False)

    def test_empty_allowed_only_for_saving(self):
        self.assertEqual(config_store.normalize([]), [])
        with self.assertRaises(ValueError):
            config_store.normalize("", allow_empty=False)

    def test_max_channels(self):
        names = [f"ch{i:02d}" for i in range(config_store.MAX_CHANNELS + 1)]
        with self.assertRaises(ValueError):
            config_store.normalize(names)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "data" / "analytics.db"
        config_store.init(self.db, default_channels=["env_default"])

    def tearDown(self):
        config_store._reset_state()
        self.tmp.cleanup()

    def test_no_override_uses_env(self):
        self.assertIsNone(config_store.configured())
        self.assertEqual(config_store.effective(["env_default"]), ["env_default"])

    def test_save_is_effective_and_persists_after_restart(self):
        self.assertEqual(config_store.save(["@NewOne", "newone", "second"]), ["NewOne", "second"])
        self.assertEqual(config_store.configured(), ["NewOne", "second"])
        self.assertEqual(config_store.effective(["env_default"]), ["NewOne", "second"])

        # 模拟重启：重新 init 同一个库，仍以库里的值为准
        config_store.init(self.db, default_channels=["env_default"])
        self.assertEqual(config_store.effective(["env_default"]), ["NewOne", "second"])

    def test_explicit_empty_disables_instead_of_fallback(self):
        config_store.save([])
        self.assertEqual(config_store.configured(), [])
        self.assertEqual(config_store.effective(["env_default"]), [])

    def test_clear_returns_to_env(self):
        config_store.save(["one"])
        config_store.clear()
        self.assertIsNone(config_store.configured())
        self.assertEqual(config_store.effective(["env_default"]), ["env_default"])

    def test_invalid_input_does_not_change_existing_config(self):
        config_store.save(["good"])
        with self.assertRaises(ValueError):
            config_store.save(["good", "bad/name"])
        self.assertEqual(config_store.configured(), ["good"])

    def test_broken_row_falls_back_to_env(self):
        import sqlite3

        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tg_settings(key, value, updated_ts) VALUES(?,?,?)",
                ("channels", "not json", 0),
            )
            conn.commit()
        finally:
            conn.close()
        config_store.init(self.db, default_channels=["env_default"])
        self.assertIsNone(config_store.configured())
        self.assertEqual(config_store.effective(["env_default"]), ["env_default"])


class PluginIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config_store.init(Path(self.tmp.name) / "analytics.db", default_channels=["env_default"])

    def tearDown(self):
        config_store._reset_state()
        self.tmp.cleanup()

    def test_plugin_reads_admin_override(self):
        config_store.save(["from_admin"])
        with patch.object(tg_searchers, "get_settings", return_value=FakeSettings()):
            self.assertEqual(tg_searchers._channels(None), ["from_admin"])
            self.assertEqual(
                tg_searchers.TGSeachersPlugin().cache_key("三体"),
                tg_cache_key("三体", ["from_admin"]),
            )

    def test_ext_still_wins_over_admin_override(self):
        config_store.save(["from_admin"])
        with patch.object(tg_searchers, "get_settings", return_value=FakeSettings()):
            self.assertEqual(tg_searchers._channels(["forced"]), ["forced"])

    def test_empty_override_raises_instead_of_silent_fallback(self):
        config_store.save([])
        with patch.object(tg_searchers, "get_settings", return_value=FakeSettings()):
            with self.assertRaises(ValueError):
                run(tg_searchers.TGSeachersPlugin().fetch(None, "三体", {}))


if __name__ == "__main__":
    unittest.main()
