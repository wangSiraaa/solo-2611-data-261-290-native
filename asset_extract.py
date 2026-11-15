#!/usr/bin/env python3
"""IFC4 资产属性提取工具(仅 IfcWall / IfcWallType,仅 IfcPropertySingleValue)。

用法:
    python3 asset_extract.py extract  <model.ifc> [--json]
    python3 asset_extract.py inspect  <model.ifc> <GlobalId> <PsetName.PropName> [--json]

语义规则(与题面一致):
  * 构件身份 = GlobalId;PropertySet / Property 记录源 STEP 编号(#id)。
  * 键 = (PropertySet.Name, Property.Name),精确区分大小写。
  * 每个键先取实例层声明,实例没有才继承类型层;实例显式空值(NominalValue=$)
    表示"显式未知",不回退类型默认。
  * 同一层同键多个声明 => ambiguous,不按文件先后选值;实例层已有唯一值时,
    类型层歧义仅记录来源,不影响有效值。
  * 一个 IfcWall 被赋予两个不同 IfcWallType => 整件隔离,不影响其他构件。
  * 仅支持 IfcPropertySingleValue,值类型仅 IfcLabel / IfcBoolean /
    IfcLengthMeasure;字符串与布尔不互转;其余属性类别标为 unsupported。
  * 长度:属性显式 Unit 优先,否则项目唯一 LENGTHUNIT;支持 METRE 与
    MILLI 前缀,输出米值并保留原值与单位来源。默认单位缺失/冲突时,
    仅使依赖它的长度属性失败,不波及显式单位属性;空值不虚构单位。
  * 只读:本工具不修改输入 IFC,也不改写被多个构件共享的源对象。
"""
from __future__ import annotations

import argparse
import json
import sys

import ifcopenshell

SUPPORTED_VALUE_TYPES = ("IfcLabel", "IfcBoolean", "IfcLengthMeasure")
# (Prefix, Name) -> 换算为米的因子;Prefix 为 None 表示无前缀
LENGTH_UNIT_FACTORS = {(None, "METRE"): 1.0, ("MILLI", "METRE"): 0.001}

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNSUPPORTED = "unsupported"
STATUS_ERROR = "error"


# ---------------------------------------------------------------- 单位

def _si_length_factor(unit_entity):
    """IfcSIUnit(LENGTHUNIT) -> (factor, label);不支持返回 (None, label)。"""
    prefix = unit_entity.Prefix  # None 或 'MILLI'
    name = unit_entity.Name      # 'METRE'
    label = f"{prefix} {name}" if prefix else name
    return LENGTH_UNIT_FACTORS.get((prefix, name)), label


def project_default_length_unit(model):
    """项目唯一 LENGTHUNIT。返回 dict:status 为 ok/missing/conflicting/unsupported。"""
    units = []
    for proj in model.by_type("IfcProject"):
        assignment = proj.UnitsInContext
        if assignment:
            units.extend(u for u in (assignment.Units or []) if u.UnitType == "LENGTHUNIT")
    if not units:
        return {"status": "missing", "detail": "项目未定义 LENGTHUNIT"}
    if len(units) > 1:
        return {"status": "conflicting",
                "detail": "项目存在多个 LENGTHUNIT: " + ",".join(f"#{u.id()}" for u in units)}
    u = units[0]
    if not u.is_a("IfcSIUnit"):
        return {"status": "unsupported", "detail": f"默认长度单位为 {u.is_a()},仅支持 IfcSIUnit"}
    factor, label = _si_length_factor(u)
    if factor is None:
        return {"status": "unsupported", "detail": f"不支持的默认长度单位 {label}"}
    return {"status": "ok", "factor": factor, "label": label, "source": f"#{u.id()}"}


def resolve_length_unit(prop_unit_entity, default_unit):
    """返回 (unit_dict | None, error_note | None)。显式单位优先于项目默认。"""
    if prop_unit_entity is not None:
        if prop_unit_entity.is_a("IfcSIUnit") and prop_unit_entity.UnitType == "LENGTHUNIT":
            factor, label = _si_length_factor(prop_unit_entity)
            if factor is not None:
                return ({"source": "explicit", "factor": factor, "label": label,
                         "step": f"#{prop_unit_entity.id()}"}, None)
            return None, f"属性显式单位 {label} 不支持(仅支持 METRE / MILLI METRE)"
        return None, f"属性显式单位 {prop_unit_entity.is_a()} 不支持(仅支持 IfcSIUnit)"
    if default_unit["status"] == "ok":
        return ({"source": "default", "factor": default_unit["factor"],
                 "label": default_unit["label"], "step": default_unit["source"]}, None)
    return None, f"属性无显式单位,且项目默认长度单位{default_unit['status']}: {default_unit['detail']}"


# ---------------------------------------------------------------- 声明提取

def _make_decl(pset, prop, layer):
    """把一条 IfcProperty 变成声明记录(保留 STEP 编号)。"""
    decl = {
        "layer": layer,
        "pset_name": pset.Name or "",
        "pset_step": pset.id(),
        "prop_name": prop.Name or "",
        "prop_step": prop.id(),
        "category": prop.is_a(),
        "status": STATUS_OK,
        "value_type": None,
        "raw_value": None,
        "unit_entity": None,   # 仅内部使用,输出前剔除
        "note": None,
    }
    if prop.is_a() != "IfcPropertySingleValue":
        decl["status"] = STATUS_UNSUPPORTED
        decl["note"] = f"属性类别 {prop.is_a()} 不在支持范围(仅 IfcPropertySingleValue)"
        return decl
    nominal = prop.NominalValue
    if nominal is None:
        decl["status"] = STATUS_EMPTY
        decl["note"] = "NominalValue 为空:显式未知"
        return decl
    vtype = nominal.is_a()
    decl["value_type"] = vtype
    if vtype not in SUPPORTED_VALUE_TYPES:
        decl["status"] = STATUS_UNSUPPORTED
        decl["note"] = f"值类型 {vtype} 不在支持范围{list(SUPPORTED_VALUE_TYPES)}"
        return decl
    decl["raw_value"] = nominal.wrappedValue
    decl["unit_entity"] = prop.Unit  # IfcPropertySingleValue.Unit,可为 None
    return decl


def _dedupe_psets(psets):
    seen, out = set(), []
    for ps in psets:
        if ps.id() not in seen:
            seen.add(ps.id())
            out.append(ps)
    return out


def _decls_from_psets(psets, layer):
    decls = []
    for ps in _dedupe_psets(psets):
        for prop in (ps.HasProperties or ()):
            decls.append(_make_decl(ps, prop, layer))
    return decls


# ---------------------------------------------------------------- 有效值解析

def _decl_public(d):
    """声明的对外视图(原始属性来源用)。"""
    out = {k: d[k] for k in ("layer", "pset_name", "pset_step", "prop_name",
                             "prop_step", "category", "status", "value_type",
                             "raw_value", "note")}
    return out


def _resolve_key(inst_decls, type_decls, default_unit):
    """解析单个键的有效属性行。"""
    if inst_decls:
        winners, layer, overridden = inst_decls, "instance", type_decls
    else:
        winners, layer, overridden = type_decls, "type", []
    first = winners[0]
    row = {
        "key": {"pset": first["pset_name"], "prop": first["prop_name"]},
        "status": STATUS_OK,
        "layer": layer,
        "value": None,
        "raw_value": None,
        "value_type": None,
        "unit": None,
        "sources": [{"pset_step": d["pset_step"], "prop_step": d["prop_step"]} for d in winners],
        "candidates": [
            {"pset_step": d["pset_step"], "prop_step": d["prop_step"],
             "status": d["status"], "value_type": d["value_type"], "raw_value": d["raw_value"]}
            for d in winners
        ],
        "overridden": [
            {"layer": d["layer"], "pset_step": d["pset_step"], "prop_step": d["prop_step"],
             "status": d["status"], "value_type": d["value_type"], "raw_value": d["raw_value"]}
            for d in overridden
        ],
        "notes": [],
    }
    if len(overridden) > 1:
        row["notes"].append(
            f"类型层同键存在 {len(overridden)} 个声明(歧义来源记录);实例层已取值,不参与决定有效值")
    if len(winners) > 1:
        row["status"] = STATUS_AMBIGUOUS
        row["notes"].append(
            f"{layer} 层同键 {len(winners)} 个声明,歧义,不按文件先后选值")
        return row
    decl = winners[0]
    row["value_type"] = decl["value_type"]
    row["raw_value"] = decl["raw_value"]
    if decl["status"] != STATUS_OK:
        row["status"] = decl["status"]
        if decl["note"]:
            row["notes"].append(decl["note"])
        if decl["status"] == STATUS_EMPTY and overridden:
            row["notes"].append("实例显式空值,不回退类型默认值")
        return row
    if decl["value_type"] == "IfcLengthMeasure":
        unit, err = resolve_length_unit(decl["unit_entity"], default_unit)
        if err:
            row["status"] = STATUS_ERROR
            row["notes"].append(err)
            return row
        row["unit"] = unit
        row["value"] = decl["raw_value"] * unit["factor"]
    else:
        # IfcLabel / IfcBoolean 原样保留,互不转换
        row["value"] = decl["raw_value"]
    return row


# ---------------------------------------------------------------- 主提取

def extract_model(path):
    """读取 IFC(只读),返回构件表、有效属性表、原始属性来源与诊断。"""
    model = ifcopenshell.open(path)
    default_unit = project_default_length_unit(model)

    walls = {}
    for w in model.by_type("IfcWall"):
        walls[w.id()] = w

    # IfcRelDefinesByType:构件 -> 不同的 IfcWallType 集合
    elem_types = {wid: {} for wid in walls}      # wid -> {type_step: type_entity}
    type_rel_steps = {wid: [] for wid in walls}  # wid -> [rel_step]
    for rel in model.by_type("IfcRelDefinesByType"):
        rtype = rel.RelatingType
        if rtype is None or not rtype.is_a("IfcWallType"):
            continue
        for obj in (rel.RelatedObjects or ()):
            if obj.id() in walls:
                elem_types[obj.id()][rtype.id()] = rtype
                type_rel_steps[obj.id()].append(rel.id())

    # IfcRelDefinesByProperties:构件 -> [IfcPropertySet]
    elem_psets = {wid: [] for wid in walls}
    skipped_definitions = []
    for rel in model.by_type("IfcRelDefinesByProperties"):
        pdef = rel.RelatingPropertyDefinition
        targets = [o for o in (rel.RelatedObjects or ()) if o.id() in walls]
        if not targets:
            continue
        if pdef is not None and pdef.is_a("IfcPropertySet"):
            for o in targets:
                elem_psets[o.id()].append(pdef)
        else:
            skipped_definitions.append({
                "rel_step": rel.id(),
                "definition": None if pdef is None else f"{pdef.is_a()} #{pdef.id()}",
                "note": "非 IfcPropertySet 定义(如数量集)不在范围内,已跳过",
            })

    elements, properties, raw_sources, quarantine = [], [], [], []
    counts = {STATUS_OK: 0, STATUS_EMPTY: 0, STATUS_AMBIGUOUS: 0,
              STATUS_UNSUPPORTED: 0, STATUS_ERROR: 0}

    for wid in sorted(walls, key=lambda i: walls[i].GlobalId):
        wall = walls[wid]
        types = sorted(elem_types[wid].values(), key=lambda t: t.id())
        elem_rec = {
            "global_id": wall.GlobalId,
            "name": wall.Name,
            "step": wid,
            "status": "ok",
            "types": [{"global_id": t.GlobalId, "name": t.Name, "step": t.id()} for t in types],
            "type_rels": type_rel_steps[wid],
            "reason": None,
        }
        if len(types) > 1:
            elem_rec["status"] = "quarantined"
            elem_rec["reason"] = (
                f"构件被赋予 {len(types)} 个不同类型: "
                + ", ".join(f"{t.Name or ''}({t.GlobalId}, #{t.id()})" for t in types)
                + ";整件隔离,不提取属性")
            elements.append(elem_rec)
            quarantine.append({"global_id": wall.GlobalId, "name": wall.Name,
                               "reason": elem_rec["reason"]})
            continue
        elements.append(elem_rec)

        inst_decls = _decls_from_psets(elem_psets[wid], "instance")
        type_decls = []
        if len(types) == 1:
            type_decls = _decls_from_psets(list(types[0].HasPropertySets or ()), "type")
        for d in inst_decls + type_decls:
            src = _decl_public(d)
            src["element"] = wall.GlobalId
            raw_sources.append(src)

        # 键 -> 各层声明(保持精确大小写)
        inst_by_key, type_by_key = {}, {}
        for d in inst_decls:
            inst_by_key.setdefault((d["pset_name"], d["prop_name"]), []).append(d)
        for d in type_decls:
            type_by_key.setdefault((d["pset_name"], d["prop_name"]), []).append(d)

        for key in sorted(set(inst_by_key) | set(type_by_key)):
            row = _resolve_key(inst_by_key.get(key, []), type_by_key.get(key, []), default_unit)
            row["element"] = wall.GlobalId
            row["element_name"] = wall.Name
            counts[row["status"]] += 1
            properties.append(row)

    properties.sort(key=lambda r: (r["element"], r["key"]["pset"], r["key"]["prop"]))

    # 共享属性集提示:同一 pset STEP 编号被多个构件引用,不表示同一资产
    pset_users = {}
    for s in raw_sources:
        pset_users.setdefault(s["pset_step"], set()).add(s["element"])
    shared_psets = sorted(pid for pid, users in pset_users.items() if len(users) > 1)

    diagnostics = {
        "elements_total": len(elements),
        "elements_ok": sum(1 for e in elements if e["status"] == "ok"),
        "elements_quarantined": len(quarantine),
        "quarantine": quarantine,
        "property_status_counts": counts,
        "property_rows": sum(counts.values()),
        "default_length_unit": default_unit,
        "skipped_definitions": skipped_definitions,
        "shared_psets": [
            {"pset_step": pid,
             "note": "该属性集被多个构件共享;共享属性集不表示同一资产,源对象未被改写"}
            for pid in shared_psets
        ],
        "notes": [
            "范围: IfcWall/IfcWallType, IfcPropertySingleValue(IfcLabel/IfcBoolean/IfcLengthMeasure)",
            "空值/歧义/不支持/错误的行已保留并可追溯;未解析为有效值的部分不构成完整资产清单",
        ],
    }
    return {
        "file": str(path),
        "schema": model.schema,
        "elements": elements,
        "properties": properties,
        "raw_sources": raw_sources,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------- inspect

def inspect_key(path, global_id, pset_name, prop_name):
    """解释某构件某键为何取该值。"""
    result = extract_model(path)
    elem = next((e for e in result["elements"] if e["global_id"] == global_id), None)
    if elem is None:
        known = ", ".join(e["global_id"] for e in result["elements"])
        raise SystemExit(f"未找到构件 GlobalId={global_id};已知构件: {known}")
    report = {"element": elem, "key": {"pset": pset_name, "prop": prop_name}}
    if elem["status"] == "quarantined":
        report["verdict"] = f"构件已隔离: {elem['reason']}"
        return report
    decls = [s for s in result["raw_sources"]
             if s["element"] == global_id and s["pset_name"] == pset_name
             and s["prop_name"] == prop_name]
    report["instance_declarations"] = [d for d in decls if d["layer"] == "instance"]
    report["type_declarations"] = [d for d in decls if d["layer"] == "type"]
    row = next((p for p in result["properties"]
                if p["element"] == global_id and p["key"]["pset"] == pset_name
                and p["key"]["prop"] == prop_name), None)
    if row is None:
        report["verdict"] = "该键在此构件上不存在(实例层与类型层均无声明)"
        report["available_keys"] = sorted(
            {f"{p['key']['pset']}.{p['key']['prop']}" for p in result["properties"]
             if p["element"] == global_id})
        return report
    report["effective"] = row
    report["verdict"] = _explain(row)
    return report


def _explain(row):
    key = f"{row['key']['pset']}.{row['key']['prop']}"
    src = ",".join(f"pset#{s['pset_step']}/prop#{s['prop_step']}" for s in row["sources"])
    if row["status"] == STATUS_AMBIGUOUS:
        layer_cn = "实例层" if row["layer"] == "instance" else "类型层"
        cand = "; ".join(f"prop#{c['prop_step']}={c['raw_value']!r}({c['status']})"
                         for c in row["candidates"])
        return (f"键 {key}: {layer_cn}存在 {len(row['candidates'])} 个声明({cand}),"
                f"歧义,不按文件先后选值;来源 {src}")
    layer_cn = "实例层" if row["layer"] == "instance" else "类型层"
    if row["status"] == STATUS_EMPTY:
        return (f"键 {key}: {layer_cn}唯一声明 NominalValue 为空 => 显式未知,"
                f"不回退类型默认;来源 {src}")
    if row["status"] == STATUS_UNSUPPORTED:
        return f"键 {key}: {layer_cn}唯一声明不受支持: {'; '.join(row['notes'])};来源 {src}"
    if row["status"] == STATUS_ERROR:
        return f"键 {key}: {layer_cn}声明解析失败: {'; '.join(row['notes'])};来源 {src}"
    parts = [f"键 {key}: {layer_cn}唯一声明 => 有效值 {row['value']!r} ({row['value_type']})"]
    if row["unit"]:
        u = row["unit"]
        src_cn = "属性显式单位" if u["source"] == "explicit" else "项目默认单位"
        parts.append(f"单位 {u['label']}({src_cn} {u['step']}),原值 {row['raw_value']} -> {row['value']} m")
    if row["overridden"]:
        ov = "; ".join(f"{o['layer']} pset#{o['pset_step']}/prop#{o['prop_step']}={o['raw_value']!r}"
                       for o in row["overridden"])
        parts.append(f"被覆盖声明(仅记录): {ov}")
    parts.append(f"来源 {src}")
    return "; ".join(parts)


# ---------------------------------------------------------------- 文本输出

def _fmt_value(row):
    if row["status"] == STATUS_OK:
        if row["value_type"] == "IfcLengthMeasure":
            return f"{row['value']:g} m"
        if row["value_type"] == "IfcBoolean":
            return "true" if row["value"] else "false"
        return repr(row["value"])
    return {"empty": "(显式空值)", "ambiguous": "(歧义)", "unsupported": "(不支持)",
            "error": "(错误)"}.get(row["status"], "()")


def print_extract(result):
    diag = result["diagnostics"]
    du = diag["default_length_unit"]
    du_txt = (f"{du['label']} (唯一, 来源 {du['source']})" if du["status"] == "ok"
              else f"{du['status']}: {du['detail']}")
    print(f"文件: {result['file']}  schema: {result['schema']}")
    print(f"项目默认长度单位: {du_txt}")
    print()
    print(f"== 构件表 ({len(result['elements'])}) ==")
    for e in result["elements"]:
        types = ", ".join(f"{t['name'] or ''}({t['global_id']},#{t['step']})" for t in e["types"]) or "-"
        line = f"  {e['global_id']}  {e['name'] or ''!r:6}  [{e['status']}]  类型: {types}"
        if e["reason"]:
            line += f"\n      隔离原因: {e['reason']}"
        print(line)
    print()
    print(f"== 有效属性表 ({len(result['properties'])} 行) ==")
    for p in result["properties"]:
        key = f"{p['key']['pset']}.{p['key']['prop']}"
        src = ",".join(f"pset#{s['pset_step']}/prop#{s['prop_step']}" for s in p["sources"])
        ov = ""
        if p["overridden"]:
            ov = "  被覆盖: " + ";".join(
                f"{o['layer']} pset#{o['pset_step']}/prop#{o['prop_step']}" for o in p["overridden"])
        unit = ""
        if p["unit"]:
            u = p["unit"]
            unit = f"  单位: {u['label']}({u['source']},原值 {p['raw_value']:g})"
        notes = f"  注: {'; '.join(p['notes'])}" if p["notes"] else ""
        print(f"  {p['element']} ({p['element_name']})  {key}")
        print(f"      [{p['status']}] {p['layer']}  值: {_fmt_value(p)}  来源: {src}{ov}{unit}{notes}")
    print()
    print(f"== 原始属性来源 ({len(result['raw_sources'])} 条声明) ==")
    for s in result["raw_sources"]:
        val = "" if s["raw_value"] is None else f" = {s['raw_value']!r}"
        note = f"  注: {s['note']}" if s["note"] else ""
        print(f"  {s['element']}  [{s['layer']}] pset#{s['pset_step']} '{s['pset_name']}'"
              f" / prop#{s['prop_step']} '{s['prop_name']}' ({s['category']}, {s['status']}){val}{note}")
    print()
    print("== 诊断 ==")
    print(f"  构件: 共 {diag['elements_total']},正常 {diag['elements_ok']},隔离 {diag['elements_quarantined']}")
    for q in diag["quarantine"]:
        print(f"    隔离: {q['global_id']} ({q['name']}): {q['reason']}")
    c = diag["property_status_counts"]
    print(f"  属性行: 共 {diag['property_rows']};正常 {c['ok']},空值 {c['empty']},"
          f"歧义 {c['ambiguous']},不支持 {c['unsupported']},错误 {c['error']}")
    for sd in diag["skipped_definitions"]:
        print(f"  跳过定义: rel#{sd['rel_step']} {sd['definition']} — {sd['note']}")
    for sp in diag["shared_psets"]:
        print(f"  共享属性集: pset#{sp['pset_step']} — {sp['note']}")
    for n in diag["notes"]:
        print(f"  说明: {n}")


def print_inspect(report):
    e = report["element"]
    print(f"构件: {e['global_id']} (Name={e['name']!r}, #{e['step']})  状态: {e['status']}")
    for t in e["types"]:
        print(f"类型: {t['name'] or ''!r} ({t['global_id']}, #{t['step']})")
    print(f"键: {report['key']['pset']}.{report['key']['prop']}")
    for layer, decls in (("实例层", report.get("instance_declarations")),
                         ("类型层", report.get("type_declarations"))):
        if decls is None:
            continue
        print(f"{layer}声明 ({len(decls)}):")
        for d in decls:
            val = "" if d["raw_value"] is None else f" = {d['raw_value']!r}"
            note = f"  注: {d['note']}" if d["note"] else ""
            print(f"  pset#{d['pset_step']} '{d['pset_name']}' / prop#{d['prop_step']}"
                  f" ({d['category']}, {d['status']}){val}{note}")
    print(f"结论: {report['verdict']}")
    if report.get("available_keys"):
        print("可用键: " + ", ".join(report["available_keys"]))


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(prog="asset_extract",
                                     description="IFC4 资产属性提取(IfcWall/IfcWallType,只读)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("extract", help="输出构件表、有效属性表、原始属性来源及诊断")
    pe.add_argument("ifc")
    pe.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    pi = sub.add_parser("inspect", help="解释某构件某键为何取该值")
    pi.add_argument("ifc")
    pi.add_argument("global_id")
    pi.add_argument("key", help="格式 PsetName.PropertyName(精确区分大小写)")
    pi.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "extract":
        result = extract_model(args.ifc)
        if args.json:
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
            print()
        else:
            print_extract(result)
        return 0
    # inspect
    if "." not in args.key:
        raise SystemExit("键格式应为 PsetName.PropertyName")
    pset_name, prop_name = args.key.split(".", 1)
    report = inspect_key(args.ifc, args.global_id, pset_name, prop_name)
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2, default=str)
        print()
    else:
        print_inspect(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
