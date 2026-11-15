"""链接语义、变动子集、损坏记录边界等测试。"""

from __future__ import annotations

import io
import json

import pytest
from pymarc import MARCReader, MARCWriter

from marctools.extract import extract_bytes, extract_file, summary_row
from marctools.inspect import inspect_file, inspect_pair
from marctools.link import build_links
from marctools.reader import split_records
from tests.marc_factory import build


def _pair(res, record_index=0, field_index=1):
    for row in res.pairing_rows:
        if row["record_index"] == record_index and row["field_index"] == field_index:
            return row
    return None


def test_add_field_and_880_keeps_occurrence_order():
    base = build(
        [
            (
                "A",
                [
                    ("245", (" ", " "), [("6", "880-01"), ("a", "T")]),
                    ("880", (" ", " "), [("6", "245-01/$1"), ("a", "题")]),
                ],
            )
        ]
    )
    added = build(
        [
            (
                "B",
                [
                    ("245", (" ", " "), [("6", "880-02"), ("a", "T2")]),
                    ("880", (" ", " "), [("6", "245-02/$1"), ("a", "题2")]),
                ],
            )
        ]
    )
    res = extract_bytes(base + added)
    assert res.read_records == 2
    # 第二条记录的字段序号重新从 0 起，与第一条互不干扰
    tags = [(r["record_index"], r["field_index"], r["tag"]) for r in res.field_rows]
    assert tags == [(0, 0, "001"), (0, 1, "245"), (0, 2, "880"),
                    (1, 0, "001"), (1, 1, "245"), (1, 2, "880")]


def test_remove_880_gives_missing_reverse_link_diagnostic():
    data = build(
        [
            (
                "A",
                [
                    ("245", (" ", " "), [("6", "880-01"), ("a", "T")]),
                    ("650", (" ", " "), [("a", "Subject")]),
                ],
            )
        ]
    )
    res = extract_bytes(data)
    row = _pair(res, 0, 1)
    assert row is not None and row["status"] == "unpaired"
    assert row["substitute_field_index"] == ""
    assert any(d.code == "missing_880_counterpart" for d in res.diagnostics)
    # 未配对普通字段仍完整进入基础表
    assert any(
        r["field_index"] == 1 and r["tag"] == "245" for r in res.field_rows
    )
    assert any(
        r["tag"] == "245" and r["subfield_index"] == 1
        for r in res.subfield_rows
        if r["record_index"] == 0
    )


def test_change_number_prevents_pairing():
    data = build(
        [
            (
                "A",
                [
                    ("245", (" ", " "), [("6", "880-01"), ("a", "T")]),
                    ("880", (" ", " "), [("6", "245-02/$1"), ("a", "题")]),
                ],
            )
        ]
    )
    res = extract_bytes(data)
    assert _pair(res, 0, 1)["status"] == "unpaired"
    assert _pair(res, 0, 2)["status"] == "unpaired"
    assert any(d.code == "missing_880_counterpart" for d in res.diagnostics)
    assert any(d.code == "missing_ordinary_counterpart" for d in res.diagnostics)


def test_same_number_different_tag_does_not_pair():
    data = build(
        [
            (
                "A",
                [
                    ("245", (" ", " "), [("6", "880-02"), ("a", "T")]),
                    ("880", (" ", " "), [("6", "650-02/$1"), ("a", "题")]),
                ],
            )
        ]
    )
    res = extract_bytes(data)
    assert _pair(res, 0, 1)["status"] == "unpaired"
    assert _pair(res, 0, 2)["status"] == "unpaired"
    # 编号相同但 TAG 不同不构成对应
    assert not [r for r in res.pairing_rows if r["status"] == "paired"]


def test_suffix_saved_but_not_used_for_matching():
    data = build(
        [
            (
                "A",
                [
                    ("245", (" ", " "), [("6", "880-01/$1"), ("a", "T")]),
                    ("880", (" ", " "), [("6", "245-01/$2"), ("a", "题")]),
                ],
            )
        ]
    )
    res = extract_bytes(data)
    row = _pair(res, 0, 1)
    assert row["status"] == "paired"
    assert row["suffix"] == "/$1"
    sub_link = [r for r in res.field_rows if r["field_index"] == 2][0]
    assert sub_link["link_suffix"] == "/$2"
    # 后缀不用于猜语言，也不影响 tag/nn 匹配


def test_reorder_records_preserves_source_indices():
    first = build(
        [("A", [("245", (" ", " "), [("6", "880-01"), ("a", "A")]),
                ("880", (" ", " "), [("6", "245-01/$1"), ("a", "甲")])])]
    )
    second = build(
        [("B", [("245", (" ", " "), [("6", "880-01"), ("b", "B")]),
                ("880", (" ", " "), [("6", "245-01/$1"), ("b", "乙")])])]
    )
    res = extract_bytes(second + first)
    assert [r.record_index for r in res.records] == [0, 1]
    assert [r.control_number for r in res.records] == ["B", "A"]
    assert res.pair_for(0, 1) == 2
    assert res.pair_for(1, 1) == 2


def test_no_subfield6_ordinary_field_is_complete_and_unpaired():
    data = build(
        [
            (
                "A",
                [
                    ("245", ("1", "0"), [("a", "T"), ("b", "rest")]),
                    ("650", (" ", "0"), [("a", "S"), ("x", "x")]),
                ],
            )
        ]
    )
    res = extract_bytes(data)
    field = [r for r in res.field_rows if r["record_index"] == 0 and r["field_index"] == 1][0]
    assert field["link_has_subfield6"] == 0
    assert field["link_status"] == "no_link"
    assert field["indicator1"] == "1" and field["indicator2"] == "0"
    assert _pair(res, 0, 1) is None  # 配对表不为无 $6 字段伪造关系
    assert len([r for r in res.subfield_rows if r["record_index"] == 0 and r["field_index"] == 1]) == 2


def test_multiple_subfield6_diagnostic_and_no_pick():
    data = build(
        [
            (
                "A",
                [
                    (
                        "245",
                        (" ", " "),
                        [("6", "880-01"), ("6", "880-02"), ("a", "T")],
                    ),
                    ("880", (" ", " "), [("6", "245-01/$1"), ("a", "甲")]),
                ],
            )
        ]
    )
    res = extract_bytes(data)
    field = [r for r in res.field_rows if r["field_index"] == 1][0]
    assert field["link_status"] == "multiple"
    assert field["pair_field_index"] == ""
    codes = {d.code for d in res.diagnostics}
    assert "multiple_link_subfields" in codes
    # 880 缺少可唯一配对的普通字段
    assert _pair(res, 0, 2)["status"] == "unpaired"


def test_invalid_syntax_diagnostic():
    bad_values = ["880-1", "880-001", "880-aa", "880", "245-01", "880-01 extra", "880-01/x y"]
    fields = []
    for i, value in enumerate(bad_values):
        fields.append(("245", (" ", " "), [("6", value), ("a", f"T{i}")]))
    data = build([("A", fields)])
    res = extract_bytes(data)
    invalid = [r for r in res.field_rows if r["link_status"] == "invalid"]
    assert len(invalid) == len(bad_values)
    assert {d.code for d in res.diagnostics} >= {"invalid_link_syntax"}
    # 不合法语法不得因“看起来像”而配对
    assert not [r for r in res.pairing_rows if r["status"] == "paired"]


def test_00_on_ordinary_field_is_invalid():
    data = build(
        [("A", [("245", (" ", " "), [("6", "880-00"), ("a", "T")])])]
    )
    res = extract_bytes(data)
    assert [r for r in res.field_rows if r["field_index"] == 1][0]["link_status"] == "invalid"
    assert any(
        d.code == "link_number_00_on_ordinary_field" for d in res.diagnostics
    )


def test_orphan_breaks_into_diagnostic_not_pair():
    data = build(
        [
            (
                "A",
                [
                    ("245", (" ", " "), [("6", "880-01"), ("a", "T")]),
                    ("880", (" ", " "), [("6", "245-01/$1"), ("a", "题")]),
                    ("880", (" ", " "), [("6", "245-00/$1"), ("a", "孤")]),
                ],
            )
        ]
    )
    res = extract_bytes(data)
    assert res.isolated_880_count == 1
    assert res.paired_count == 1
    orphan = [r for r in res.field_rows if r["field_index"] == 3][0]
    assert orphan["link_status"] == "orphan_00"
    assert orphan["pair_field_index"] == ""


def test_inspect_returns_both_sides_and_subfield6_evidence(tmp_path):
    path = tmp_path / "x.mrc"
    path.write_bytes(build(
        [("A", [("245", (" ", " "), [("6", "880-01"), ("a", "T"), ("b", "B")]),
                ("880", (" ", " "), [("6", "245-01/$1"), ("a", "题")])])]
    ))
    payload = inspect_file(path, 0, 2)
    assert payload["ok"] is True
    assert payload["status"] == "paired"
    assert payload["field"]["field_index"] == 2
    assert payload["counterpart"]["field_index"] == 1
    assert payload["field"]["subfield6"][0]["value"] == "245-01/$1"
    assert payload["counterpart"]["subfield6"][0]["value"] == "880-01"
    assert payload["pair_evidence"]["nn"] == "01"
    # 原始字节范围可回查
    raw = path.read_bytes()
    f = payload["field"]
    assert raw[f["byte_start"]:f["terminator_byte"]].decode("utf-8").startswith("  \x1f6")


def test_inspect_ambiguous_lists_all_candidates(tmp_path):
    path = tmp_path / "x.mrc"
    path.write_bytes(build(
        [("A", [("245", (" ", " "), [("6", "880-01"), ("a", "T")]),
                ("880", (" ", " "), [("6", "245-01/$1"), ("a", "甲")]),
                ("880", (" ", " "), [("6", "245-01/$1"), ("a", "乙")])])]
    ))
    payload = inspect_file(path, 0, 1)
    assert payload["status"] == "ambiguous"
    assert payload["pair_evidence"]["candidates"] == [2, 3]
    assert payload["counterpart"] is None


def test_invalid_utf8_record_fails_but_later_records_survive():
    good1 = build([("A", [("245", (" ", " "), [("a", "A")])])])
    bad = build([("BAD", [("245", (" ", " "), [("a", "题")])])])
    good2 = build([("C", [("245", (" ", " "), [("a", "C")])])])
    # 让 bad 记录的某个 UTF-8 首字节孤立，但长度字段保持可信。
    marker = "题".encode("utf-8")
    # 用等长（3 字节）的非法序列替换，保持 leader 长度可信。
    bad_corrupt = bad.replace(marker, b"\xff\xfe\xfd", 1)
    data = good1 + bad_corrupt + good2
    res = extract_bytes(data)
    assert res.total_records == 3
    assert res.read_records == 2
    assert res.failed_records == 1
    assert [r.control_number for r in res.records] == ["A", "C"]
    assert [f["record_index"] for f in res.record_failures] == [1]
    assert "UTF-8" in res.record_failures[0]["reason"]


def test_bad_leader_length_uses_record_terminator_boundary():
    good1 = build([("A", [("245", (" ", " "), [("a", "A")])])])
    good2 = build([("C", [("245", (" ", " "), [("a", "C")])])])
    bad = bytearray(build([("B", [("245", (" ", " "), [("a", "B")])])]))
    bad[0:5] = b"99999"  # leader 长度不可信，但 0x1D 仍可确认边界
    res = extract_bytes(good1 + bytes(bad) + good2)
    assert res.total_records == 3
    # 结构不可信 -> 该记录失败，但后续记录仍按边界读取
    assert res.read_records == 2
    assert res.failed_records == 1
    assert [r.control_number for r in res.records] == ["A", "C"]


def test_record_without_terminator_is_failed_not_guessed():
    good = build([("A", [("245", (" ", " "), [("a", "A")])])])
    broken = build([("B", [("245", (" ", " "), [("a", "B")])])])[:-1]
    res = extract_bytes(good + broken)
    assert res.total_records == 2
    assert res.read_records == 1
    assert res.failed_records == 1
