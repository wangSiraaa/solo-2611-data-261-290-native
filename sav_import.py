"""SPSS SAV 保真接入：导出 records / variables / value_labels / missing_rules 四张独立可读的表，
并提供按源行号 + 变量名的单元格溯源（inspect）。

设计要点
--------
- 用 pyreadstat(user_missing=True) 读取：系统缺失读回为 NaN，用户缺失保留原始编码；
  缺失分类完全由本模块依据 missing_ranges 元数据判定，不让读取库默认把缺失一律变 NaN。
- 数值按 binary64 原样读回，写出时用 repr() 的最短往返表示，不做任何额外舍入，
  也不恢复写入前不存在的十进制精度。
- 值标签与缺失规则都以变量名为作用域，分别存表；标签只用于展示，不参与缺失分类，
  多个原码共用一个标签时各自保留，不据标签合并类别。
- 字符串没有系统缺失：空白串按 SAV 定长空格填充的存储语义读回，只要不是文件显式
  声明的离散用户缺失，就记为 observed；不做 strip 后判空。
- 任何解码失败或超出支持范围（仅数值/字符串；数值缺失为 ≤3 个离散值或一个闭区间
  加一个区间外离散值；字符串仅离散缺失）都整份失败，不产出任何表。

CLI
---
    python sav_import.py import  input.sav  out_dir
    python sav_import.py inspect out_dir ROW VARIABLE
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import pyreadstat

OBSERVED = "observed"
USER = "user"
SYSTEM = "system"

_NUMERIC_TYPES = {"double", "float", "int8", "int16", "int32"}
_STRING_TYPE = "string"

RECORDS_FIELDS = [
    "row_id", "var_index", "variable", "value_type",
    "raw_value", "value_label", "missing_kind", "missing_rule", "analysis_value",
]
VARIABLES_FIELDS = ["var_index", "variable", "label", "value_type", "original_type", "storage_width"]
VALUE_LABELS_FIELDS = ["variable", "code", "label"]
MISSING_RULES_FIELDS = ["variable", "rule_index", "kind", "lo", "hi"]

TABLE_FILES = {
    "records": "records.csv",
    "variables": "variables.csv",
    "value_labels": "value_labels.csv",
    "missing_rules": "missing_rules.csv",
}


class SavImportError(Exception):
    """输入无法解码或超出支持范围：整份导入失败，不产出任何表。"""


def fmt_num(x: float) -> str:
    """binary64 的最短往返文本表示（repr），不额外舍入。"""
    return repr(float(x))


@dataclass(frozen=True)
class MissingRule:
    """一条用户缺失规则：discrete（lo == hi）或闭区间 range [lo, hi]。"""

    kind: str          # 'discrete' | 'range'
    lo: object         # float（数值变量）或 str（字符串变量）
    hi: object         # discrete 时等于 lo

    def matches(self, value) -> bool:
        if self.kind == "discrete":
            return value == self.lo
        return self.lo <= value <= self.hi

    def text(self, value_type: str) -> str:
        if self.kind == "discrete":
            if value_type == "string":
                return f"discrete:{self.lo!r}"
            return f"discrete:{fmt_num(self.lo)}"
        return f"range:[{fmt_num(self.lo)},{fmt_num(self.hi)}]"


# ---------------------------------------------------------------- 读取与校验

def _read_sav(src: Path):
    try:
        return pyreadstat.read_sav(str(src), user_missing=True)
    except Exception as exc:  # ReadstatError 等：整份失败
        raise SavImportError(f"无法解码 {src}: {exc}") from exc


def _value_type(meta, name: str) -> str:
    rt = meta.readstat_variable_types.get(name)
    if rt in _NUMERIC_TYPES:
        return "numeric"
    if rt == _STRING_TYPE:
        return "string"
    raise SavImportError(f"变量 {name!r} 类型 {rt!r} 超出支持范围（仅 numeric/string）")


def _parse_rules(meta, name: str, value_type: str) -> list[MissingRule]:
    rules: list[MissingRule] = []
    for entry in meta.missing_ranges.get(name) or []:
        lo, hi = entry["lo"], entry["hi"]
        kind = "discrete" if lo == hi else "range"
        rules.append(MissingRule(kind, lo, hi))
    # readstat 对 SAV 把用户缺失都放进 missing_ranges；missing_user_values 正常为空，
    # 若非空则按离散规则防御性并入。
    extra = (meta.missing_user_values or {}).get(name)
    if extra:
        vals = extra if isinstance(extra, (list, tuple)) else [extra]
        rules.extend(MissingRule("discrete", v, v) for v in vals)
    _validate_rules(name, value_type, rules)
    return rules


def _validate_rules(name: str, value_type: str, rules: list[MissingRule]) -> None:
    if value_type == "string":
        if any(r.kind != "discrete" for r in rules):
            raise SavImportError(f"字符串变量 {name!r} 只支持显式声明的离散用户缺失")
        return
    n_range = sum(r.kind == "range" for r in rules)
    n_disc = sum(r.kind == "discrete" for r in rules)
    ok = (n_range == 0 and n_disc <= 3) or (n_range == 1 and n_disc <= 1)
    if not ok:
        raise SavImportError(
            f"数值变量 {name!r} 的缺失规则超出支持范围"
            f"（≤3 个离散值，或一个闭区间加一个区间外离散值）：{rules}"
        )


def _classify_numeric(value: float, rules: list[MissingRule]):
    if isinstance(value, float) and math.isnan(value):
        return SYSTEM, None
    for rule in rules:
        if rule.matches(value):
            return USER, rule
    return OBSERVED, None


def _classify_string(value: str, rules: list[MissingRule]):
    for rule in rules:  # 已校验全部为 discrete；精确匹配，不 strip
        if rule.matches(value):
            return USER, rule
    return OBSERVED, None


# ---------------------------------------------------------------- 导入

def import_sav(src, out_dir) -> dict:
    """读取 SAV，构建四张表 + 清单并写入 out_dir。

    全部内容先在内存中构建并校验，任何失败都在写盘前抛出，不产出貌似完成的数据。
    返回 {"records", "variables", "value_labels", "missing_rules", "manifest"}（list[dict]）。
    """
    src = Path(src)
    out_dir = Path(out_dir)
    df, meta = _read_sav(src)

    names = list(meta.column_names)  # 文件中的变量顺序即列身份
    labels_map = meta.column_names_to_labels
    value_labels_map = meta.variable_value_labels

    # ---- variables 表 + 每变量的类型/规则/标签索引
    variables: list[dict] = []
    var_info: dict[str, dict] = {}
    for idx, name in enumerate(names, start=1):
        vtype = _value_type(meta, name)
        rules = _parse_rules(meta, name, vtype)
        labels = value_labels_map.get(name) or {}
        var_info[name] = {"type": vtype, "rules": rules, "labels": labels, "var_index": idx}
        variables.append({
            "var_index": idx,
            "variable": name,
            "label": labels_map.get(name) or "",
            "value_type": vtype,
            "original_type": meta.original_variable_types.get(name) or "",
            "storage_width": meta.variable_storage_width.get(name) or "",
        })

    # ---- value_labels 表（与数值分开保存；同一标签的多个原码各占一行，不合并）
    value_labels: list[dict] = []
    for name in names:
        info = var_info[name]
        for code, label in info["labels"].items():
            value_labels.append({
                "variable": name,
                "code": fmt_num(code) if info["type"] == "numeric" else str(code),
                "label": label,
            })

    # ---- missing_rules 表（以变量名为作用域）
    missing_rules: list[dict] = []
    for name in names:
        info = var_info[name]
        for ridx, rule in enumerate(info["rules"], start=1):
            lo = fmt_num(rule.lo) if info["type"] == "numeric" else str(rule.lo)
            hi = fmt_num(rule.hi) if info["type"] == "numeric" else str(rule.hi)
            missing_rules.append({
                "variable": name,
                "rule_index": ridx,
                "kind": rule.kind,
                "lo": lo,
                "hi": hi,
            })

    # ---- records 长表：行身份 = 源物理行号（1 起），受访者编号只是普通文本列
    records: list[dict] = []
    columns = {name: df[name].tolist() for name in names}
    n_rows = len(df)
    for row_id in range(1, n_rows + 1):
        for name in names:
            info = var_info[name]
            vtype = info["type"]
            value = columns[name][row_id - 1]
            if vtype == "numeric":
                kind, hit = _classify_numeric(value, info["rules"])
                raw_text = "" if kind == SYSTEM else fmt_num(value)
                label = "" if kind == SYSTEM else str(info["labels"].get(value, ""))
            else:
                if value is None:  # 防御：空白串读回为空串
                    value = ""
                kind, hit = _classify_string(value, info["rules"])
                raw_text = value
                label = str(info["labels"].get(value, ""))
            records.append({
                "row_id": row_id,
                "var_index": var_info[name]["var_index"],
                "variable": name,
                "value_type": vtype,
                "raw_value": raw_text,
                "value_label": label,
                "missing_kind": kind,
                "missing_rule": hit.text(vtype) if hit else "",
                "analysis_value": raw_text if kind == OBSERVED else "",
            })

    # ---- 清单：输入摘要、行列数、逐变量三类数量（由实际写出的长表统计）
    counts = {name: {OBSERVED: 0, USER: 0, SYSTEM: 0} for name in names}
    for rec in records:
        counts[rec["variable"]][rec["missing_kind"]] += 1
    totals = {k: sum(c[k] for c in counts.values()) for k in (OBSERVED, USER, SYSTEM)}
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    manifest = {
        "input": {
            "path": str(src),
            "sha256": digest,
            "bytes": src.stat().st_size,
            "file_encoding": meta.file_encoding or "",
            "file_label": meta.file_label or "",
        },
        "n_rows": n_rows,
        "n_columns": len(names),
        "variables": names,
        "counts": counts,
        "totals": {**totals, "cells": sum(totals.values())},
    }

    # ---- 全部成功后才落盘
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / TABLE_FILES["records"], RECORDS_FIELDS, records)
    _write_csv(out_dir / TABLE_FILES["variables"], VARIABLES_FIELDS, variables)
    _write_csv(out_dir / TABLE_FILES["value_labels"], VALUE_LABELS_FIELDS, value_labels)
    _write_csv(out_dir / TABLE_FILES["missing_rules"], MISSING_RULES_FIELDS, missing_rules)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "records": records,
        "variables": variables,
        "value_labels": value_labels,
        "missing_rules": missing_rules,
        "manifest": manifest,
    }


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------- 读回与溯源

def load_tables(out_dir) -> dict:
    """独立读回四张表（不依赖导入时的内存状态）。"""
    out_dir = Path(out_dir)
    tables = {}
    for key, fname in TABLE_FILES.items():
        with open(out_dir / fname, newline="", encoding="utf-8") as fh:
            tables[key] = list(csv.DictReader(fh))
    with open(out_dir / "manifest.json", encoding="utf-8") as fh:
        tables["manifest"] = json.load(fh)
    return tables


def inspect_cell(out_dir, row_id: int, variable: str) -> dict:
    """按源行号 + 变量名溯源一个单元格：原值、相关标签、缺失规则与分类依据。"""
    tables = load_tables(out_dir)
    var_row = next((v for v in tables["variables"] if v["variable"] == variable), None)
    if var_row is None:
        raise SavImportError(f"变量 {variable!r} 不存在于 {out_dir}")
    cell = next(
        (r for r in tables["records"]
         if int(r["row_id"]) == row_id and r["variable"] == variable),
        None,
    )
    if cell is None:
        raise SavImportError(f"源行号 {row_id} 不存在于 {out_dir}")
    rules = [r for r in tables["missing_rules"] if r["variable"] == variable]

    vtype = cell["value_type"]
    kind = cell["missing_kind"]
    lines = [f"== 单元格溯源：源第 {row_id} 行 × 变量 {variable!r} =="]
    label_txt = var_row["label"] or "（无变量标签）"
    lines.append(
        f"变量        : {variable}（列序 {var_row['var_index']}；{label_txt}；"
        f"{vtype}/{var_row['original_type']}）"
    )
    if vtype == "numeric" and kind == SYSTEM:
        lines.append("原始值      : SYSMIS（SAV 数值系统缺失哨兵，pyreadstat 读回为 NaN；非用户缺失码）")
    elif vtype == "string":
        note = "（空白字符串：SAV 定长字段以空格填充存储，读回为空串；不是系统缺失）" if cell["raw_value"] == "" else ""
        lines.append(f"原始值      : {cell['raw_value']!r} {note}")
    else:
        lines.append(f"原始值      : {cell['raw_value']}（binary64 原值，未舍入）")
    lines.append(f"值标签      : {cell['value_label'] or '（该原码无标签）'}")
    lines.append(f"missing_kind: {kind}")
    if rules:
        lines.append("缺失规则    :")
        for r in rules:
            txt = (f"discrete:{r['lo']}" if r["kind"] == "discrete" and vtype == "numeric"
                   else f"discrete:{r['lo']!r}" if r["kind"] == "discrete"
                   else f"range:[{r['lo']},{r['hi']}]")
            hit = "  <== 命中" if txt == cell["missing_rule"] else ""
            lines.append(f"  [{r['rule_index']}] {txt}{hit}")
    else:
        lines.append("缺失规则    : （该变量无用户缺失规则）")
    if kind == OBSERVED:
        av = cell["analysis_value"] if vtype == "numeric" else repr(cell["analysis_value"])
        lines.append(f"analysis_value: {av}")
    else:
        lines.append("analysis_value: （空——非 observed，不进入分析）")
    return {"cell": cell, "variable": var_row, "rules": rules, "report": "\n".join(lines)}


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sav_import", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_imp = sub.add_parser("import", help="导入 SAV 并导出四表 + manifest.json")
    p_imp.add_argument("src")
    p_imp.add_argument("out_dir")
    p_ins = sub.add_parser("inspect", help="按源行号 + 变量名溯源单元格")
    p_ins.add_argument("out_dir")
    p_ins.add_argument("row_id", type=int)
    p_ins.add_argument("variable")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "import":
            res = import_sav(args.src, args.out_dir)
            m = res["manifest"]
            print(f"导入完成：{m['n_rows']} 行 × {m['n_columns']} 列 -> {args.out_dir}")
            for name in m["variables"]:
                c = m["counts"][name]
                print(f"  {name}: observed={c[OBSERVED]} user={c[USER]} system={c[SYSTEM]}")
        else:
            print(inspect_cell(args.out_dir, args.row_id, args.variable)["report"])
    except SavImportError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
