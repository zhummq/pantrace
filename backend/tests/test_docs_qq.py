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

        def fake_block(_pad: str, _tab: str, start: int, end: int = 0) -> list[list[str]]:
            seen.append((start, end))
            return [["x", "https://pan.quark.cn/s/a"]]

        with mock.patch.object(dq, "fetch_meta", fake_meta), \
             mock.patch.object(dq, "read_block", fake_block):
            rows, _ = dq.read_tab("https://docs.qq.com/sheet/X", tab_id="t1", end_row=99999)

        # 只读两块；第二块被夹到 299——但这里每块都只回 1 行，所以 512 那块不会再请求
        self.assertEqual([s for s, _ in seen], [0, 256])
        self.assertEqual(seen[1], (256, 299))   # end_row 夹到表长 300 的最后一行
        self.assertEqual(len(rows), 2)


class TestRowAlignment(unittest.TestCase):
    """块内对齐：文字格和链接格数量对不上时，不能按下标硬配。

    接口返回的单元格是按**类型**分组的（纯文字一堆、带超链接一堆），不是按列。
    只要某行多一格文字（作者的广告行）或少一格（空标题），两堆长度就不再相等，
    按数组下标配就会整体错位——标题配上隔壁行的链接，而且不报错、点得开。
    """

    @staticmethod
    def _sheet(rows):
        """模拟接口：按行给数据，返回时按类型分组。"""
        def fake(_pad, _tab, start, end):
            texts, links = [], []
            for row in rows[start:end + 1]:
                for cell in row:
                    if not cell:
                        continue
                    if cell.startswith("http"):
                        links.append(("", [cell]))
                    else:
                        texts.append((cell, []))
            return texts + links
        return fake

    def test_plain_block_pairs_by_index(self) -> None:
        rows = [["甲", "https://pan.quark.cn/s/a"], ["乙", "https://pan.quark.cn/s/b"]]
        got = dq._align_rows("P", "T", 0, 1, [dq.MAX_ALIGN_REQUESTS], self._sheet(rows)("P", "T", 0, 1))
        self.assertEqual(got, rows)

    def test_extra_text_cell_does_not_shift_rows(self) -> None:
        """广告行有两个文字格、没有链接——这正是线上错位的起因。"""
        from unittest import mock

        rows = [
            ["Ctel+F查询文件…", "找资源+v  zzgshr123"],
            ["踢毽子视频教学", "https://pan.xunlei.com/s/VOfTG6"],
            ["植物大战僵尸合集", "https://pan.xunlei.com/s/VOfTG7L"],
        ]
        fake = self._sheet(rows)
        with mock.patch.object(dq, "_window_cells", fake):
            got = dq._align_rows("P", "T", 0, 2, [dq.MAX_ALIGN_REQUESTS])

        self.assertEqual(len(got), 3)
        self.assertEqual(got[0], ["Ctel+F查询文件…", "找资源+v  zzgshr123"])
        self.assertEqual(got[1], ["踢毽子视频教学", "https://pan.xunlei.com/s/VOfTG6"])
        self.assertEqual(got[2], ["植物大战僵尸合集", "https://pan.xunlei.com/s/VOfTG7L"])

    def test_empty_title_keeps_link_on_its_own_row(self) -> None:
        from unittest import mock

        rows = [
            ["甲", "https://pan.quark.cn/s/a"],
            ["", "https://pan.quark.cn/s/b"],
            ["丙", "https://pan.quark.cn/s/c"],
        ]
        with mock.patch.object(dq, "_window_cells", self._sheet(rows)):
            got = dq._align_rows("P", "T", 0, 2, [dq.MAX_ALIGN_REQUESTS])

        self.assertEqual([r[1] for r in got],
                         ["https://pan.quark.cn/s/a", "https://pan.quark.cn/s/b",
                          "https://pan.quark.cn/s/c"])
        self.assertEqual(got[1][0], "")      # 空标题就留空，不去抢别人的

    def test_single_row_cells_keep_column_order(self) -> None:
        cells = [("标题", []), ("https://pan.quark.cn/s/x", ["https://pan.quark.cn/s/x"])]
        self.assertEqual(dq._cells_to_row(cells), ["标题", "https://pan.quark.cn/s/x"])

    def test_budget_exhausted_still_returns_rows(self) -> None:
        """预算用完要退回硬配，不能抛异常、也不能少行——少行等于丢数据。"""
        from unittest import mock

        rows = [["", "https://pan.quark.cn/s/%d" % i] for i in range(8)]
        with mock.patch.object(dq, "_window_cells", self._sheet(rows)):
            got = dq._align_rows("P", "T", 0, 7, [1])

        self.assertEqual(len(got), 8)


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

        meta = dq.DocMeta(pad_id="X", title="t", tabs=[dq.Tab("t1", "表", max_row=600)])
        with mock.patch.object(dq, "fetch_meta", lambda _u: meta), \
             mock.patch.object(dq, "read_block", lambda *_a, **_k: []), \
             mock.patch.object(dq, "_window_cells", lambda *_a, **_k: [("有数据", [])]):
            with self.assertRaises(dq.DocError):
                dq.read_tab("https://docs.qq.com/sheet/X", tab_id="t1")

    def test_empty_fingerprint_is_not_cached(self) -> None:
        """指纹读空时不能进缓存：缓存成空串，之后每块都会被判越界。"""
        from unittest import mock

        with mock.patch.object(
            dq, "_window_cells", side_effect=[[], [("表头文字", [])]]
        ):
            dq._header_cache.clear()
            self.assertEqual(dq._header_fingerprint("P", "T"), "")
            self.assertEqual(dq._header_fingerprint("P", "T"), "表头文字")
            self.assertEqual(dq._header_fingerprint("P", "T"), "表头文字")
        dq._header_cache.clear()


if __name__ == "__main__":
    unittest.main()
