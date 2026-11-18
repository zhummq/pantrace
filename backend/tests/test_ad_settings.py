"""广告配置的后台读写测试。

要守住两件事：
1. 关键词输入的清洗（去空、去重、全角逗号、长度与数量上限）——这是唯一
   能造成误杀的入口，脏词进库等于线上误删。
2. 库覆盖环境变量。线上改完重启，值必须还在；不然运营白调。

跑法：``cd backend && .venv/Scripts/python.exe -m unittest discover -s tests -v``
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import pan


class TestAdSettings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "settings.db"
        pan.init(
            self.db,
            ad_preskip=True,
            ad_filter=False,
            ad_keywords="微信,公众号",
            ad_depth=2,
            ad_max_mb=2.0,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_hot_updates(self):
        """保存即生效：不用重启，panic 里的 _cfg 就是下一次点击用的值。"""
        pan.save_ad_settings(
            preskip=False,
            filter=True,
            keywords="加群, 公众号 ,, 加群",
            depth=3,
            max_mb=5,
        )
        snap = pan.ad_settings()
        # 去空、去重、去空格
        self.assertEqual(snap["keywords"], "加群,公众号")
        self.assertEqual(snap["depth"], 3)
        self.assertEqual(snap["max_mb"], 5.0)
        self.assertFalse(snap["preskip"])
        self.assertTrue(snap["filter"])
        # 真正生效在 _cfg 上，ad_keywords() 是转存时读的那个
        self.assertEqual(pan.ad_keywords(), ["加群", "公众号"])

    def test_db_wins_over_env_on_restart(self):
        pan.save_ad_settings(
            preskip=True, filter=False, keywords="存库里的词", depth=2, max_mb=2
        )
        # 模拟重启：环境变量又给了一份旧值
        pan._cfg["ad_keywords"] = "env里的旧词"
        pan._load_settings()
        self.assertEqual(pan.ad_keywords(), ["存库里的词"])

    def test_fullwidth_comma(self):
        pan.save_ad_settings(
            preskip=True, filter=False, keywords="加群，领福利", depth=2, max_mb=2
        )
        self.assertEqual(pan.ad_keywords(), ["加群", "领福利"])

    def test_clamped_ranges(self):
        pan.save_ad_settings(
            preskip=True, filter=False, keywords="x", depth=99, max_mb=-5
        )
        snap = pan.ad_settings()
        self.assertEqual(snap["depth"], 5)
        self.assertEqual(snap["max_mb"], 0.0)

    def test_word_too_long(self):
        with self.assertRaises(ValueError):
            pan.save_ad_settings(
                preskip=True, filter=False, keywords="啊" * 21, depth=2, max_mb=2
            )

    def test_too_many_words(self):
        words = ",".join(f"w{i}" for i in range(201))
        with self.assertRaises(ValueError):
            pan.save_ad_settings(
                preskip=True, filter=False, keywords=words, depth=2, max_mb=2
            )

    def test_rejected_input_does_not_touch_runtime(self):
        """校验失败时线上配置必须原封不动——不能存一半就生效。"""
        before = pan.ad_settings()
        with self.assertRaises(ValueError):
            pan.save_ad_settings(
                preskip=False, filter=True, keywords="啊" * 30, depth=4, max_mb=8
            )
        self.assertEqual(pan.ad_settings(), before)


if __name__ == "__main__":
    unittest.main()
