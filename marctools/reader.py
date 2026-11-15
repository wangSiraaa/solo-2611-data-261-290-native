"""按记录边界切分 MARC21 文件，并逐条严格按 UTF-8 解码。

题目要求：整个记录无法解码时记录失败，但只要能从独立记录边界确认后续记录，
仍应继续处理。因此这里不依赖 pymarc 的流式 reader（它在遇错后可能停止），
而是：

1. 用 leader(0-4) 的记录长度切出记录字节范围；
2. 每条记录单独做 UTF-8 解码并检查结构；
3. 长度明显不可信时，退回到记录终止符 0x1D 找边界，但不从损坏字节里猜测
   字段层内容——这类记录记为失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

RECORD_TERMINATOR = b"\x1d"
FIELD_TERMINATOR = b"\x1e"
SUBFIELD_DELIMITER = b"\x1f"
MIN_RECORD_LENGTH = 24


@dataclass
class RawRecord:
    """一条记录的原始字节范围（起止均为文件内字节偏移，左闭右开）。"""

    record_index: int  # 以 0 起的文件内记录序号，作为来源标识
    byte_start: int
    byte_end: int
    raw: bytes
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _declared_length(data: bytes, pos: int) -> Optional[int]:
    if pos + 5 > len(data):
        return None
    head = data[pos : pos + 5]
    if not head.isdigit():
        return None
    return int(head)


def split_records(data: bytes) -> list[RawRecord]:
    """切出文件中的全部记录字节范围。

    正常记录使用 leader 的前 5 位长度；若长度不可信，则用 0x1D 终止符寻找
    记录末尾，并标记该记录的结构错误（仍保留原始字节以便诊断）。
    """

    records: list[RawRecord] = []
    pos = 0
    index = 0
    total = len(data)

    while pos < total:
        # 跳过记录之间可能存在的空白/换行（某些导出工具会加换行）。
        while pos < total and data[pos] in b" \t\r\n":
            pos += 1
        if pos >= total:
            break

        declared = _declared_length(data, pos)
        end: Optional[int] = None
        error: Optional[str] = None

        if declared is not None and declared >= MIN_RECORD_LENGTH:
            candidate = pos + declared
            if candidate <= total:
                # 记录末尾应当是 0x1D；否则长度不可信。
                if data[candidate - 1 : candidate] == RECORD_TERMINATOR:
                    end = candidate
                else:
                    error = (
                        f"leader 声明长度 {declared}，但第 {candidate - 1} "
                        "字节不是记录终止符 0x1D"
                    )
            else:
                error = (
                    f"leader 声明长度 {declared} 超出文件剩余字节 "
                    f"{total - pos}"
                )

        if end is None:
            term = data.find(RECORD_TERMINATOR, pos)
            if term == -1:
                # 无终止符：剩余字节构成一条无法确认边界的失败记录。
                records.append(
                    RawRecord(
                        record_index=index,
                        byte_start=pos,
                        byte_end=total,
                        raw=data[pos:total],
                        error=error or "找不到记录终止符 0x1D，无法确认记录边界",
                    )
                )
                break
            end = term + 1
            if error is None:
                error = (
                    "leader 长度不可用，依靠 0x1D 确认边界："
                    f"实际长度 {end - pos}"
                )

        raw = data[pos:end]
        records.append(
            RawRecord(
                record_index=index,
                byte_start=pos,
                byte_end=end,
                raw=raw,
                error=error,
            )
        )
        pos = end
        index += 1

    return records


def decode_record(raw: RawRecord) -> tuple[Optional[str], Optional[str]]:
    """严格 UTF-8 解码一条记录，返回 (文本, 错误)。"""

    if raw.error:
        return None, raw.error
    try:
        text = raw.raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return None, (
            f"UTF-8 解码失败：字节 {exc.start}..{exc.end} "
            f"({exc.reason})"
        )
    return text, None


@dataclass
class RecordText:
    """可供字段级解析的一条记录文本。"""

    raw: RawRecord
    text: str


def load_records(path: str | Path) -> tuple[list[RawRecord], list[RecordText]]:
    """读取文件，返回 (全部原始记录, 成功解码记录)。"""

    data = Path(path).read_bytes()
    raws = split_records(data)
    decoded: list[RecordText] = []
    for raw in raws:
        text, err = decode_record(raw)
        if err is not None:
            raw.error = err
        else:
            decoded.append(RecordText(raw=raw, text=text))
    return raws, decoded
