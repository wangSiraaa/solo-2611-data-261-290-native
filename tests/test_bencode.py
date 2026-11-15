"""内置 bencode 编解码器的严格性与往返测试。"""

from __future__ import annotations

import unittest

from bep3asm import bencode
from bep3asm.bencode import BencodeError


class DecodeStrictnessTest(unittest.TestCase):
    def test_roundtrip_basic_types(self) -> None:
        cases = [
            0, 1, -1, 42,
            b"", b"spam",
            [1, b"a", [b"nested"]],
            {b"z": 1, b"a": [b"x"], b"m": b""},
        ]
        for value in cases:
            self.assertEqual(bencode.decode(bencode.encode(value)), value)

    def test_reject_leading_zero_integer(self) -> None:
        for bad in (b"i01e", b"i-0e", b"ie", b"i-e"):
            with self.assertRaises(BencodeError):
                bencode.decode(bad)

    def test_reject_leading_zero_string(self) -> None:
        with self.assertRaises(BencodeError):
            bencode.decode(b"04:spam")

    def test_reject_truncation_and_trailing(self) -> None:
        with self.assertRaises(BencodeError):
            bencode.decode(b"4:spa")
        with self.assertRaises(BencodeError):
            bencode.decode(b"i12eextra")
        with self.assertRaises(BencodeError):
            bencode.decode(b"l1:a")

    def test_reject_duplicate_dict_key(self) -> None:
        with self.assertRaises(BencodeError):
            bencode.decode(b"d1:a i1e 1:a i2e e".replace(b" ", b""))

    def test_accept_unsorted_dict_keys(self) -> None:
        # 真实 torrent 的键不保证排序，解码器必须接受（仅拒绝重复）。
        decoded = bencode.decode(b"d1:zi1e1:ai2ee")
        self.assertEqual(decoded, {b"z": 1, b"a": 2})

    def test_encoder_sorts_keys(self) -> None:
        self.assertEqual(
            bencode.encode({b"z": 1, b"a": 2}),
            b"d1:ai2e1:zi1ee",
        )

    def test_reject_boolean_and_float(self) -> None:
        with self.assertRaises(BencodeError):
            bencode.encode(True)
        with self.assertRaises(BencodeError):
            bencode.encode(1.5)
        with self.assertRaises(BencodeError):
            bencode.encode("str-not-bytes")

    def test_reject_non_bytes_dict_key(self) -> None:
        with self.assertRaises(BencodeError):
            bencode.encode({"str": 1})

    def test_fixture_torrent_decodes(self) -> None:
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "fixtures", "package.torrent",
        )
        with open(path, "rb") as handle:
            root = bencode.decode(handle.read())
        self.assertIn(b"info", root)
        self.assertEqual(root[b"info"][b"name"], b"package")


if __name__ == "__main__":
    unittest.main(verbosity=2)
