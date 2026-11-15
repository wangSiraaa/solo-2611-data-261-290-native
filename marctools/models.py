"""MARC21 记录的数据模型。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Subfield:
    """一个子字段出现，按出现次序保序，允许同 code 重复。"""

    subfield_index: int  # 0 起，字段内出现序号
    code: str
    value: str
    byte_start: int  # 0x1F 分隔符的绝对字节偏移
    byte_end: int  # 子字段结束位置（下一个 0x1F 或字段正文末尾），不含
    value_byte_start: int
    value_byte_end: int  # 不含


@dataclass
class MarcField:
    """一个字段出现（不是字段类型）。"""

    field_index: int  # 0 起，目录顺序，即原出现序号
    tag_occurrence: int  # 1 起，同 tag 的第几次出现
    tag: str
    is_control: bool
    indicator1: Optional[str]
    indicator2: Optional[str]
    control_value: Optional[str]
    subfields: list[Subfield] = field(default_factory=list)
    byte_start: int = 0  # 字段正文起点（含 indicator），文件内绝对偏移
    byte_end: int = 0  # 字段正文终点（不含 0x1E）
    terminator_byte: int = 0  # 0x1E 的绝对偏移
    directory_entry: str = ""

    def subfield(self, code: str) -> list[Subfield]:
        """返回指定 code 的全部子字段出现（不覆盖重复项）。"""

        return [sf for sf in self.subfields if sf.code == code]


@dataclass
class ParsedRecord:
    record_index: int  # 0 起，文件内原始记录序号（来源标识）
    byte_start: int
    byte_end: int
    leader: str
    base_address: int
    fields: list[MarcField]

    @property
    def control_numbers(self) -> list[str]:
        return [f.control_value or "" for f in self.fields if f.tag == "001"]

    @property
    def control_number(self) -> Optional[str]:
        values = self.control_numbers
        return values[0] if values else None

    @property
    def data_fields(self) -> list[MarcField]:
        return [f for f in self.fields if not f.is_control]

    @property
    def subfield_count(self) -> int:
        return sum(len(f.subfields) for f in self.fields)


@dataclass
class Diagnostic:
    record_index: int
    field_index: Optional[int]
    subfield_index: Optional[int]
    code: str
    message: str
    tag: Optional[str] = None
    link_value: Optional[str] = None
    severity: str = "error"
    evidence: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        return {
            "record_index": self.record_index,
            "field_index": "" if self.field_index is None else self.field_index,
            "subfield_index": (
                "" if self.subfield_index is None else self.subfield_index
            ),
            "tag": self.tag or "",
            "link_value": self.link_value or "",
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "evidence": (
                "" if not self.evidence
                else json.dumps(self.evidence, ensure_ascii=False, sort_keys=True)
            ),
        }
