"""按配对回查双方原始字段、子字段与 $6 证据。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .extract import ExtractionResult, extract_file
from .models import MarcField, ParsedRecord


@dataclass
class FieldView:
    record_index: int
    field_index: int
    tag: str
    tag_occurrence: int
    indicator1: Optional[str]
    indicator2: Optional[str]
    control_value: Optional[str]
    byte_start: int
    byte_end: int
    terminator_byte: int
    directory_entry: str
    raw_bytes_hex: str
    subfields: list[dict]

    def as_dict(self) -> dict:
        return {
            "record_index": self.record_index,
            "field_index": self.field_index,
            "tag": self.tag,
            "tag_occurrence": self.tag_occurrence,
            "indicator1": self.indicator1,
            "indicator2": self.indicator2,
            "control_value": self.control_value,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "terminator_byte": self.terminator_byte,
            "directory_entry": self.directory_entry,
            "raw_bytes_hex": self.raw_bytes_hex,
            "subfields": self.subfields,
            "subfield6": [
                sf for sf in self.subfields if sf["code"] == "6"
            ],
        }


def _view(record: ParsedRecord, marc_field: MarcField, data: bytes) -> FieldView:
    raw_field = data[marc_field.byte_start : marc_field.terminator_byte + 1]
    return FieldView(
        record_index=record.record_index,
        field_index=marc_field.field_index,
        tag=marc_field.tag,
        tag_occurrence=marc_field.tag_occurrence,
        indicator1=marc_field.indicator1,
        indicator2=marc_field.indicator2,
        control_value=marc_field.control_value,
        byte_start=marc_field.byte_start,
        byte_end=marc_field.byte_end,
        terminator_byte=marc_field.terminator_byte,
        directory_entry=marc_field.directory_entry,
        raw_bytes_hex=raw_field.hex(),
        subfields=[
            {
                "subfield_index": sf.subfield_index,
                "code": sf.code,
                "value": sf.value,
                "byte_start": sf.byte_start,
                "byte_end": sf.byte_end,
                "value_byte_start": sf.value_byte_start,
                "value_byte_end": sf.value_byte_end,
            }
            for sf in marc_field.subfields
        ],
    )


def inspect_pair(
    result: ExtractionResult,
    record_index: int,
    field_index: int,
    *,
    data: Optional[bytes] = None,
) -> dict:
    """给出一条（普通或 880）字段出现，返回配对双方与 $6 证据。

    如果给的是已配对字段，两侧都返回；否则只返回该字段并说明状态。
    """

    record = next(
        (r for r in result.records if r.record_index == record_index), None
    )
    if record is None:
        return {
            "ok": False,
            "error": f"记录 {record_index} 未成功读取",
            "record_index": record_index,
        }
    if field_index < 0 or field_index >= len(record.fields):
        return {
            "ok": False,
            "error": (
                f"字段序号 {field_index} 越界（记录 {record_index} 有 "
                f"{len(record.fields)} 个字段）"
            ),
            "record_index": record_index,
        }

    if data is None:
        raise ValueError(
            "inspect_pair 需要原始文件字节以回查字段范围；"
            "直接调用请传 data=Path(...).read_bytes()，或使用 inspect_file"
        )

    links = result.links.get(record_index, {})
    link = links.get(field_index)
    field = record.fields[field_index]
    view = _view(record, field, data)

    payload: dict = {
        "ok": True,
        "record_index": record_index,
        "requested_field_index": field_index,
        "status": None if link is None else link.status,
        "role": None if link is None else link.role,
        "link_raw_values": [] if link is None else link.raw_values,
        "link_subfield_indices": (
            []
            if link is None
            else [
                sf.subfield_index for sf in field.subfields if sf.code == "6"
            ]
        ),
        "field": view.as_dict(),
        "counterpart": None,
    }

    if link is not None and link.pair_field_index is not None:
        counterpart = record.fields[link.pair_field_index]
        payload["counterpart"] = _view(record, counterpart, data).as_dict()
        payload["pair_evidence"] = {
            "record_index": record_index,
            "ordinary_field_index": (
                link.field_index
                if link.role == "ordinary"
                else link.pair_field_index
            ),
            "substitute_field_index": (
                link.field_index
                if link.role == "880"
                else link.pair_field_index
            ),
            "tag": field.tag if link.role == "ordinary" else counterpart.tag,
            "nn": link.nn,
            "suffix": link.suffix,
            "matched_on": "same record + TAG + NN; both sides unique",
        }
    elif link is not None:
        payload["pair_evidence"] = {
            "record_index": record_index,
            "status": link.status,
            "candidates": link.candidates,
            "explanation": {
                "unpaired": "缺反向链接：记录内没有匹配的对方字段",
                "ambiguous": "候选不唯一，按题意不建立配对",
                "orphan_00": "NN=00 的孤立 880，不参与配对",
                "invalid": "自身 $6 语法不合法",
                "multiple": "同一字段出现多个 $6，不任选一个",
                "no_link": "普通字段不含 $6",
                "no_link_880": "880 字段不含 $6",
            }.get(link.status, "该字段没有配对关系"),
        }

    return payload


def inspect_file(
    path: str | Path,
    record_index: int,
    field_index: int,
    *,
    scope_path: Optional[str | Path] = None,
) -> dict:
    path = Path(path)
    result = extract_file(path, scope_path=scope_path)
    return inspect_pair(
        result,
        record_index,
        field_index,
        data=path.read_bytes(),
    )


def format_inspection(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
