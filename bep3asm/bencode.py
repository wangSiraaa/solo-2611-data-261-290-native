"""最小化、严格的 bencode 编解码（BEP3 所需子集）。

运行环境不允许联网安装第三方库，因此在此内置一个仅依赖标准库的实现。
解码端刻意保持严格：拒绝前导零、负零、字典重复键、尾随字节与非字典顶层
结构；编码端按 bencode 规范对字典键按字节序排序。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .errors import AssembleError


class BencodeError(AssembleError):
    """bencode 数据格式非法。"""


def _decode_at(buf: bytes, pos: int) -> tuple[object, int]:
    if pos >= len(buf):
        raise BencodeError("bencode 数据被截断")
    tag = buf[pos]

    if tag == 0x69:  # 'i' 整数
        end = buf.find(b"e", pos + 1)
        if end == -1:
            raise BencodeError("整数缺少结束符 'e'")
        text = buf[pos + 1:end]
        if not text or (text.startswith(b"-") and len(text) == 1):
            raise BencodeError("整数体为空")
        if text != b"0" and (text.startswith(b"0") or text.startswith(b"-0")):
            raise BencodeError("整数含有非法前导零")
        if not text.lstrip(b"-").isdigit():
            raise BencodeError("整数体含非数字字符")
        return int(text), end + 1

    if tag == 0x6C:  # 'l' 列表
        pos += 1
        items: list[object] = []
        while True:
            if pos >= len(buf):
                raise BencodeError("列表被截断")
            if buf[pos] == 0x65:  # 'e'
                return items, pos + 1
            value, pos = _decode_at(buf, pos)
            items.append(value)

    if tag == 0x64:  # 'd' 字典
        pos += 1
        result: dict[bytes, object] = {}
        while True:
            if pos >= len(buf):
                raise BencodeError("字典被截断")
            if buf[pos] == 0x65:  # 'e'
                return result, pos + 1
            key, pos = _decode_at(buf, pos)
            if not isinstance(key, bytes):
                raise BencodeError("字典键必须是字节串")
            if key in result:
                # 重复键有歧义，拒绝；但不要求键按字节序排列（真实数据不保证）。
                raise BencodeError("字典出现重复键")
            value, pos = _decode_at(buf, pos)
            result[key] = value

    if 0x30 <= tag <= 0x39:  # 字节串 "<长度>:<数据>"
        colon = buf.find(b":", pos)
        if colon == -1:
            raise BencodeError("字节串缺少长度分隔符 ':'")
        length_text = buf[pos:colon]
        if not length_text.isdigit():
            raise BencodeError("字节串长度非法")
        if length_text != b"0" and length_text.startswith(b"0"):
            raise BencodeError("字节串长度含非法前导零")
        length = int(length_text)
        start = colon + 1
        end = start + length
        if end > len(buf):
            raise BencodeError("字节串数据被截断")
        return buf[start:end], end

    raise BencodeError(f"位置 {pos} 出现无法识别的 bencode 标签 0x{tag:02x}")


def decode(data: bytes) -> object:
    """解码 bencode 字节串，拒绝尾随数据。"""
    if not isinstance(data, bytes):
        raise BencodeError("bencode 解码输入必须是 bytes")
    value, end = _decode_at(data, 0)
    if end != len(data):
        raise BencodeError("bencode 顶层结构之后存在尾随字节")
    return value


def encode(value: object) -> bytes:
    """把 Python 对象编码为 bencode 字节串。"""
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, bool):
        # bool 是 int 的子类，bencode 里没有布尔类型，先于 int 分支拦截。
        raise BencodeError("bencode 不支持布尔值")
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, Mapping):
        pieces: list[bytes] = []
        for raw_key, item in value.items():
            if not isinstance(raw_key, bytes):
                raise BencodeError("bencode 字典键必须是 bytes")
            pieces.append(encode(raw_key) + encode(item))
        pieces.sort()
        return b"d" + b"".join(pieces) + b"e"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    raise BencodeError(f"无法编码类型 {type(value).__name__}")
