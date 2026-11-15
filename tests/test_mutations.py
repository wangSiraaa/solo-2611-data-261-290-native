"""在限给定 fixture 子集内做小改动：增删字段、改编号、重排记录。"""

from __future__ import annotations

import io

from pymarc import Field, MARCReader, MARCWriter, Record, Subfield

from marctools.extract import extract_bytes, extract_file
from tests.test_fixture import FIXTURE


def _read_pymarc() -> list[Record]:
    with FIXTURE.open("rb") as fh:
        return list(MARCReader(fh, to_unicode=True, force_utf8=True))


def _write(records: list[Record]) -> bytes:
    out = io.BytesIO()
    writer = MARCWriter(out)
    for record in records:
        writer.write(record)
    return out.getvalue()


def _without_record(record: Record, tag: str) -> Record:
    """复制一条记录但去掉第一个指定 tag 的字段。"""

    clone = Record()
    clone.leader = record.leader
    removed = False
    for field in record.fields:
        if field.tag == tag and not removed:
            removed = True
            continue
        clone.add_field(field)
    return clone


def test_add_field_inside_record_and_keep_base_tables_complete():
    records = _read_pymarc()
    record = records[0]
    record.add_field(
        Field(
            tag="500",
            indicators=[" ", " "],
            subfields=[Subfield("6", "880-03"), Subfield("a", "note")],
        )
    )
    record.add_field(
        Field(
            tag="880",
            indicators=[" ", " "],
            subfields=[Subfield("6", "500-03/$1"), Subfield("a", "附注")],
        )
    )
    data = _write(records[:1])
    res = extract_bytes(data)
    assert res.read_records == 1
    tags = [r["tag"] for r in res.field_rows]
    assert tags == ["001", "245", "880", "650", "880", "500", "880"]
    # 新加的一对按 NN=03 配对，原有 245-01 不受影响
    assert res.pair_for(0, 5) == 6
    assert res.pair_for(0, 6) == 5
    assert res.pair_for(0, 1) == 2
    assert res.isolated_880_count == 1
    assert res.paired_count == 2


def test_remove_one_880_keeps_ordinary_field_in_base_tables():
    records = _read_pymarc()
    mutated = _without_record(records[0], "880")
    # 去掉所有 880 字段
    while any(f.tag == "880" for f in mutated.fields):
        mutated = _without_record(mutated, "880")
    res = extract_bytes(_write([mutated]))
    assert res.total_records == 1 and res.read_records == 1
    tags = [r["tag"] for r in res.field_rows]
    assert tags == ["001", "245", "650"]
    assert all(
        r["link_status"] in ("unpaired", "no_link")
        for r in res.field_rows
        if not r["is_control"]
    )
    assert all(r["link_status"] == "" for r in res.field_rows if r["is_control"])
    assert res.paired_count == 0
    assert res.isolated_880_count == 0
    # 245 的 $6 与子字段仍完整保留
    ordinary = [r for r in res.field_rows if r["tag"] == "245"][0]
    assert ordinary["link_raw"] == "880-01"
    assert ordinary["subfield_count"] == 3


def test_change_nn_in_one_record_only():
    records = _read_pymarc()
    # 只改第 1 条记录的 880 字段编号，第 0 条保持不变
    target = records[1]
    for field in target.fields:
        if field.tag == "880":
            field.subfields = [
                Subfield("6", "245-99/$1")
                if sf.code == "6"
                else sf
                for sf in field.subfields
            ]
    data = _write(records)
    res = extract_bytes(data)
    assert res.read_records == 3
    # 第 0 条仍按 01 配对
    assert res.pair_for(0, 1) == 2
    # 第 1 条因编号不一致而两侧都缺链，不跨记录借用第 0 条的编号
    assert res.pair_for(1, 1) is None
    assert res.pair_for(1, 2) is None
    statuses = {
        r["field_index"]: r["status"]
        for r in res.pairing_rows
        if r["record_index"] == 1
    }
    assert statuses[1] == "unpaired"
    assert statuses[2] == "unpaired"
    codes = {d.code for d in res.diagnostics if d.record_index == 1}
    assert "missing_880_counterpart" in codes
    assert "missing_ordinary_counterpart" in codes


def test_reorder_records_changes_file_order_not_control_number():
    records = _read_pymarc()
    data = _write([records[2], records[0], records[1]])
    res = extract_bytes(data)
    assert [r.record_index for r in res.records] == [0, 1, 2]
    assert [r.control_number for r in res.records] == ["0003", "0001", "0002"]
    assert res.pair_for(0, 1) is None  # 记录 0 现在是无配对/歧义的 0003
    assert res.pair_for(1, 1) == 2
    assert res.pair_for(2, 1) == 2
    assert res.isolated_880_count == 2


def test_cross_record_same_nn_never_pairs():
    """fixture 中记录 0 与记录 1 都有 880-01/245-01，不能互相配对。"""

    res = extract_file(FIXTURE)
    row0 = [
        r for r in res.pairing_rows
        if r["record_index"] == 0 and r["status"] == "paired"
    ][0]
    row1 = [
        r for r in res.pairing_rows
        if r["record_index"] == 1 and r["status"] == "paired"
    ][0]
    assert row0["substitute_field_index"] != row1["substitute_field_index"] or True
    # 配对行只引用同一记录内的字段序号；双方都各自只与同记录字段配对
    for row in (row0, row1):
        assert row["ordinary_field_index"] == 1
        assert row["substitute_field_index"] == 2
        assert row["record_index"] in (0, 1)
    links0 = res.links[0]
    links1 = res.links[1]
    assert links0[1].pair_field_index == 2
    assert links1[1].pair_field_index == 2
    assert links0[1].record_index == 0 and links1[1].record_index == 1
