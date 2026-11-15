"""sav_import 的测试：fixture 核对 + 同子集补测（增减行列、变动规则与标签）。

补测约束：只在 survey.sav 的变量子集 {respondent, score, satisfaction, status}
内增减行/列、改动缺失规则与值标签，不引入新变量、不做统计分析。
"""
import csv
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import pyreadstat
from pyreadstat.pyclasses import MissingRange

from sav_import import (
    OBSERVED, SYSTEM, USER,
    SavImportError, import_sav, inspect_cell, load_tables,
)

ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixtures" / "survey.sav"


# ---------------------------------------------------------------- 工具

def cell(records, row_id, variable):
    for r in records:
        if int(r["row_id"]) == row_id and r["variable"] == variable:
            return r
    raise AssertionError(f"cell ({row_id}, {variable}) not found")


def survey_df():
    """与 fixtures/survey.sav 等价的数据。"""
    return pd.DataFrame({
        "respondent": ["001", "002", "003", "004", "005", "006", "007"],
        "score": [0.0, 1.0, -99.0, float("nan"), 90.0, 99.0, 42.0],
        "satisfaction": [1.0, 2.0, 3.0, -99.0, 1.0, 2.0, 3.0],
        "status": ["OK", "NA", "", "OK", "SKIP", "OK", "NA"],
    })


def survey_meta(value_labels=None, missing=None):
    """survey.sav 的元数据（可在副本上改动规则/标签）。"""
    return dict(
        column_labels={"score": "评分", "satisfaction": "满意度", "status": "状态"},
        variable_value_labels=value_labels if value_labels is not None else {
            "score": {0.0: "零分", 1.0: "已答", 42.0: "已答",
                      -99.0: "拒答", 90.0: "保留编码", 99.0: "保留编码"},
            "satisfaction": {1.0: "不满意", 2.0: "一般", 3.0: "满意"},
            "status": {"NA": "未回答", "SKIP": "跳过", "OK": "正常"},
        },
        missing_ranges=missing if missing is not None else {
            "score": [MissingRange(lo=90.0, hi=99.0), -99.0],
            "satisfaction": [-99.0],
            "status": ["NA", "SKIP"],
        },
    )


def write_sav(path, df, **meta):
    pyreadstat.write_sav(df, str(path), **meta)
    return path


@pytest.fixture(scope="module")
def imported(tmp_path_factory):
    out = tmp_path_factory.mktemp("survey_out")
    import_sav(FIXTURE, out)
    return out, load_tables(out)


# ---------------------------------------------------------------- fixture 核对

def test_variables_order_labels_types(imported):
    _, t = imported
    vs = t["variables"]
    assert [v["variable"] for v in vs] == ["respondent", "score", "satisfaction", "status"]
    assert [int(v["var_index"]) for v in vs] == [1, 2, 3, 4]
    assert [v["label"] for v in vs] == ["", "评分", "满意度", "状态"]
    assert [v["value_type"] for v in vs] == ["string", "numeric", "numeric", "string"]
    assert [v["original_type"] for v in vs] == ["A3", "F8.2", "F8.2", "A4"]


def test_records_shape_and_row_identity(imported):
    _, t = imported
    recs = t["records"]
    assert len(recs) == 7 * 4
    assert sorted({int(r["row_id"]) for r in recs}) == [1, 2, 3, 4, 5, 6, 7]
    # 受访者编号只是文本列：7 行各有独立记录身份
    ids = [r["raw_value"] for r in recs if r["variable"] == "respondent"]
    assert ids == ["001", "002", "003", "004", "005", "006", "007"]


def test_zero_is_observed_not_missing(imported):
    _, t = imported
    c = cell(t["records"], 1, "score")
    assert c["raw_value"] == "0.0"
    assert c["value_label"] == "零分"
    assert c["missing_kind"] == OBSERVED
    assert c["analysis_value"] == "0.0"


def test_refused_code_kept_with_label_and_rule(imported):
    _, t = imported
    c = cell(t["records"], 3, "score")
    assert c["raw_value"] == "-99.0"          # 拒答原码未丢
    assert c["value_label"] == "拒答"          # 缺失码的标签仍保留
    assert c["missing_kind"] == USER
    assert c["missing_rule"] == "discrete:-99.0"
    assert c["analysis_value"] == ""


def test_missing_range_both_endpoints(imported):
    _, t = imported
    for row, raw in [(5, "90.0"), (6, "99.0")]:  # 闭区间上下端都算 user
        c = cell(t["records"], row, "score")
        assert c["raw_value"] == raw
        assert c["value_label"] == "保留编码"     # 区间缺失与普通有标签值共存
        assert c["missing_kind"] == USER
        assert c["missing_rule"] == "range:[90.0,99.0]"
        assert c["analysis_value"] == ""


def test_system_missing(imported):
    _, t = imported
    c = cell(t["records"], 4, "score")
    assert c["missing_kind"] == SYSTEM
    assert c["raw_value"] == "" and c["analysis_value"] == ""


def test_shared_label_codes_not_merged(imported):
    _, t = imported
    # 1.0 与 42.0 共用标签「已答」，value_labels 表仍是两行、records 仍各带原码
    pairs = {(r["code"], r["label"]) for r in t["value_labels"] if r["variable"] == "score"}
    assert ("1.0", "已答") in pairs and ("42.0", "已答") in pairs
    assert cell(t["records"], 2, "score")["raw_value"] == "1.0"
    assert cell(t["records"], 7, "score")["raw_value"] == "42.0"
    assert cell(t["records"], 2, "score")["missing_kind"] == OBSERVED
    assert cell(t["records"], 7, "score")["missing_kind"] == OBSERVED


def test_unlabeled_missing_code_and_unlabeled_legal_values(imported):
    _, t = imported
    c = cell(t["records"], 4, "satisfaction")   # -99 无标签但命中规则
    assert c["missing_kind"] == USER and c["value_label"] == "" and c["raw_value"] == "-99.0"
    for r in t["records"]:                       # respondent 全列无标签，仍全部 observed
        if r["variable"] == "respondent":
            assert r["missing_kind"] == OBSERVED and r["value_label"] == ""
            assert r["analysis_value"] == r["raw_value"]


def test_string_declared_missing_and_blank(imported):
    _, t = imported
    na = cell(t["records"], 2, "status")
    assert na["missing_kind"] == USER and na["raw_value"] == "NA" and na["value_label"] == "未回答"
    skip = cell(t["records"], 5, "status")
    assert skip["missing_kind"] == USER and skip["raw_value"] == "SKIP" and skip["value_label"] == "跳过"
    blank = cell(t["records"], 3, "status")      # 空白串不是系统缺失
    assert blank["missing_kind"] == OBSERVED and blank["raw_value"] == "" and blank["analysis_value"] == ""
    ok = cell(t["records"], 1, "status")
    assert ok["missing_kind"] == OBSERVED and ok["value_label"] == "正常"


def test_missing_rules_table_scoped_by_variable(imported):
    _, t = imported
    rules = t["missing_rules"]
    score_rules = [r for r in rules if r["variable"] == "score"]
    assert {(r["kind"], r["lo"], r["hi"]) for r in score_rules} == {
        ("range", "90.0", "99.0"), ("discrete", "-99.0", "-99.0")}
    assert [("discrete", "-99.0")] == [(r["kind"], r["lo"]) for r in rules if r["variable"] == "satisfaction"]
    assert {(r["lo"]) for r in rules if r["variable"] == "status"} == {"NA", "SKIP"}
    assert not [r for r in rules if r["variable"] == "respondent"]


def test_manifest_counts_match_records_table(imported):
    out, t = imported
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert m["n_rows"] == 7 and m["n_columns"] == 4
    assert m["input"]["file_encoding"] == "UTF-8" and len(m["input"]["sha256"]) == 64
    # 清单数量必须与实际读回长表一致
    recount = {v: {OBSERVED: 0, USER: 0, SYSTEM: 0} for v in m["variables"]}
    for r in t["records"]:
        recount[r["variable"]][r["missing_kind"]] += 1
    assert m["counts"] == recount
    assert m["counts"]["score"] == {OBSERVED: 3, USER: 3, SYSTEM: 1}
    assert m["counts"]["status"] == {OBSERVED: 4, USER: 3, SYSTEM: 0}
    assert m["totals"]["cells"] == 28


def test_inspect_explains_each_missing_kind(imported):
    out, _ = imported
    sysm = inspect_cell(out, 4, "score")
    assert "SYSMIS" in sysm["report"] and "系统缺失" in sysm["report"]
    rng = inspect_cell(out, 5, "score")
    assert "range:[90.0,99.0]" in rng["report"] and "命中" in rng["report"]
    blank = inspect_cell(out, 3, "status")
    assert "空白字符串" in blank["report"] and "observed" in blank["report"]
    ref = inspect_cell(out, 3, "score")
    assert "拒答" in ref["report"] and "discrete:-99.0" in ref["report"]
    with pytest.raises(SavImportError):
        inspect_cell(out, 99, "score")
    with pytest.raises(SavImportError):
        inspect_cell(out, 1, "nope")


# ---------------------------------------------------------------- 补测：同子集内增减行列

def test_subset_add_remove_rows_cols(tmp_path):
    df = survey_df().drop(columns=["status"])            # 减一列
    extra = pd.DataFrame({
        "respondent": ["003", "008"],                    # 重复受访者编号：不得折叠记录
        "score": [7.0, 0.30000000000000004],             # 7.0 无标签合法值；后者检验 binary64 不额外舍入
        "satisfaction": [2.0, 3.0],
    })
    df = pd.concat([df, extra], ignore_index=True)       # 增两行 -> 9 行
    meta = survey_meta()
    src = write_sav(tmp_path / "subset.sav", df,
                    column_labels=meta["column_labels"],
                    variable_value_labels=meta["variable_value_labels"],
                    missing_ranges=meta["missing_ranges"])
    out = tmp_path / "out"
    import_sav(src, out)
    t = load_tables(out)

    assert [v["variable"] for v in t["variables"]] == ["respondent", "score", "satisfaction"]
    assert len(t["records"]) == 9 * 3
    dup = [r for r in t["records"] if r["variable"] == "respondent" and r["raw_value"] == "003"]
    assert len(dup) == 2 and {r["row_id"] for r in dup} == {"3", "8"}   # 两行都保留
    c7 = cell(t["records"], 8, "score")
    assert c7["raw_value"] == "7.0" and c7["missing_kind"] == OBSERVED and c7["value_label"] == ""
    c64 = cell(t["records"], 9, "score")
    assert c64["raw_value"] == "0.30000000000000004" and c64["analysis_value"] == "0.30000000000000004"
    m = t["manifest"]
    assert m["n_rows"] == 9 and m["n_columns"] == 3


# ---------------------------------------------------------------- 补测：变动标签不改变缺失分类

def test_relabel_same_code_keeps_classification(tmp_path):
    labels = survey_meta()["variable_value_labels"]
    labels["score"][-99.0] = "拒绝作答"                  # 同一原码换标签
    labels["score"][0.0] = "真是零"
    src = write_sav(tmp_path / "relabel.sav", survey_df(),
                    column_labels=survey_meta()["column_labels"],
                    variable_value_labels=labels,
                    missing_ranges=survey_meta()["missing_ranges"])
    out = tmp_path / "out"
    import_sav(src, out)
    t = load_tables(out)
    c = cell(t["records"], 3, "score")
    assert c["value_label"] == "拒绝作答"                # 标签已变
    assert c["missing_kind"] == USER                     # 分类不变
    assert cell(t["records"], 1, "score")["missing_kind"] == OBSERVED
    assert cell(t["records"], 5, "score")["missing_kind"] == USER
    assert cell(t["records"], 4, "score")["missing_kind"] == SYSTEM


# ---------------------------------------------------------------- 补测：变动规则改变分类

def test_change_missing_rules_changes_classification(tmp_path):
    missing = {
        "score": [-99.0, 0.0],                           # 去掉区间、新增离散 0：零只有被声明才算缺失
        "satisfaction": [1.0],                           # 1.0 在 satisfaction 变缺失
        "status": ["NA", "SKIP"],
    }
    src = write_sav(tmp_path / "rules.sav", survey_df(),
                    column_labels=survey_meta()["column_labels"],
                    variable_value_labels=survey_meta()["variable_value_labels"],
                    missing_ranges=missing)
    out = tmp_path / "out"
    import_sav(src, out)
    t = load_tables(out)
    # 区间规则被移除：90/99 变 observed，标签仍在
    for row in (5, 6):
        c = cell(t["records"], row, "score")
        assert c["missing_kind"] == OBSERVED and c["value_label"] == "保留编码"
        assert c["analysis_value"] == c["raw_value"]
    # 0.0 被声明为缺失：变 user
    assert cell(t["records"], 1, "score")["missing_kind"] == USER
    # 同一数值 1.0 在不同列含义不同：score 仍 observed，satisfaction 变 user
    assert cell(t["records"], 1, "satisfaction")["missing_kind"] == USER
    assert cell(t["records"], 2, "score")["missing_kind"] == OBSERVED
    m = t["manifest"]
    assert m["counts"]["score"] == {OBSERVED: 4, USER: 2, SYSTEM: 1}
    # satisfaction 规则换成 1.0 后：两个 1.0 变 user；-99.0 不再命中任何规则，
    # 成为「未标注的合法值」→ observed
    assert m["counts"]["satisfaction"] == {OBSERVED: 5, USER: 2, SYSTEM: 0}
    assert cell(t["records"], 4, "satisfaction")["missing_kind"] == OBSERVED


# ---------------------------------------------------------------- 补测：非法输入整份失败

def test_undecodable_file_fails_without_output(tmp_path):
    bad = tmp_path / "bad.sav"
    bad.write_bytes(b"this is definitely not a sav file......")
    out = tmp_path / "out"
    with pytest.raises(SavImportError):
        import_sav(bad, out)
    assert not (out / "records.csv").exists()            # 不输出貌似完成的数据

    trunc = tmp_path / "trunc.sav"
    trunc.write_bytes(FIXTURE.read_bytes()[:200])
    with pytest.raises(SavImportError):
        import_sav(trunc, tmp_path / "out2")
    assert not (tmp_path / "out2" / "records.csv").exists()


# ---------------------------------------------------------------- CLI

def test_cli_import_inspect_and_failure(tmp_path):
    out = tmp_path / "cli_out"
    r = subprocess.run([sys.executable, "sav_import.py", "import", str(FIXTURE), str(out)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "7 行 × 4 列" in r.stdout
    r = subprocess.run([sys.executable, "sav_import.py", "inspect", str(out), "6", "score"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "range:[90.0,99.0]" in r.stdout and "命中" in r.stdout
    bad = tmp_path / "bad.sav"
    bad.write_bytes(b"junk junk junk")
    r = subprocess.run([sys.executable, "sav_import.py", "import", str(bad), str(tmp_path / "x")],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 1 and "失败" in r.stderr
    assert not (tmp_path / "x" / "records.csv").exists()
