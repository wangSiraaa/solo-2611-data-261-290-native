"""严格的 MARC21 结构解析，保留字段/子字段出现身份与字节范围。

只处理 UTF-8、ISO 2709 目录结构；不解释 MARC-8、不翻译、不合并实体。

解析以原始字节为准：leader、目录项、字段与子字段的起点/长度都是字节值，
UTF-8 多字节字符不会让“字符下标”和“字节偏移”混淆。整条记录先做严格
UTF-8 解码；解码失败由 reader 层判为记录失败。
"""

from __future__ import annotations

from .models import Diagnostic, MarcField, ParsedRecord, Subfield
from .reader import RecordText

FIELD_TERMINATOR = 0x1E
RECORD_TERMINATOR = 0x1D
SUBFIELD_DELIMITER = 0x1F


class RecordParseError(ValueError):
    """目录结构不可信，整条记录无法可靠解析。"""


def _ascii(data: bytes) -> str:
    return data.decode("ascii")


def _safe_text(data: bytes) -> str:
    """整条记录已验证 UTF-8，这里的切片位于 ASCII 分隔符边界，可严格解码。"""

    return data.decode("utf-8", errors="strict")


def _is_control_tag(tag: str, control_tags: set[str]) -> bool:
    """控制字段由 scope 指定（本题为 001/005），其余一律按数据字段保存。

    题面只要求处理 001/005 的控制字段语义；不把其余 00X 的 MARC21
    控制字段规则扩展进来。
    """

    return tag in control_tags


def _first_char_length(data: bytes, start: int) -> int:
    """返回从 start 开始的第一个 UTF-8 字符占用字节数（用于异常 code）。"""

    for length in range(1, 5):
        chunk = data[start : start + length]
        if not chunk:
            return 0
        try:
            chunk.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and length < 4:
                continue
            return 1
        else:
            return length
    return 1


def _parse_subfields(
    body_after_indicators: bytes,
    body_abs_start: int,
    field_index: int,
    record_index: int,
    diagnostics: list[Diagnostic],
    tag: str,
) -> list[Subfield]:
    """按出现次序解析数据字段正文中 indicator 之后的部分。

    body_after_indicators 的字节偏移由 body_abs_start 给出；重复 code 全部保留，
    绝不把子字段放进普通 dict 而覆盖重复项。
    """

    subfields: list[Subfield] = []
    pos = 0
    subfield_index = 0
    length = len(body_after_indicators)

    first = body_after_indicators.find(bytes([SUBFIELD_DELIMITER]))
    if first == -1:
        if length:
            diagnostics.append(
                Diagnostic(
                    record_index=record_index,
                    field_index=field_index,
                    subfield_index=None,
                    code="data_without_subfield_delimiter",
                    tag=tag,
                    message=(
                        "数据字段在 indicator 之后没有 0x1F 子字段分隔符，"
                        "无法按子字段解析；字段边界仍可信"
                    ),
                    severity="error",
                    evidence={"body": _safe_text(body_after_indicators)},
                )
            )
        return subfields

    if first > 0:
        diagnostics.append(
            Diagnostic(
                record_index=record_index,
                field_index=field_index,
                subfield_index=None,
                code="data_before_first_subfield",
                tag=tag,
                message=(
                    f"indicator 之后、首个 0x1F 之前有 {first} 个字节，"
                    "不属于任何子字段；不猜测其含义"
                ),
                severity="error",
                evidence={
                    "prefix": _safe_text(body_after_indicators[:first]),
                    "byte_start": body_abs_start,
                    "byte_end": body_abs_start + first,
                },
            )
        )

    while pos < length:
        delimiter = body_after_indicators.find(bytes([SUBFIELD_DELIMITER]), pos)
        if delimiter == -1:
            break
        code_start = delimiter + 1
        next_delimiter = length

        if code_start >= length:
            code = ""
            value_start = code_start
            diagnostics.append(
                Diagnostic(
                    record_index=record_index,
                    field_index=field_index,
                    subfield_index=subfield_index,
                    tag=tag,
                    code="trailing_subfield_delimiter",
                    message="0x1F 后缺少子字段 code，已保留为空 code 的出现",
                    severity="error",
                    evidence={
                        "byte_start": body_abs_start + delimiter,
                        "byte_end": body_abs_start + length,
                    },
                )
            )
        else:
            code_byte = body_after_indicators[code_start]
            if code_byte < 0x80:
                code = chr(code_byte)
                value_start = code_start + 1
            else:
                code_len = _first_char_length(body_after_indicators, code_start)
                code = _safe_text(
                    body_after_indicators[code_start : code_start + code_len]
                )
                value_start = code_start + code_len
                diagnostics.append(
                    Diagnostic(
                        record_index=record_index,
                        field_index=field_index,
                        subfield_index=subfield_index,
                        tag=tag,
                        code="non_ascii_subfield_code",
                        message="子字段 code 不是单字节 ASCII，按 UTF-8 字符保留",
                        severity="error",
                        evidence={"code": code},
                    )
                )

            next_delimiter = body_after_indicators.find(
                bytes([SUBFIELD_DELIMITER]), value_start
            )
            if next_delimiter == -1:
                next_delimiter = length

        value = _safe_text(body_after_indicators[value_start:next_delimiter])

        delim_abs = body_abs_start + delimiter
        value_abs = body_abs_start + value_start
        end_abs = body_abs_start + next_delimiter
        subfields.append(
            Subfield(
                subfield_index=subfield_index,
                code=code,
                value=value,
                byte_start=delim_abs,
                byte_end=end_abs,
                value_byte_start=value_abs,
                value_byte_end=end_abs,
            )
        )
        subfield_index += 1
        pos = next_delimiter

    return subfields


def parse_record(
    decoded: RecordText,
    control_tags: set[str],
    max_fields: int | None = None,
) -> tuple[ParsedRecord | None, list[Diagnostic]]:
    """解析一条已成功 UTF-8 解码的记录。

    返回 (记录, 诊断)。结构错误返回 None，并给出 record_parse_error 诊断。
    """

    raw_bytes = decoded.raw.raw
    record_index = decoded.raw.record_index
    record_byte_start = decoded.raw.byte_start
    diagnostics: list[Diagnostic] = []

    def fail(message: str, evidence: dict | None = None):
        diagnostics.append(
            Diagnostic(
                record_index=record_index,
                field_index=None,
                subfield_index=None,
                code="record_parse_error",
                message=message,
                severity="error",
                evidence=evidence or {},
            )
        )
        raise RecordParseError(message)

    try:
        if len(raw_bytes) < 24:
            fail("记录少于 24 字节 leader", {"length": len(raw_bytes)})
        try:
            leader = _ascii(raw_bytes[:24])
        except UnicodeDecodeError:
            fail("leader 不是 ASCII", {"leader_hex": raw_bytes[:24].hex()})

        if not leader[0:5].isdigit():
            fail("leader 的记录长度不是 5 位数字", {"leader": leader})
        declared_size = int(leader[0:5])
        if declared_size != len(raw_bytes):
            fail(
                "leader 记录长度与记录字节范围不一致",
                {"declared": declared_size, "actual": len(raw_bytes)},
            )
        if raw_bytes[-1] != RECORD_TERMINATOR:
            fail("记录末尾不是 0x1D", {"last_byte": f"0x{raw_bytes[-1]:02X}"})
        if not leader[12:17].isdigit():
            fail("leader 的 base address 不是 5 位数字", {"leader": leader})
        base_address = int(leader[12:17])

        if base_address < 25:
            fail("base address 小于 25", {"base_address": base_address})
        if base_address > len(raw_bytes):
            fail(
                "base address 超出记录范围",
                {"base_address": base_address, "length": len(raw_bytes)},
            )
        if raw_bytes[base_address - 1] != FIELD_TERMINATOR:
            fail(
                "目录区末尾缺少 0x1E 字段终止符",
                {"base_address": base_address},
            )

        directory_bytes = raw_bytes[24 : base_address - 1]
        if len(directory_bytes) % 12 != 0:
            fail(
                "目录长度不是 12 的倍数",
                {"directory_length": len(directory_bytes)},
            )
        entry_count = len(directory_bytes) // 12
        if max_fields is not None and entry_count > max_fields:
            # scope.json 的字段数上限用于界定输入范围；超出时只提示，
            # 不因此丢弃可解析的字段数据。
            diagnostics.append(
                Diagnostic(
                    record_index=record_index,
                    field_index=None,
                    subfield_index=None,
                    code="field_count_exceeds_scope",
                    message=(
                        f"目录字段数 {entry_count} 超过 scope 上限 "
                        f"{max_fields}；仍完整解析"
                    ),
                    severity="warning",
                    evidence={
                        "entry_count": entry_count,
                        "max_fields": max_fields,
                    },
                )
            )

        fields: list[MarcField] = []
        tag_counts: dict[str, int] = {}
        data_area_end = len(raw_bytes) - 1  # 不含记录终止符

        for entry_index in range(entry_count):
            entry = directory_bytes[entry_index * 12 : (entry_index + 1) * 12]
            try:
                tag = _ascii(entry[0:3])
            except UnicodeDecodeError:
                fail(
                    "目录项 tag 不是 ASCII",
                    {"field_index": entry_index, "entry_hex": entry.hex()},
                )
            length_bytes = entry[3:7]
            start_bytes = entry[7:12]
            if not length_bytes.isdigit() or not start_bytes.isdigit():
                fail(
                    "目录项的长度或起始位置不是数字",
                    {"field_index": entry_index, "directory_entry": entry.hex()},
                )
            field_length = int(length_bytes)
            relative_start = int(start_bytes)
            field_offset = base_address + relative_start
            field_offset_end = field_offset + field_length

            if field_length < 1:
                fail(
                    "目录项长度小于 1",
                    {"field_index": entry_index, "directory_entry": entry.hex()},
                )
            if field_offset < base_address or field_offset_end > data_area_end:
                fail(
                    "字段范围超出可变字段区",
                    {
                        "field_index": entry_index,
                        "directory_entry": entry.hex(),
                        "byte_start": field_offset,
                        "byte_end": field_offset_end,
                        "data_area_end": data_area_end,
                    },
                )
            if raw_bytes[field_offset_end - 1] != FIELD_TERMINATOR:
                fail(
                    "字段末尾不是 0x1E",
                    {
                        "field_index": entry_index,
                        "directory_entry": entry.hex(),
                        "last_byte": f"0x{raw_bytes[field_offset_end - 1]:02X}",
                    },
                )

            # 对外保存文件内绝对字节偏移；记录内部计算使用相对偏移。
            byte_start = record_byte_start + field_offset
            byte_end = record_byte_start + field_offset_end

            body_bytes = raw_bytes[field_offset : field_offset_end - 1]
            body = _safe_text(body_bytes)
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            control = _is_control_tag(tag, control_tags)

            if control:
                field_obj = MarcField(
                    field_index=entry_index,
                    tag_occurrence=tag_counts[tag],
                    tag=tag,
                    is_control=True,
                    indicator1=None,
                    indicator2=None,
                    control_value=body,
                    subfields=[],
                    byte_start=byte_start,
                    byte_end=byte_end - 1,
                    terminator_byte=byte_end - 1,
                    directory_entry=entry.hex(),
                )
            else:
                indicator1 = ""
                indicator2 = ""
                prefix_bytes = 0
                rest_bytes = b""
                if len(body) < 2:
                    diagnostics.append(
                        Diagnostic(
                            record_index=record_index,
                            field_index=entry_index,
                            subfield_index=None,
                            tag=tag,
                            code="short_data_field",
                            message=(
                                "数据字段正文不足两个 indicator 字符；"
                                "字段边界可信但 indicator 缺失"
                            ),
                            severity="error",
                            evidence={"body": body},
                        )
                    )
                    if len(body) >= 1:
                        indicator1 = body[0]
                        prefix_bytes = len(body[0].encode("utf-8"))
                    rest_bytes = body_bytes[prefix_bytes:]
                else:
                    indicator1 = body[0]
                    indicator2 = body[1]
                    prefix_bytes = len(indicator1.encode("utf-8")) + len(
                        indicator2.encode("utf-8")
                    )
                    rest_bytes = body_bytes[prefix_bytes:]

                subfields = _parse_subfields(
                    body_after_indicators=rest_bytes,
                    body_abs_start=record_byte_start + field_offset + prefix_bytes,
                    field_index=entry_index,
                    record_index=record_index,
                    diagnostics=diagnostics,
                    tag=tag,
                )
                field_obj = MarcField(
                    field_index=entry_index,
                    tag_occurrence=tag_counts[tag],
                    tag=tag,
                    is_control=False,
                    indicator1=indicator1,
                    indicator2=indicator2,
                    control_value=None,
                    subfields=subfields,
                    byte_start=byte_start,
                    byte_end=byte_end - 1,
                    terminator_byte=byte_end - 1,
                    directory_entry=entry.hex(),
                )
            fields.append(field_obj)

        return (
            ParsedRecord(
                record_index=record_index,
                byte_start=decoded.raw.byte_start,
                byte_end=decoded.raw.byte_end,
                leader=leader,
                base_address=base_address,
                fields=fields,
            ),
            diagnostics,
        )
    except RecordParseError:
        return None, diagnostics
    except (ValueError, IndexError) as exc:
        diagnostics.append(
            Diagnostic(
                record_index=record_index,
                field_index=None,
                subfield_index=None,
                code="record_parse_error",
                message=f"记录结构解析异常：{exc}",
                severity="error",
            )
        )
        return None, diagnostics
