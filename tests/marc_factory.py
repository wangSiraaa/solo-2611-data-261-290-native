"""测试用：用 pymarc 构造合法的 UTF-8 MARC21 记录。"""

from __future__ import annotations

import io

from pymarc import Field, MARCWriter, Record, Subfield


def make_record(
    control_number: str | None,
    fields: list[tuple],
    *,
    leader: str = "00000nam a2200000 a 4500",
    extra_controls: list[tuple[str, str]] | None = None,
) -> Record:
    """fields 为 (tag, indicators 或 None, [(code, value), ...]) 列表。

    tag 以 00 开头且 indicators 为 None 时按控制字段写入。
    """

    record = Record()
    record.leader = leader
    if control_number is not None:
        record.add_field(Field(tag="001", data=control_number))
    for tag, value in extra_controls or []:
        record.add_field(Field(tag=tag, data=value))
    for tag, indicators, subfields in fields:
        if indicators is None:
            record.add_field(Field(tag=tag, data=subfields))
        else:
            record.add_field(
                Field(
                    tag=tag,
                    indicators=list(indicators),
                    subfields=[Subfield(code, value) for code, value in subfields],
                )
            )
    return record


def write_records(records: list[Record]) -> bytes:
    out = io.BytesIO()
    writer = MARCWriter(out)
    for record in records:
        writer.write(record)
    return out.getvalue()


def build(records_spec: list[tuple]) -> bytes:
    """records_spec: [(control_number, fields), ...]；fields 同 make_record。"""

    records = []
    for spec in records_spec:
        control_number, fields = spec[0], spec[1]
        kwargs = {}
        if len(spec) > 2:
            kwargs["extra_controls"] = spec[2]
        records.append(make_record(control_number, fields, **kwargs))
    return write_records(records)
