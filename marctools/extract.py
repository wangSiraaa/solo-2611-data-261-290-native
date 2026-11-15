"""抽取驱动：生成四张基础表、配对表和诊断，并可写 CSV / JSON。"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .link import FieldLink, build_links
from .models import Diagnostic, ParsedRecord
from .parser import parse_record
from .reader import RecordText, decode_record, split_records


@dataclass
class ExtractionResult:
    source_file: str
    records: list[ParsedRecord] = field(default_factory=list)
    record_failures: list[dict] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    links: dict[int, dict[int, FieldLink]] = field(default_factory=dict)
    pair_ids: dict[tuple[int, int], int] = field(default_factory=dict)

    # 四张基础表的行
    bibliography_rows: list[dict] = field(default_factory=list)
    field_rows: list[dict] = field(default_factory=list)
    subfield_rows: list[dict] = field(default_factory=list)
    pairing_rows: list[dict] = field(default_factory=list)

    # 统计
    total_records: int = 0
    read_records: int = 0
    failed_records: int = 0
    isolated_880_count: int = 0
    unpaired_links_count: int = 0
    paired_count: int = 0

    def record_ids(self) -> list[int]:
        return [r.record_index for r in self.records]

    def pair_for(self, record_index: int, field_index: int) -> Optional[int]:
        links = self.links.get(record_index)
        if not links:
            return None
        info = links.get(field_index)
        return None if info is None else info.pair_field_index


def _source_label(record_index: int) -> str:
    return f"record[{record_index}]"


def _record_status(
    record_index: int,
    links: dict[int, FieldLink],
) -> str:
    """记录级状态：paired / mixed / ambiguous / unpaired / isolated_only / no_links。"""

    statuses = [link.status for link in links.values()]
    if any(status == "paired" for status in statuses):
        return (
            "mixed"
            if any(
                status in ("orphan_00", "unpaired", "ambiguous")
                for status in statuses
            )
            else "paired"
        )
    if any(status == "ambiguous" for status in statuses):
        return "ambiguous"
    if any(status == "unpaired" for status in statuses):
        return "unpaired"
    if any(status == "orphan_00" for status in statuses):
        return "isolated_only"
    return "no_links"


def extract_bytes(
    data: bytes,
    *,
    source_name: str = "<memory>",
    control_tags: Optional[set[str]] = None,
    max_fields: Optional[int] = None,
    max_records: Optional[int] = None,
) -> ExtractionResult:
    control_tags = control_tags or {"001", "005"}
    result = ExtractionResult(source_file=source_name)
    raws = split_records(data)
    result.total_records = len(raws)
    if max_records is not None and result.total_records > max_records:
        result.diagnostics.append(
            Diagnostic(
                record_index=-1,
                field_index=None,
                subfield_index=None,
                code="record_count_exceeds_scope",
                message=(
                    f"文件记录数 {result.total_records} 超过 scope 上限 "
                    f"{max_records}；仍逐条完整处理，不做截断"
                ),
                severity="warning",
                evidence={
                    "total_records": result.total_records,
                    "max_records": max_records,
                },
            )
        )

    for raw in raws:
        text, error = decode_record(raw)
        if error is not None:
            raw.error = error
            result.failed_records += 1
            result.record_failures.append(
                {
                    "record_index": raw.record_index,
                    "byte_start": raw.byte_start,
                    "byte_end": raw.byte_end,
                    "reason": error,
                    "source": _source_label(raw.record_index),
                }
            )
            continue

        parsed, diagnostics = parse_record(
            RecordText(raw=raw, text=text),
            control_tags=control_tags,
            max_fields=max_fields,
        )
        result.diagnostics.extend(diagnostics)
        if parsed is None:
            result.failed_records += 1
            reason = "; ".join(
                d.message for d in diagnostics if d.code == "record_parse_error"
            ) or "记录结构解析失败"
            result.record_failures.append(
                {
                    "record_index": raw.record_index,
                    "byte_start": raw.byte_start,
                    "byte_end": raw.byte_end,
                    "reason": reason,
                    "source": _source_label(raw.record_index),
                }
            )
            continue

        result.records.append(parsed)
        result.read_records += 1

        links, link_diagnostics = build_links(parsed)
        result.diagnostics.extend(link_diagnostics)
        result.links[parsed.record_index] = links

        _assign_pair_ids(result, parsed, links)
        _build_bibliography_row(result, parsed)
        _build_field_and_subfield_rows(result, parsed)

        for link in links.values():
            if link.status == "orphan_00":
                result.isolated_880_count += 1
            elif link.status in ("unpaired", "ambiguous"):
                result.unpaired_links_count += 1

        _build_pairing_rows(result, parsed, links)

    return result


def _assign_pair_ids(
    result: ExtractionResult,
    record: ParsedRecord,
    links: dict[int, FieldLink],
) -> None:
    """为已配对字段出现分配全局 pair_id，供各表按身份连接。"""

    for link in sorted(links.values(), key=lambda item: item.field_index):
        if link.role != "ordinary" or link.status != "paired":
            continue
        if link.pair_field_index is None:
            continue
        if (record.record_index, link.field_index) in result.pair_ids:
            continue
        result.paired_count += 1
        pair_id = result.paired_count
        result.pair_ids[(record.record_index, link.field_index)] = pair_id
        result.pair_ids[(record.record_index, link.pair_field_index)] = pair_id


def result_pair_count(links: dict[int, FieldLink]) -> int:
    """已建立配对的字段对数（每个配对两侧各一条 FieldLink）。"""

    return sum(1 for link in links.values() if link.status == "paired") // 2


def _build_bibliography_row(result: ExtractionResult, record: ParsedRecord) -> None:
    links = result.links.get(record.record_index, {})
    cn_values = record.control_numbers
    result.bibliography_rows.append(
        {
            "record_index": record.record_index,
            "source": _source_label(record.record_index),
            "source_byte_range": f"{record.byte_start}:{record.byte_end}",
            "byte_start": record.byte_start,
            "byte_end": record.byte_end,
            "leader": record.leader,
            "base_address": record.base_address,
            "control_number_001": cn_values[0] if cn_values else "",
            "control_number_001_all": " | ".join(cn_values),
            "control_number_count": len(cn_values),
            "field_count": len(record.fields),
            "data_field_count": len(record.data_fields),
            "subfield_count": record.subfield_count,
            "pair_count": result_pair_count(links),
            "orphan_880_count": sum(
                1 for link in links.values() if link.status == "orphan_00"
            ),
            "link_status": _record_status(record.record_index, links),
        }
    )


def _build_field_and_subfield_rows(
    result: ExtractionResult, record: ParsedRecord
) -> None:
    links = result.links.get(record.record_index, {})
    for marc_field in record.fields:
        link = links.get(marc_field.field_index)
        pair_index = (
            None if link is None else link.pair_field_index
        )
        result.field_rows.append(
            {
                "record_index": record.record_index,
                "source": _source_label(record.record_index),
                "field_index": marc_field.field_index,
                "tag_occurrence": marc_field.tag_occurrence,
                "tag": marc_field.tag,
                "is_control": int(marc_field.is_control),
                "indicator1": "" if marc_field.indicator1 is None else marc_field.indicator1,
                "indicator2": "" if marc_field.indicator2 is None else marc_field.indicator2,
                "control_value": marc_field.control_value or "",
                "subfield_count": len(marc_field.subfields),
                "byte_start": marc_field.byte_start,
                "byte_end": marc_field.byte_end,
                "terminator_byte": marc_field.terminator_byte,
                "directory_entry": marc_field.directory_entry,
                "link_status": "" if link is None else link.status,
                "link_has_subfield6": "" if link is None else int(link.has_subfield6),
                "link_subfield_index": (
                    ""
                    if link is None or link.link_subfield is None
                    else link.link_subfield.subfield_index
                ),
                "link_raw": "" if link is None else link.raw,
                "link_target_tag": (
                    "" if link is None or link.target_tag is None else link.target_tag
                ),
                "link_nn": "" if link is None or link.nn is None else link.nn,
                "link_suffix": (
                    "" if link is None or link.suffix is None else link.suffix
                ),
                "link_candidates": (
                    "" if link is None else "|".join(map(str, link.candidates))
                ),
                "pair_field_index": "" if pair_index is None else pair_index,
                "pair_id": (
                    ""
                    if pair_index is None
                    else result.pair_ids.get(
                        (record.record_index, marc_field.field_index), ""
                    )
                ),
            }
        )

        for subfield in marc_field.subfields:
            is_link = subfield.code == "6"
            result.subfield_rows.append(
                {
                    "record_index": record.record_index,
                    "source": _source_label(record.record_index),
                    "field_index": marc_field.field_index,
                    "tag": marc_field.tag,
                    "tag_occurrence": marc_field.tag_occurrence,
                    "subfield_index": subfield.subfield_index,
                    "code": subfield.code,
                    "value": subfield.value,
                    "is_link_subfield": int(is_link),
                    "byte_start": subfield.byte_start,
                    "byte_end": subfield.byte_end,
                    "value_byte_start": subfield.value_byte_start,
                    "value_byte_end": subfield.value_byte_end,
                }
            )


def _collect_evidence(
    record: ParsedRecord, field_index: int
) -> dict[str, Any]:
    marc_field = record.fields[field_index]
    return {
        "record_index": record.record_index,
        "field_index": marc_field.field_index,
        "tag": marc_field.tag,
        "tag_occurrence": marc_field.tag_occurrence,
        "indicator1": marc_field.indicator1,
        "indicator2": marc_field.indicator2,
        "byte_start": marc_field.byte_start,
        "byte_end": marc_field.byte_end,
        "directory_entry": marc_field.directory_entry,
        "subfield6_values": [
            {
                "subfield_index": sf.subfield_index,
                "value": sf.value,
                "byte_start": sf.byte_start,
                "byte_end": sf.byte_end,
            }
            for sf in marc_field.subfields
            if sf.code == "6"
        ],
        "subfields": [
            {"subfield_index": sf.subfield_index, "code": sf.code, "value": sf.value}
            for sf in marc_field.subfields
        ],
    }


def _pairing_base_row(
    record: ParsedRecord, link: FieldLink, status: str
) -> dict:
    marc_field = record.fields[link.field_index]
    return {
        "record_index": record.record_index,
        "source": _source_label(record.record_index),
        "pair_id": "",
        "status": status,
        "role": link.role,
        "tag": link.target_tag or marc_field.tag,
        "nn": link.nn or "",
        "suffix": link.suffix or "",
        "field_index": link.field_index,
        "tag_occurrence": marc_field.tag_occurrence,
        "field_tag": marc_field.tag,
        "indicator1": marc_field.indicator1 or "",
        "indicator2": marc_field.indicator2 or "",
        "subfield_count": len(marc_field.subfields),
        "byte_start": marc_field.byte_start,
        "byte_end": marc_field.byte_end,
        "link_raw": link.raw,
        "substitute_field_index": "",
        "substitute_tag_occurrence": "",
        "substitute_tag": "",
        "substitute_link_raw": "",
        "substitute_subfield_count": "",
        "substitute_byte_start": "",
        "substitute_byte_end": "",
        "ordinary_field_index": "",
        "ordinary_tag_occurrence": "",
        "ordinary_tag": "",
        "ordinary_indicator1": "",
        "ordinary_indicator2": "",
        "ordinary_subfield_count": "",
        "ordinary_byte_start": "",
        "ordinary_byte_end": "",
        "ordinary_link_raw": "",
        "candidate_field_indices": "",
        "note": "",
    }


def _attach_ordinary(row: dict, record: ParsedRecord, field_index: int) -> None:
    marc_field = record.fields[field_index]
    row.update(
        {
            "ordinary_field_index": field_index,
            "ordinary_tag_occurrence": marc_field.tag_occurrence,
            "ordinary_tag": marc_field.tag,
            "ordinary_indicator1": marc_field.indicator1 or "",
            "ordinary_indicator2": marc_field.indicator2 or "",
            "ordinary_subfield_count": len(marc_field.subfields),
            "ordinary_byte_start": marc_field.byte_start,
            "ordinary_byte_end": marc_field.byte_end,
            "ordinary_link_raw": record.fields[field_index]
            .subfield("6")[0]
            .value,
        }
    )


def _attach_substitute(row: dict, record: ParsedRecord, field_index: int) -> None:
    marc_field = record.fields[field_index]
    row.update(
        {
            "substitute_field_index": field_index,
            "substitute_tag_occurrence": marc_field.tag_occurrence,
            "substitute_tag": marc_field.tag,
            "substitute_subfield_count": len(marc_field.subfields),
            "substitute_byte_start": marc_field.byte_start,
            "substitute_byte_end": marc_field.byte_end,
            "substitute_link_raw": marc_field.subfield("6")[0].value,
        }
    )


def _build_pairing_rows(
    result: ExtractionResult,
    record: ParsedRecord,
    links: dict[int, FieldLink],
) -> None:
    """每个“有 $6 的字段出现”都在配对表里有一个可回查的行：

    * 已配对：普通字段与 880 字段合成一行（pair_id 唯一）；
    * 歧义：所有候选字段出现各有一行，candidate_field_indices 列出全部候选，
      不分配 pair_id；
    * 缺反向链接：缺失的一方没有字段出现，行里只列出实际存在的一方；
    * 孤立 00、无 $6 的普通字段、语法不合法/多次 $6：属于无配对关系，
      在字段表与诊断中完整保留，不在配对表伪造关系。
    """

    paired_handled: set[int] = set()
    for link in links.values():
        if link.field_index in paired_handled:
            continue

        if link.status == "paired" and link.pair_field_index is not None:
            ordinary_index = (
                link.field_index
                if link.role == "ordinary"
                else link.pair_field_index
            )
            substitute_index = (
                link.pair_field_index
                if link.role == "ordinary"
                else link.field_index
            )
            ordinary_link = links[ordinary_index]
            substitute_link = links[substitute_index]
            paired_handled.update({ordinary_index, substitute_index})

            pair_id = result.pair_ids[
                (record.record_index, ordinary_index)
            ]

            ordinary_field = record.fields[ordinary_index]
            substitute_field = record.fields[substitute_index]
            row = _pairing_base_row(record, ordinary_link, "paired")
            row["pair_id"] = pair_id
            row["tag"] = ordinary_field.tag
            row["nn"] = ordinary_link.nn or substitute_link.nn or ""
            row["suffix"] = ordinary_link.suffix or substitute_link.suffix or ""
            row["field_index"] = ordinary_index
            row["tag_occurrence"] = ordinary_field.tag_occurrence
            row["field_tag"] = ordinary_field.tag
            row["indicator1"] = ordinary_field.indicator1 or ""
            row["indicator2"] = ordinary_field.indicator2 or ""
            row["subfield_count"] = len(ordinary_field.subfields)
            row["byte_start"] = ordinary_field.byte_start
            row["byte_end"] = ordinary_field.byte_end
            row["link_raw"] = ordinary_link.raw
            row["role"] = "ordinary"
            _attach_ordinary(row, record, ordinary_index)
            _attach_substitute(row, record, substitute_index)
            row["evidence"] = json.dumps(
                _collect_evidence(record, ordinary_index),
                ensure_ascii=False,
                sort_keys=True,
            )
            row["counterpart_evidence"] = json.dumps(
                _collect_evidence(record, substitute_index),
                ensure_ascii=False,
                sort_keys=True,
            )
            result.pairing_rows.append(row)
            continue

        if link.status == "ambiguous":
            row = _pairing_base_row(record, link, "ambiguous")
            row["candidate_field_indices"] = "|".join(
                str(i) for i in link.candidates
            )
            row["note"] = "候选不唯一，不建立配对，也不任选一个"
            _attach_row_side(row, record, link)
            row["evidence"] = json.dumps(
                _collect_evidence(record, link.field_index),
                ensure_ascii=False,
                sort_keys=True,
            )
            result.pairing_rows.append(row)
            continue

        if link.status == "unpaired":
            row = _pairing_base_row(record, link, "unpaired")
            if link.role == "ordinary":
                missing = [
                    i for i, other in links.items()
                    if other.role == "880"
                    and other.target_tag == link.target_tag
                    and other.nn == link.nn
                ]
                if missing:
                    # 对方存在但自身不唯一/自身多次 $6 等；仍列出候选。
                    row["candidate_field_indices"] = "|".join(
                        str(i) for i in missing
                    )
                row["note"] = "缺反向链接：记录内没有匹配的 880 字段"
                _attach_ordinary(row, record, link.field_index)
            else:
                missing = [
                    i for i, other in links.items()
                    if other.role == "ordinary"
                    and other.target_tag == link.target_tag
                    and other.nn == link.nn
                ]
                if missing:
                    row["candidate_field_indices"] = "|".join(
                        str(i) for i in missing
                    )
                row["note"] = "缺反向链接：记录内没有匹配的普通字段"
                _attach_substitute(row, record, link.field_index)
            row["evidence"] = json.dumps(
                _collect_evidence(record, link.field_index),
                ensure_ascii=False,
                sort_keys=True,
            )
            result.pairing_rows.append(row)


def _attach_row_side(
    row: dict,
    record: ParsedRecord,
    link: FieldLink,
) -> None:
    if link.role == "ordinary":
        _attach_ordinary(row, record, link.field_index)
    else:
        _attach_substitute(row, record, link.field_index)


def extract_file(
    path: str | Path,
    *,
    control_tags: Optional[set[str]] = None,
    max_fields: Optional[int] = None,
    max_records: Optional[int] = None,
    scope_path: Optional[str | Path] = None,
) -> ExtractionResult:
    path = Path(path)
    if scope_path is None:
        candidate = path.parent / "scope.json"
        if candidate.exists():
            scope_path = candidate
    if scope_path is not None:
        scope = json.loads(Path(scope_path).read_text(encoding="utf-8"))
        control_tags = set(scope.get("control_tags", ["001", "005"]))
        max_fields = scope.get("max_fields_per_record", max_fields)
        max_records = scope.get("max_records", max_records)
    return extract_bytes(
        path.read_bytes(),
        source_name=str(path),
        control_tags=control_tags,
        max_fields=max_fields,
        max_records=max_records,
    )


def write_csv(result: ExtractionResult, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    tables = {
        "bibliographic_records": result.bibliography_rows,
        "fields": result.field_rows,
        "subfields": result.subfield_rows,
        "substitute_pairings": result.pairing_rows,
        "diagnostics": [d.as_row() for d in result.diagnostics],
        "record_failures": result.record_failures,
        "summary": [summary_row(result)],
    }
    for name, rows in tables.items():
        path = out / f"{name}.csv"
        _write_rows(path, rows, EMPTY_TABLE_COLUMNS.get(name))
        paths[name] = path
    return paths


EMPTY_TABLE_COLUMNS = {
    "bibliographic_records": [
        "record_index", "source", "source_byte_range", "byte_start",
        "byte_end", "leader", "base_address", "control_number_001",
        "control_number_001_all", "control_number_count", "field_count",
        "data_field_count", "subfield_count", "pair_count",
        "orphan_880_count", "link_status",
    ],
    "fields": [
        "record_index", "source", "field_index", "tag_occurrence", "tag",
        "is_control", "indicator1", "indicator2", "control_value",
        "subfield_count", "byte_start", "byte_end", "terminator_byte",
        "directory_entry", "link_status", "link_has_subfield6",
        "link_subfield_index", "link_raw", "link_target_tag", "link_nn",
        "link_suffix", "link_candidates", "pair_field_index", "pair_id",
    ],
    "subfields": [
        "record_index", "source", "field_index", "tag", "tag_occurrence",
        "subfield_index", "code", "value", "is_link_subfield",
        "byte_start", "byte_end", "value_byte_start", "value_byte_end",
    ],
    "substitute_pairings": [
        "record_index", "source", "pair_id", "status", "role", "tag",
        "nn", "suffix", "field_index", "tag_occurrence", "field_tag",
        "indicator1", "indicator2", "subfield_count", "byte_start",
        "byte_end", "link_raw", "substitute_field_index",
        "substitute_tag_occurrence", "substitute_tag",
        "substitute_link_raw", "substitute_subfield_count",
        "substitute_byte_start", "substitute_byte_end",
        "ordinary_field_index", "ordinary_tag_occurrence", "ordinary_tag",
        "ordinary_indicator1", "ordinary_indicator2",
        "ordinary_subfield_count", "ordinary_byte_start",
        "ordinary_byte_end", "ordinary_link_raw",
        "candidate_field_indices", "note", "evidence",
        "counterpart_evidence",
    ],
    "diagnostics": [
        "record_index", "field_index", "subfield_index", "tag",
        "link_value", "severity", "code", "message", "evidence",
    ],
    "record_failures": [
        "record_index", "byte_start", "byte_end", "reason", "source",
    ],
}


def _write_rows(
    path: Path, rows: list[dict], empty_columns: list[str] | None = None
) -> None:
    if rows:
        fieldnames = list(rows[0].keys())
    else:
        fieldnames = empty_columns or ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def summary_row(result: ExtractionResult) -> dict:
    return {
        "source_file": result.source_file,
        "total_records": result.total_records,
        "read_records": result.read_records,
        "failed_records": result.failed_records,
        "isolated_880_records": sum(
            1
            for record in result.records
            if any(
                link.status == "orphan_00"
                for link in result.links.get(record.record_index, {}).values()
            )
        ),
        "isolated_880_fields": result.isolated_880_count,
        "paired_field_pairs": result.paired_count,
        "unpaired_or_ambiguous_links": result.unpaired_links_count,
    }


def write_json(result: ExtractionResult, path: str | Path) -> Path:
    path = Path(path)
    payload = {
        "source_file": result.source_file,
        "summary": summary_row(result),
        "bibliographic_records": result.bibliography_rows,
        "fields": result.field_rows,
        "subfields": result.subfield_rows,
        "substitute_pairings": result.pairing_rows,
        "diagnostics": [d.as_row() for d in result.diagnostics],
        "record_failures": result.record_failures,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def record_summary_lines(result: ExtractionResult) -> list[str]:
    """人类可读的成功清单：明确区分已读/失败/孤立，不只报配对数量。"""

    summary = summary_row(result)
    failed_ids = [f"record[{f['record_index']}]" for f in result.record_failures]
    isolated_ids: list[str] = []
    for record in result.records:
        for link in result.links.get(record.record_index, {}).values():
            if link.status == "orphan_00":
                isolated_ids.append(
                    f"record[{record.record_index}]:field[{link.field_index}]"
                )

    lines = [
        f"来源文件: {summary['source_file']}",
        f"文件内记录总数: {summary['total_records']}（按字节记录边界计）",
        f"已成功读取记录: {summary['read_records']}",
        f"失败记录: {summary['failed_records']}"
        + (f" -> {', '.join(failed_ids)}" if failed_ids else ""),
        f"孤立 880 字段(NN=00): {summary['isolated_880_fields']}"
        + (f" -> {', '.join(isolated_ids)}" if isolated_ids else ""),
        f"成功配对字段对: {summary['paired_field_pairs']}",
        f"未配对或歧义链接: {summary['unpaired_or_ambiguous_links']}",
    ]
    return lines
