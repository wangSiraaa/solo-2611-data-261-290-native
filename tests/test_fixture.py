"""针对 fixtures/catalog.mrc 的验收测试，并独立用 pymarc 核对字段数与顺序。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pymarc import MARCReader, MARCWriter, Record, Field, Subfield

from marctools.extract import extract_file, extract_bytes, summary_row

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "catalog.mrc"


@pytest.fixture(scope="module")
def result():
    return extract_file(FIXTURE)


def test_record_count_and_source_identity(result):
    assert result.total_records == 3
    assert result.read_records == 3
    assert result.failed_records == 0
    assert sorted(r.record_index for r in result.records) == [0, 1, 2]


def test_duplicate_control_numbers_are_not_merged():
    data = FIXTURE.read_bytes()
    # fixture 本身就有来自不同记录、本应互不干扰的重复控制号样式输入：
    # 这里再加两条 001 相同的记录，确认来源由记录序号而非 001 决定。
    from tests.marc_factory import build

    dup = build(
        [
            (
                "SAME",
                [
                    ("245", (" ", " "), [("6", "880-01"), ("a", "One")]),
                    ("880", (" ", " "), [("6", "245-01/$1"), ("a", "一")]),
                ],
            ),
            (
                "SAME",
                [
                    ("245", (" ", " "), [("6", "880-01"), ("a", "Two")]),
                    ("880", (" ", " "), [("6", "245-01/$1"), ("a", "二")]),
                ],
            ),
        ]
    )
    res = extract_bytes(data + dup, source_name="combined")
    assert res.total_records == 5
    assert res.read_records == 5
    assert [r.record_index for r in res.records] == [0, 1, 2, 3, 4]
    assert [r.control_number for r in res.records] == ["0001", "0002", "0003", "SAME", "SAME"]
    # 编号 01 只在各自记录内匹配，不因 001 相同而串记录。
    for record_index in (3, 4):
        assert res.pair_for(record_index, 1) == 2


def test_dual_script_title_pairs(result):
    pair = [
        r
        for r in result.pairing_rows
        if r["record_index"] == 0 and r["status"] == "paired"
    ]
    assert len(pair) == 1
    row = pair[0]
    assert row["tag"] == "245"
    assert row["nn"] == "01"
    assert row["suffix"] == "/$1"
    assert row["field_index"] == 1
    assert row["substitute_field_index"] == 2
    assert row["link_raw"] == "880-01"
    assert row["substitute_link_raw"] == "245-01/$1"
    # 两边子字段数量不同（3 vs 2）仍保留整字段配对，不做逐项对齐。
    assert row["ordinary_subfield_count"] == 3
    assert row["substitute_subfield_count"] == 2


def test_isolated_00_and_no_link_fields(result):
    fields = {r["field_index"]: r for r in result.field_rows if r["record_index"] == 0}
    assert fields[4]["tag"] == "880"
    assert fields[4]["link_status"] == "orphan_00"
    assert fields[4]["link_nn"] == "00"
    assert fields[3]["tag"] == "650"
    assert fields[3]["link_status"] == "no_link"
    assert result.isolated_880_count == 2
    diagnostic_codes = {d.code for d in result.diagnostics}
    assert "orphan_880_00" in diagnostic_codes


def test_repeated_subject_subfields_keep_occurrence_order(result):
    subs = [
        r for r in result.subfield_rows if r["record_index"] == 0 and r["field_index"] == 3
    ]
    assert [s["code"] for s in subs] == ["a", "x", "x"]
    assert [s["value"] for s in subs] == ["数据", "工程", "入门"]
    assert [s["subfield_index"] for s in subs] == [0, 1, 2]


def test_ambiguous_link_is_not_forced(result):
    ambiguous = [
        r
        for r in result.pairing_rows
        if r["record_index"] == 2 and r["status"] == "ambiguous"
    ]
    assert len(ambiguous) == 3  # 两个普通字段各一行 + 一个 880
    assert all(r["pair_id"] == "" for r in ambiguous)
    ordinary = [r for r in ambiguous if r["role"] == "ordinary"]
    assert len(ordinary) == 2
    assert all(r["candidate_field_indices"] == "3" for r in ordinary)
    assert all(r["substitute_field_index"] == "" for r in ordinary)
    sub_880 = [r for r in ambiguous if r["role"] == "880"][0]
    assert sub_880["candidate_field_indices"] == "1|2"
    assert sub_880["pair_id"] == ""
    assert any(
        d.code == "ambiguous_link_candidates" and d.record_index == 2
        for d in result.diagnostics
    )


def test_summary_does_not_substitute_pair_count_for_records(result):
    summary = summary_row(result)
    assert summary["total_records"] == 3
    assert summary["read_records"] == 3
    assert summary["paired_field_pairs"] == 2
    assert summary["isolated_880_fields"] == 2


def test_field_count_and_order_match_pymarc():
    """独立用 MARC 读取库核对原字段数及顺序。"""

    with FIXTURE.open("rb") as fh:
        records = list(MARCReader(fh, to_unicode=True, force_utf8=True))
    res = extract_file(FIXTURE)
    assert len(records) == len(res.records)
    for index, (reference, parsed) in enumerate(zip(records, res.records)):
        assert parsed.record_index == index
        assert list(reference.leader) == list(parsed.leader)
        reference_tags = [f.tag for f in reference.fields]
        parsed_tags = [f.tag for f in parsed.fields]
        assert reference_tags == parsed_tags
        assert reference_tags == [
            r["tag"]
            for r in res.field_rows
            if r["record_index"] == index
        ]
        for ref_field, our_field in zip(reference.fields, parsed.fields):
            if ref_field.is_control_field():
                assert our_field.control_value == ref_field.data
            else:
                assert our_field.indicator1 == ref_field.indicator1 or (
                    our_field.indicator1 == ""
                    and ref_field.indicator1 == " "
                )
                ref_subs = [
                    (s.code, s.value)
                    for s in (ref_field.subfields or [])
                ]
                our_subs = [(s.code, s.value) for s in our_field.subfields]
                assert ref_subs == our_subs


def test_pymarc_independent_subfield_order_on_repeated_tags():
    with FIXTURE.open("rb") as fh:
        records = list(MARCReader(fh, to_unicode=True, force_utf8=True))
    res = extract_file(FIXTURE)
    for reference, parsed in zip(records, res.records):
        reference_880 = [
            [(s.code, s.value) for s in f.subfields]
            for f in reference.fields
            if f.tag == "880"
        ]
        parsed_880 = [
            [(s.code, s.value) for s in f.subfields]
            for f in parsed.fields
            if f.tag == "880"
        ]
        assert reference_880 == parsed_880
