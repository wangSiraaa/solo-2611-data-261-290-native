#!/usr/bin/env python3
"""asset_extract 的验收与补测。

验收样例(给定 fixtures):共享类型、局部覆盖、显式空值、毫米单位、同名冲突、坏双类型。
补测:<=20 构件、同子集增减属性、关系/STEP 行重排(语义不变,来源编号可不同)、
     默认单位缺失/冲突、不支持的类别与值类型、字符串布尔不互转、只读、共享属性集。
"""
import hashlib
import json
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import asset_extract as ax

FIX = Path(__file__).parent / "fixtures"
ASSETS = FIX / "assets.ifc"
BAD = FIX / "bad-two-types.ifc"

GID = {"A": "3PNvuNJ8f0WB1iDUNRLONq", "B": "1q0Bl1QMXBMwzBHYgp$6c2",
       "C": "2zpULDWln4IA5QsGO1VejZ", "D": "0ZIgC3mbnFih_9W56NVsr4"}


@pytest.fixture(scope="module")
def assets():
    return ax.extract_model(ASSETS)


def props_of(result, gid):
    return {f"{p['key']['pset']}.{p['key']['prop']}": p
            for p in result["properties"] if p["element"] == gid}


# ------------------------------------------------------------- 验收样例

def test_shared_type_full_inheritance(assets):
    """共享类型:D 无实例属性集,三个键全部继承类型层。"""
    props = props_of(assets, GID["D"])
    assert set(props) == {"Asset.Material", "Asset.Height", "Asset.Enabled"}
    assert all(p["layer"] == "type" and p["status"] == "ok" for p in props.values())
    assert props["Asset.Material"]["value"] == "brick"
    assert props["Asset.Height"]["value"] == 3.0
    assert props["Asset.Enabled"]["value"] is True
    assert all(p["sources"] == [{"pset_step": 8, "prop_step": s}]
               for p, s in ((props["Asset.Material"], 5), (props["Asset.Height"], 6),
                            (props["Asset.Enabled"], 7)))


def test_local_override(assets):
    """局部覆盖:A 的 Material/Height 取实例层,Enabled 仍继承;类型值记录为被覆盖。"""
    props = props_of(assets, GID["A"])
    m, h, e = props["Asset.Material"], props["Asset.Height"], props["Asset.Enabled"]
    assert (m["status"], m["layer"], m["value"]) == ("ok", "instance", "wood")
    assert m["value_type"] == "IfcLabel"
    assert [o["prop_step"] for o in m["overridden"]] == [5]  # 类型 brick 被覆盖
    assert (h["status"], h["layer"], h["value"]) == ("ok", "instance", 2.5)
    assert (e["layer"], e["value"]) == ("type", True)


def test_explicit_empty_no_fallback(assets):
    """显式空值:B 的 Material 实例声明为空 => 显式未知,不回退类型默认 brick。"""
    m = props_of(assets, GID["B"])["Asset.Material"]
    assert m["status"] == "empty" and m["layer"] == "instance" and m["value"] is None
    assert m["sources"] == [{"pset_step": 19, "prop_step": 18}]
    assert [o["raw_value"] for o in m["overridden"]] == ["brick"]


def test_millimetre_explicit_unit(assets):
    """毫米单位:A 的 Height 显式 MILLI METRE,2500mm -> 2.5m,保留原值与单位来源。"""
    h = props_of(assets, GID["A"])["Asset.Height"]
    assert h["raw_value"] == 2500.0 and h["value"] == 2.5
    assert h["unit"] == {"source": "explicit", "factor": 0.001,
                         "label": "MILLI METRE", "step": "#2"}


def test_default_unit_for_plain_length(assets):
    """类型 Height 无显式单位 => 用项目唯一 LENGTHUNIT(METRE)。"""
    h = props_of(assets, GID["D"])["Asset.Height"]
    assert h["unit"]["source"] == "default" and h["unit"]["label"] == "METRE"
    assert h["value"] == 3.0


def test_same_name_conflict_ambiguous(assets):
    """同名冲突:C 的 Material 同层两个声明 => 歧义,不按文件先后选值。"""
    m = props_of(assets, GID["C"])["Asset.Material"]
    assert m["status"] == "ambiguous" and m["value"] is None
    assert sorted(c["raw_value"] for c in m["candidates"]) == ["x", "y"]
    assert {c["prop_step"] for c in m["candidates"]} == {20, 21}
    # 其余键不受歧义影响
    assert props_of(assets, GID["C"])["Asset.Height"]["value"] == 3.0


def test_bad_two_types_quarantined():
    """坏双类型:D 整件隔离,其他构件与正常文件完全一致。"""
    bad = ax.extract_model(BAD)
    good = ax.extract_model(ASSETS)
    d = next(e for e in bad["elements"] if e["global_id"] == GID["D"])
    assert d["status"] == "quarantined" and len(d["types"]) == 2
    assert not [p for p in bad["properties"] if p["element"] == GID["D"]]
    assert bad["diagnostics"]["elements_quarantined"] == 1
    for gid in (GID["A"], GID["B"], GID["C"]):
        assert props_of(bad, gid) == props_of(good, gid)


def test_diagnostics_counts(assets):
    """清单报告正常/空值/歧义/不支持数量,且不自称完整。"""
    c = assets["diagnostics"]["property_status_counts"]
    assert c == {"ok": 10, "empty": 1, "ambiguous": 1, "unsupported": 0, "error": 0}
    assert assets["diagnostics"]["elements_total"] == 4
    assert assets["diagnostics"]["shared_psets"][0]["pset_step"] == 8


def test_string_bool_not_interconverted(assets):
    """字符串与布尔不互转:类型系统保留各自值类型。"""
    assert props_of(assets, GID["D"])["Asset.Enabled"]["value_type"] == "IfcBoolean"
    assert props_of(assets, GID["D"])["Asset.Material"]["value_type"] == "IfcLabel"


# ------------------------------------------------------------- 补测:变体生成

def _variant(tmp_path, transform, name="variant.ifc"):
    text = ASSETS.read_text()
    out = tmp_path / name
    out.write_text(transform(text))
    return out


def test_reorder_step_lines_keeps_semantics(tmp_path):
    """重排 STEP 行(含关系)不改变语义;来源编号不变时结果应完全相等。"""
    def shuffle(text):
        head, rest = text.split("DATA;\n", 1)
        body, tail = rest.split("ENDSEC;\nEND-ISO-10303-21;", 1)
        lines = [ln for ln in body.strip().splitlines() if ln.strip()]
        random.Random(42).shuffle(lines)
        return (head + "DATA;\n" + "\n".join(lines) +
                "\nENDSEC;\nEND-ISO-10303-21;" + tail)
    reordered = ax.extract_model(_variant(tmp_path, shuffle))
    base = ax.extract_model(ASSETS)
    assert reordered["elements"] == base["elements"]
    assert reordered["properties"] == base["properties"]
    assert reordered["raw_sources"] == base["raw_sources"]
    assert reordered["diagnostics"]["property_status_counts"] == \
        base["diagnostics"]["property_status_counts"]


def test_relationship_reorder_only(tmp_path):
    """仅重排关系实体(IfcRelDefinesBy*)所在行,语义不变。"""
    def move_rels_to_end(text):
        lines = text.splitlines()
        rels = [ln for ln in lines if "IFCRELDEFINES" in ln]
        rest = [ln for ln in lines if "IFCRELDEFINES" not in ln]
        idx = rest.index("ENDSEC;")
        return "\n".join(rest[:idx] + rels + rest[idx:]) + "\n"
    reordered = ax.extract_model(_variant(tmp_path, move_rels_to_end))
    assert reordered["properties"] == ax.extract_model(ASSETS)["properties"]


def test_add_property_within_subset(tmp_path):
    """同子集增属性:给 A 的实例属性集加 IfcLabel 属性。"""
    def add(text):
        text = text.replace(
            "#17=IFCPROPERTYSET('0hcFTxSyHEYgO2e0FlBycd',$,'Asset',$,(#15,#16));",
            "#17=IFCPROPERTYSET('0hcFTxSyHEYgO2e0FlBycd',$,'Asset',$,(#15,#16,#28));\n"
            "#28=IFCPROPERTYSINGLEVALUE('Grade',$,IFCLABEL('A1'),$);")
        return text
    res = ax.extract_model(_variant(tmp_path, add))
    g = props_of(res, GID["A"])["Asset.Grade"]
    assert (g["status"], g["layer"], g["value"], g["value_type"]) == \
        ("ok", "instance", "A1", "IfcLabel")
    assert res["diagnostics"]["property_status_counts"]["ok"] == 11


def test_remove_property_within_subset(tmp_path):
    """同子集减属性:从类型属性集移除 Enabled,所有构件该键消失。"""
    def remove(text):
        return text.replace("(IFCPROPERTYSINGLEVALUE('Enabled',$,IFCBOOLEAN(.T.),$))",
                            "(IFCPROPERTYSINGLEVALUE('Enabled',$,IFCBOOLEAN(.T.),$))") \
                   .replace(",#7)", ")").replace("#7=IFCPROPERTYSINGLEVALUE('Enabled',$,IFCBOOLEAN(.T.),$);\n", "")
    res = ax.extract_model(_variant(tmp_path, remove))
    assert all("Asset.Enabled" not in props_of(res, g) for g in GID.values())
    assert res["diagnostics"]["property_status_counts"]["ok"] == 6


def test_missing_default_unit(tmp_path):
    """默认单位缺失:依赖默认单位的长度失败,显式单位与空值不受波及。"""
    def drop(text):
        return text.replace("#3=IFCUNITASSIGNMENT((#1));", "#3=IFCUNITASSIGNMENT(());")
    res = ax.extract_model(_variant(tmp_path, drop))
    assert res["diagnostics"]["default_length_unit"]["status"] == "missing"
    a_h = props_of(res, GID["A"])["Asset.Height"]
    assert a_h["status"] == "ok" and a_h["value"] == 2.5          # 显式单位不受影响
    d_h = props_of(res, GID["D"])["Asset.Height"]
    assert d_h["status"] == "error" and d_h["value"] is None      # 需要默认单位 => 失败
    assert props_of(res, GID["D"])["Asset.Material"]["status"] == "ok"  # 非长度不受影响
    assert props_of(res, GID["B"])["Asset.Material"]["status"] == "empty"  # 空值无需单位


def test_conflicting_default_unit(tmp_path):
    """默认单位冲突:两个 LENGTHUNIT => 依赖默认单位的长度失败,显式单位正常。"""
    def conflict(text):
        return text.replace("#3=IFCUNITASSIGNMENT((#1));", "#3=IFCUNITASSIGNMENT((#1,#2));")
    res = ax.extract_model(_variant(tmp_path, conflict))
    assert res["diagnostics"]["default_length_unit"]["status"] == "conflicting"
    assert props_of(res, GID["A"])["Asset.Height"]["status"] == "ok"
    assert props_of(res, GID["D"])["Asset.Height"]["status"] == "error"


def test_unsupported_category_and_value_type(tmp_path):
    """其他属性类别/值类型明确标为 unsupported,保留可追溯。"""
    def add(text):
        return text.replace(
            "#17=IFCPROPERTYSET('0hcFTxSyHEYgO2e0FlBycd',$,'Asset',$,(#15,#16));",
            "#17=IFCPROPERTYSET('0hcFTxSyHEYgO2e0FlBycd',$,'Asset',$,(#15,#16,#28,#29));\n"
            "#28=IFCPROPERTYLISTVALUE('Tags',$, (IFCLABEL('a'),IFCLABEL('b')), $);\n"
            "#29=IFCPROPERTYSINGLEVALUE('Note',$,IFCTEXT('hello'),$);")
    res = ax.extract_model(_variant(tmp_path, add))
    props = props_of(res, GID["A"])
    assert props["Asset.Tags"]["status"] == "unsupported"
    assert "IfcPropertyListValue" in props["Asset.Tags"]["notes"][0]
    assert props["Asset.Note"]["status"] == "unsupported"
    assert "IfcText" in props["Asset.Note"]["notes"][0]
    assert res["diagnostics"]["property_status_counts"]["unsupported"] == 2
    # 不支持的行仍带源 STEP 编号
    assert props["Asset.Tags"]["sources"][0]["prop_step"] == 28


def test_shared_instance_pset_not_same_asset(tmp_path):
    """多构件共享同一实例属性集:各自独立成行,不表示同一资产。"""
    def share(text):
        return text.replace("(#11),#19)", "(#11),#17)")  # B 也指向 A 的 pset#17
    res = ax.extract_model(_variant(tmp_path, share))
    a, b = props_of(res, GID["A"]), props_of(res, GID["B"])
    assert a["Asset.Material"]["value"] == b["Asset.Material"]["value"] == "wood"
    assert a["Asset.Material"]["element"] != b["Asset.Material"]["element"]
    assert any(s["pset_step"] == 17 for s in res["diagnostics"]["shared_psets"])


def test_type_layer_ambiguity_recorded_but_instance_wins(tmp_path):
    """类型层同键歧义:实例唯一值决定有效值,类型歧义仅记录来源。"""
    def dup(text):
        return text.replace(
            "#8=IFCPROPERTYSET('1S3B2DU0H6vBGDwofn8mly',$,'Asset',$,(#5,#6,#7));",
            "#8=IFCPROPERTYSET('1S3B2DU0H6vBGDwofn8mly',$,'Asset',$,(#5,#6,#7,#28));\n"
            "#28=IFCPROPERTYSINGLEVALUE('Material',$,IFCLABEL('steel'),$);")
    res = ax.extract_model(_variant(tmp_path, dup))
    a_m = props_of(res, GID["A"])["Asset.Material"]
    assert a_m["status"] == "ok" and a_m["value"] == "wood"      # 实例唯一值胜出
    assert len(a_m["overridden"]) == 2                            # 类型歧义来源已记录
    assert any("歧义" in n for n in a_m["notes"])
    d_m = props_of(res, GID["D"])["Asset.Material"]              # D 无实例 => 类型层歧义
    assert d_m["status"] == "ambiguous"


def test_twenty_elements_scale(tmp_path):
    """补测规模上限:20 个构件共享类型,全部正确继承。"""
    gids = [f"ex{i:020d}"[:22].ljust(22, '0') for i in range(16)]
    extra = "\n".join(
        f"#{100+i}=IFCWALL('{gids[i]}',$,'W{i}',$,$,$,$,.STANDARD.);" for i in range(16))
    text = ASSETS.read_text().replace(
        "#14=IFCRELDEFINESBYTYPE('0Ffhz3W1T6yxKDUK6m79sN',$,$,$,(#10,#11,#12,#13),#9);",
        "#14=IFCRELDEFINESBYTYPE('0Ffhz3W1T6yxKDUK6m79sN',$,$,$,(#10,#11,#12,#13,"
        + ",".join(f"#{100+i}" for i in range(16)) + "),#9);")
    text = text.replace("ENDSEC;", extra + "\nENDSEC;")
    out = tmp_path / "twenty.ifc"
    out.write_text(text)
    res = ax.extract_model(out)
    assert res["diagnostics"]["elements_total"] == 20
    assert res["diagnostics"]["elements_ok"] == 20
    w0 = props_of(res, gids[0])
    assert w0["Asset.Material"]["value"] == "brick" and w0["Asset.Height"]["value"] == 3.0


def test_ifc_file_not_modified(tmp_path):
    """原 IFC 保持只读:extract 与 inspect 前后哈希不变。"""
    before = hashlib.sha256(ASSETS.read_bytes()).hexdigest()
    ax.extract_model(ASSETS)
    ax.inspect_key(ASSETS, GID["A"], "Asset", "Material")
    assert hashlib.sha256(ASSETS.read_bytes()).hexdigest() == before


def test_inspect_explains_value():
    """inspect 给出取值理由;未知键给出可用键列表。"""
    rep = ax.inspect_key(ASSETS, GID["A"], "Asset", "Height")
    assert "实例层" in rep["verdict"] and "2.5" in rep["verdict"]
    assert rep["effective"]["unit"]["label"] == "MILLI METRE"
    rep = ax.inspect_key(ASSETS, GID["B"], "Asset", "Material")
    assert "显式未知" in rep["verdict"] and "不回退" in rep["verdict"]
    rep = ax.inspect_key(ASSETS, GID["A"], "Asset", "NoSuchKey")
    assert "available_keys" in rep
    rep = ax.inspect_key(BAD, GID["D"], "Asset", "Material")
    assert "隔离" in rep["verdict"]


def test_cli_smoke():
    """CLI:extract --json 可解析;inspect 正常退出。"""
    out = subprocess.run([sys.executable, "asset_extract.py", "extract",
                          str(ASSETS), "--json"], capture_output=True, text=True)
    assert out.returncode == 0
    data = json.loads(out.stdout)
    assert data["schema"] == "IFC4" and len(data["elements"]) == 4
    out = subprocess.run([sys.executable, "asset_extract.py", "inspect",
                          str(ASSETS), GID["C"], "Asset.Material"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "歧义" in out.stdout
