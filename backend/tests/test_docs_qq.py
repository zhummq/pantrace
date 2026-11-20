"""腾讯文档读取器的离线测试。

这个模块的难点不在业务，在"格式理解"——protobuf 解错一个字节不会报错，
只会让标题多一个怪字符、让链接少半截。所以测试重点全在字节层：

1. **列式还原**：block 顶层字段号就是列号。列长不一致必须按最长那列补空，
   不能等长配对——C 列短两行不是 bug，是最后两行没填。
2. **长度前缀必须严格**：`\\n<长度><内容>` 只有长度恰好吃到末尾才是文本包装，
   宽松判断会把富文本属性块切开，读出一堆长度字节。
3. **链接不许截**：迅雷分享 id 含连字符，正则少一个 `-` 就把
   `VOzLM-sN4k3...` 截成 `VOzLM`，而 `parse()` 侧仍然返回 ok，
   表现为"库里有一批点开就 404 的链接"。

跑法：``.venv/Scripts/python.exe -m unittest tests.test_docs_qq -v``
全部离线，不发请求。
"""

from __future__ import annotations

import unittest

from app import docs_qq as dq


# ---------------------------------------------------------------- 编码工具
# 自己拼 protobuf 而不是存二进制 fixture：格式理解写进代码，
# 读的人一眼能看出"我以为的结构长什么样"。


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(fn: int, payload: bytes) -> bytes:
    return _varint(fn << 3 | 2) + _varint(len(payload)) + payload


def _vfield(fn: int, value: int) -> bytes:
    return _varint(fn << 3 | 0) + _varint(value)


def _text(value: str) -> bytes:
    """腾讯文档里的文本单元：`\\n<长度><utf8>`。"""
    raw = value.encode("utf-8")
    return b"\n" + _varint(len(raw)) + raw


def _rich(*urls: str) -> bytes:
    """富文本单元格：URL 直接作为 field3 的 payload，同一 URL 出现两次。

    注意 field3 的 payload **就是** `\\n<长度><url>`，不要再套一层 field1——
    真实字节是 `\\x1a\\x35\\n\\x33https://...`，多套一层会让 `_unwrap_text`
    的严格判断走不到。
    """
    body = b""
    for url in urls:
        body += _field(3, _text(url))            # 显示文本
        body += _vfield(5, 1)                    # 属性位，读的时候要跳过
        body += _field(3, _text(url))            # 跳转目标，和显示文本重复
    return _field(3, body)


def make_block(rows: list[tuple[str, list[str]]]) -> bytes:
    """按真实层级拼一块：block → field1 → field5 → field19 → field5 → 列。"""
    col1 = b"".join(_field(1, _text(title)) for title, _ in rows)
    col2 = b"".join(_field(2, _rich(*urls)) for _, urls in rows if urls)
    data = col1 + col2
    sheet = _field(5, _field(19, _field(5, data)))
    return _field(1, sheet)


def make_workbook(tabs: list[tuple[str, str, int]]) -> bytes:
    """workbook → field1 → field5(每个工作表) → field2 → (3.1=id, 5.1=名, 7.1=行数)。"""
    body = b""
    for tab_id, title, max_row in tabs:
        sheet = (
            _field(3, _field(1, tab_id.encode()))
            + _field(5, _field(1, title.encode()))
            + _field(7, _vfield(1, max_row))
            + _field(8, _vfield(1, 26))
        )
        body += _field(5, _field(2, sheet))
    return _field(1, body)


# ---------------------------------------------------------------- 用例


class TestUnwrap(unittest.TestCase):
    def test_text_prefix_stripped(self) -> None:
        self.assertEqual(dq._unwrap_text(_text("植物大战僵尸合集")), "植物大战僵尸合集")

    def test_wrong_length_rejected(self) -> None:
        """长度不等于剩余字节数，就不是一层文本包装。

        这一条是防"标题多个怪字符"的：宽松判断会把长度字节本身当成正文。
        """
        raw = "hello".encode()
        broken = b"\n" + _varint(len(raw) + 5) + raw
        self.assertIsNone(dq._unwrap_text(broken))

    def test_not_a_text_frame(self) -> None:
        self.assertIsNone(dq._unwrap_text(b"\x1a\x05abcde"))


class TestParseBlock(unittest.TestCase):
    def test_columns_align_by_index(self) -> None:
        """标题列 3 行、链接列 2 行 → 第三行链接补空，不能错位。"""
        rows = [
            ("三体", ["https://pan.xunlei.com/s/AAA-bbb"]),
            ("繁花", ["https://pan.xunlei.com/s/CCC-ddd"]),
            ("无链接的书", []),
        ]
        out = dq.parse_block(make_block(rows))
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0][0], "三体")
        self.assertEqual(out[0][1], "https://pan.xunlei.com/s/AAA-bbb")
        self.assertEqual(out[2][0], "无链接的书")
        self.assertEqual(out[2][1], "")

    def test_hyphen_in_share_id_survives(self) -> None:
        """迅雷 id 含 `-`，截断后会变成一条点开 404 的链接。"""
        url = "https://pan.xunlei.com/s/VOzLM-sN4k3xNB6s8oAYy5AlA1"
        out = dq.parse_block(make_block([("天降女子", [url])]))
        self.assertIn(url, out[0][1])

    def test_url_deduped(self) -> None:
        """同一个 URL 在富文本里出现两次（显示文本 + 跳转目标），只留一份。"""
        url = "https://pan.xunlei.com/s/XYZ"
        cell = dq.urls_in_cell(_rich(url))
        self.assertEqual(cell, [url])

    def test_empty_block(self) -> None:
        self.assertEqual(dq.parse_block(b""), [])

    def test_garbage_does_not_raise(self) -> None:
        """截断的字节流在真实网络里会出现，读不出就该返回空而不是抛栈。"""
        for blob in (b"\xff\xff\xff\xff", b"\n\x05ab", b"\x12\x00"):
            with self.subTest(blob=blob):
                self.assertIsInstance(dq.parse_block(blob), list)


class TestWorkbook(unittest.TestCase):
    def test_tabs_parsed(self) -> None:
        blob = make_workbook([
            ("BB08J2", "最新资源1", 380642),
            ("obrrat", "迅雷资源", 525),
        ])
        tabs = dq.parse_workbook(blob)
        self.assertEqual([t.tab_id for t in tabs], ["BB08J2", "obrrat"])
        self.assertEqual(tabs[0].title, "最新资源1")
        self.assertEqual(tabs[0].max_row, 380642)
        self.assertEqual(tabs[1].max_row, 525)


class TestDocUrls(unittest.TestCase):
    def test_pad_and_tab(self) -> None:
        pad, tab = dq.pad_id_of("https://docs.qq.com/sheet/DWGtHWm5Ca2FJS2t0?tab=BB08J2")
        self.assertEqual(pad, "DWGtHWm5Ca2FJS2t0")
        self.assertEqual(tab, "BB08J2")

    def test_pad_without_tab(self) -> None:
        pad, tab = dq.pad_id_of("https://docs.qq.com/sheet/DWGtHWm5Ca2FJS2t0")
        self.assertEqual(pad, "DWGtHWm5Ca2FJS2t0")
        self.assertEqual(tab, "")

    def test_not_a_doc_url(self) -> None:
        with self.assertRaises(dq.DocError):
            dq.pad_id_of("https://pan.quark.cn/s/abcdef")


class TestRangeClamp(unittest.TestCase):
    def test_end_row_never_exceeds_sheet(self) -> None:
        """`--limit` 传大了不能读空块、也不能把进度写成比表长还大。

        线上真出现过 last_row=5013 而表只有 525 行，
        后面每次增量都从 5013 起读，等于永远读空。
        """
        from unittest import mock

        meta = dq.DocMeta(
            pad_id="X", title="t", tabs=[dq.Tab("t1", "表", max_row=300)]
        )
        seen: list[tuple[int, int]] = []

        def fake_meta(_url: str) -> dq.DocMeta:
            return meta

        def fake_block(
            _pad: str, _tab: str, start: int, end: int = 0, **_kw
        ) -> list[list[str]]:
            seen.append((start, end))
            return [["x", "https://pan.quark.cn/s/a"]]

        with mock.patch.object(dq, "fetch_meta", fake_meta), \
             mock.patch.object(dq, "read_block", fake_block):
            rows, _ = dq.read_tab("https://docs.qq.com/sheet/X", tab_id="t1", end_row=99999)

        # 块长 1024 > 表长 300，所以整块请求只有一次，且被夹到 299；
        # 之后的 (n, n) 是表尾那 16 行的逐行补读，不参与夹取。
        full = [(s, e) for s, e in seen if s != e]
        self.assertEqual(full, [(0, 299)])
        self.assertEqual(len(rows), 300)


class TestColumnRead(unittest.TestCase):
    """按列读：行号是数出来的，不是配出来的。

    以前整块请求拿到的是"按类型分组的格子流"，靠数量关系猜谁配谁，翻过两次车：
    广告行让文字格多一个、某格写俩链接让链接格多一个，总数一持平就整体错位。
    现在只请求一列——没有第二堆可以混淆，返回顺序就是行顺序。
    """

    @staticmethod
    def _fake(rows, trim=None):
        """把二维表模拟成"按列请求"的返回。trim={列号: 尾部裁掉几行}。"""
        trim = trim or {}

        def fake(_pad, _tab, col, start, end):
            # 越过表尾时接口返回表头那一段，这里照样模拟出来
            chunk = rows if start >= len(rows) else rows[start:end + 1]
            out = []
            for r in chunk:
                v = r[col] if col < len(r) else ""
                out.append(("", [v]) if v.startswith("http") else (v, []))
            cut = trim.get(col, 0)
            if cut and start < len(rows):
                out = out[:max(0, len(out) - cut)]
            return out

        return fake

    def test_ad_rows_do_not_shift_anything(self) -> None:
        """广告行有两个文字格——旧逻辑正是从这里开始整体错位。"""
        from unittest import mock

        rows = [
            ["Ctel+F查询文件…", "找资源+v  zzgshr123", ""],
            ["踢毽子视频教学", "https://pan.xunlei.com/s/VOfTG6GRKY0mPa9Ukncg2dQwA1", ""],
            ["植物大战僵尸合集", "https://pan.xunlei.com/s/VOfTG7LcejPiLKoUAqTmeDglA1", ""],
        ]
        with mock.patch.object(dq, "_column_cells", self._fake(rows)):
            got = dq.read_block("P", "T", 0, 2)

        self.assertEqual(got, rows)
        self.assertIn("2dQwA1", got[1][1])       # 踢毽子配的是它自己那一行的链接
        self.assertNotIn("DxRA1", got[1][1])     # 不是第 3 行的那条

    def test_short_column_is_padded_not_shifted(self) -> None:
        """某列尾部被裁时，缺的行补空，不能让后面的行整体上移。"""
        from unittest import mock

        rows = [
            ["甲", "https://pan.quark.cn/s/a"],
            ["乙", "https://pan.quark.cn/s/b"],
            ["丙", "https://pan.quark.cn/s/c"],
        ]
        with mock.patch.object(dq, "_column_cells", self._fake(rows, trim={0: 2})):
            got = dq.read_block("P", "T", 0, 2)

        self.assertEqual(len(got), 3)
        self.assertEqual(
            [r[1] for r in got],
            ["https://pan.quark.cn/s/a", "https://pan.quark.cn/s/b",
             "https://pan.quark.cn/s/c"],
        )
        self.assertEqual(got[0][0], "甲")
        self.assertEqual(got[1][0], "")          # 被裁的两行留空，不去抢上一行的

    def test_overflow_header_is_treated_as_no_data(self) -> None:
        """越过表尾时接口返回的是表头那一段，必须识别成"没数据"。"""
        from unittest import mock

        rows = [["Ctel+F查询文件", "找资源+v"], ["甲", "https://pan.quark.cn/s/a"]]
        dq._header_cache.clear()
        with mock.patch.object(dq, "_column_cells", self._fake(rows)):
            self.assertEqual(dq.read_block("P", "T", 5000, 6000), [])
        dq._header_cache.clear()


class TestColumnOverlap(unittest.TestCase):
    """窗口尾部的第 0 列会被接口裁掉，靠相邻块重叠补回来。

    实测：请求 0~1023 只回 1013 个标题，可单行窗读第 1015 行明明有内容；
    窗口挪到 1000 起再读，同一段 1024 个全在。头部完整、尾部不可信。
    """

    def test_tail_trimmed_rows_are_restored(self) -> None:
        from unittest import mock

        total = 1100
        records = [[f"标题{i}", f"https://pan.xunlei.com/s/{i:04d}"] for i in range(total)]

        def fake_block(_pad, _tab, start, end, ncols=3):
            chunk = [list(r) for r in records[start:end + 1]]
            if end - start >= 1000:              # 只裁大窗口的尾部，单行窗是完整的
                for r in chunk[-16:]:
                    r[0] = ""
            return chunk

        meta = dq.DocMeta(pad_id="X", tabs=[dq.Tab("t1", "表", max_row=total)])
        with mock.patch.object(dq, "fetch_meta", lambda _u: meta), \
             mock.patch.object(dq, "read_block", fake_block):
            rows, _ = dq.read_tab("https://docs.qq.com/sheet/X", tab_id="t1")

        self.assertEqual(len(rows), total)
        self.assertEqual(rows[1015][0], "标题1015")   # 被裁的那批靠下一块补回来
        self.assertEqual(rows[1080][0], "标题1080")


class TestEmptyBlockIsNotData(unittest.TestCase):
    """空 block 不等于"这一段没数据"。

    请求太密时接口回 200、结构也完整，只是 `block_datas` 空着，几十秒后自己
    恢复。把它当空块放过去就等于整块 256 行静默消失——而且 `read_tab` 正常
    返回，`last_row` 会被推到表尾，下一轮增量只回看 512 行，永远补不回来。
    2026-11-20 全量重跑因此丢过 35 万行。
    """

    def test_empty_then_data_is_retried(self) -> None:
        from unittest import mock

        calls = {"n": 0}

        def fake_opendoc(*_a, **_k):
            calls["n"] += 1
            return {"n": calls["n"]}

        def fake_offsets(payload):
            return [] if payload["n"] == 1 else [(0, b"")]

        with mock.patch.object(dq, "_opendoc", fake_opendoc), \
             mock.patch.object(dq, "_block_offsets", fake_offsets), \
             mock.patch.object(dq, "_column_payloads", lambda _b: [(2, b"c")]), \
             mock.patch.object(dq, "_text_of", lambda _v: "标题"), \
             mock.patch.object(dq, "urls_in_cell", lambda _v: []), \
             mock.patch.object(dq.time, "sleep", lambda _s: None):
            cells = dq._window_cells("P", "T", 0, 255)

        self.assertEqual(cells, [("标题", [])])
        self.assertEqual(calls["n"], 2)     # 空了一次就重试，第二次拿回来

    def test_always_empty_raises(self) -> None:
        from unittest import mock

        with mock.patch.object(dq, "_opendoc", lambda *_a, **_k: {}), \
             mock.patch.object(dq, "_block_offsets", lambda _p: []), \
             mock.patch.object(dq.time, "sleep", lambda _s: None):
            with self.assertRaises(dq.DocError):
                dq._window_cells("P", "T", 256, 511)

    def test_single_row_window_empty_is_fine(self) -> None:
        """单行窗口空是常态（空白行本来就没有格子），不能重试也不能抛。"""
        from unittest import mock

        calls = {"n": 0}

        def fake_opendoc(*_a, **_k):
            calls["n"] += 1
            return {}

        with mock.patch.object(dq, "_opendoc", fake_opendoc), \
             mock.patch.object(dq, "_block_offsets", lambda _p: []):
            got = dq._window_cells("P", "T", 700, 700)

        self.assertEqual(got, [])
        self.assertEqual(calls["n"], 1)

    def test_block_empty_but_single_row_has_data_fails(self) -> None:
        """整块读空、单行却有数据 → 限流，必须让这轮同步失败而不是写进度。"""
        from unittest import mock

        meta = dq.DocMeta(pad_id="X", title="t", tabs=[dq.Tab("t1", "表", max_row=3000)])
        with mock.patch.object(dq, "fetch_meta", lambda _u: meta), \
             mock.patch.object(dq, "read_block", lambda *_a, **_k: []), \
             mock.patch.object(dq, "_window_cells", lambda *_a, **_k: [("有数据", [])]):
            with self.assertRaises(dq.DocError):
                dq.read_tab("https://docs.qq.com/sheet/X", tab_id="t1")

    def test_empty_fingerprint_is_not_cached(self) -> None:
        """指纹读空时不能进缓存：缓存成空串，之后每块都会被判越界。"""
        from unittest import mock

        with mock.patch.object(
            dq, "_column_cells", side_effect=[[], [("表头文字", [])]]
        ):
            dq._header_cache.clear()
            self.assertEqual(dq._header_fingerprint("P", "T"), "")
            self.assertEqual(dq._header_fingerprint("P", "T"), "表头文字")
            self.assertEqual(dq._header_fingerprint("P", "T"), "表头文字")
        dq._header_cache.clear()


class TestThrottle(unittest.TestCase):
    """全局节流：宁可慢，也不能把腾讯文档压到回空块 / 弹人机验证。"""

    def tearDown(self) -> None:
        dq._next_slot = 0.0

    def test_two_requests_are_at_least_one_second_apart(self) -> None:
        import time as _time

        dq._next_slot = 0.0
        t0 = _time.monotonic()
        dq._throttle()
        dq._throttle()
        self.assertGreaterEqual(_time.monotonic() - t0, 0.95)

    def test_rate_constant_is_conservative(self) -> None:
        """这个数被谁改大都会重演 2026-11-20 那次 WAF 封禁，钉死上限。"""
        self.assertLessEqual(dq.REQ_PER_SEC, 1.5)


if __name__ == "__main__":
    unittest.main()
