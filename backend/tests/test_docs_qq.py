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
        seen: list[int] = []

        def fake_meta(_url: str) -> dq.DocMeta:
            return meta

        def fake_block(_pad: str, _tab: str, start: int) -> list[list[str]]:
            seen.append(start)
            return [["x", "https://pan.quark.cn/s/a"]]

        with mock.patch.object(dq, "fetch_meta", fake_meta), \
             mock.patch.object(dq, "read_block", fake_block):
            rows, _ = dq.read_tab("https://docs.qq.com/sheet/X", tab_id="t1", end_row=99999)

        self.assertEqual(seen, [0, 256])   # 只读两块，第三块 512 已经超了
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
