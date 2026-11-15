"""$6 链接解析与同一记录内的字段出现配对。

只解释 `TAG-NN[/suffix]`：

* 普通字段的 $6 为 `880-NN[/suffix]`（指向 880）；
* 880 字段的 $6 为 `TAG-NN[/suffix]`（指回普通字段的 tag）；
* NN 是两位十进制；`00` 表示没有可配对普通字段的孤立替代文字；
* 只在同一记录内、tag 匹配、NN 一致、双方各自唯一时配对；
* suffix 原样保存，绝不用于猜测文字语言或参与匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .models import Diagnostic, MarcField, ParsedRecord, Subfield

LINK_RE = re.compile(r"^(?P<tag>[0-9]{3})-(?P<nn>[0-9]{2})(?P<suffix>/[^\s]+)?$")


@dataclass
class FieldLink:
    """一个字段出现的 $6 解析结果和配对状态。"""

    record_index: int
    field_index: int
    role: str  # ordinary / 880
    status: str  # no_link / no_link_880 / orphan_00 / invalid / multiple / unpaired / ambiguous / paired
    has_subfield6: bool = False
    link_count: int = 0
    raw_values: list[str] = field(default_factory=list)
    link_subfield: Optional[Subfield] = None
    target_tag: Optional[str] = None
    nn: Optional[str] = None
    suffix: Optional[str] = None
    pair_id: Optional[int] = None
    pair_field_index: Optional[int] = None
    candidates: list[int] = field(default_factory=list)

    @property
    def raw(self) -> str:
        return self.link_subfield.value if self.link_subfield else ""


def _diagnostic(
    record_index: int,
    field: MarcField | None,
    code: str,
    message: str,
    *,
    severity: str = "error",
    link_value: str | None = None,
    subfield_index: int | None = None,
    evidence: dict | None = None,
) -> Diagnostic:
    return Diagnostic(
        record_index=record_index,
        field_index=None if field is None else field.field_index,
        subfield_index=subfield_index,
        tag=None if field is None else field.tag,
        link_value=link_value,
        code=code,
        message=message,
        severity=severity,
        evidence=evidence or {},
    )


def _parse_link(
    value: str,
    role: str,
    field_tag: str,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """返回 (target_tag, nn, suffix, error)。

    普通字段的 `target_tag` 取本字段 tag（880 的 $6 会再次声明它）；
    880 字段的 `target_tag` 取 $6 中声明的 tag。
    """

    if not value:
        return None, None, None, "值为空"
    if value != value.strip() or any(ch.isspace() for ch in value):
        return None, None, None, "值中包含空白字符"

    match = LINK_RE.match(value)
    if match is None:
        return None, None, None, "不符合 TAG-NN[/suffix] 语法"

    tag = match.group("tag")
    nn = match.group("nn")
    suffix = match.group("suffix")

    if role == "ordinary":
        if tag != "880":
            return (
                None,
                None,
                None,
                "普通字段的 $6 必须指向 880（形如 880-NN）",
            )
        target_tag = field_tag
    else:
        if tag == "880":
            return None, None, None, "880 字段的 $6 不能指向 880 自身"
        target_tag = tag

    return target_tag, nn, suffix, None


def build_links(
    record: ParsedRecord,
) -> tuple[dict[int, FieldLink], list[Diagnostic]]:
    """解析一条记录内全部数据字段的 $6，并建立配对。"""

    diagnostics: list[Diagnostic] = []
    links: dict[int, FieldLink] = {}
    ordinary_groups: dict[tuple[str, str], list[FieldLink]] = {}
    substitute_groups: dict[tuple[str, str], list[FieldLink]] = {}

    for marc_field in record.fields:
        if marc_field.is_control:
            continue

        role = "880" if marc_field.tag == "880" else "ordinary"
        link_subfields = [
            sf for sf in marc_field.subfields if sf.code == "6"
        ]

        if not link_subfields:
            if role == "880":
                info = FieldLink(
                    record_index=record.record_index,
                    field_index=marc_field.field_index,
                    role=role,
                    status="no_link_880",
                )
                diagnostics.append(
                    _diagnostic(
                        record.record_index,
                        marc_field,
                        "missing_link_subfield",
                        "880 字段没有 $6，无法确定它替代哪个普通字段",
                        severity="warning",
                    )
                )
            else:
                info = FieldLink(
                    record_index=record.record_index,
                    field_index=marc_field.field_index,
                    role=role,
                    status="no_link",
                )
            links[marc_field.field_index] = info
            continue

        if len(link_subfields) > 1:
            values = [sf.value for sf in link_subfields]
            info = FieldLink(
                record_index=record.record_index,
                field_index=marc_field.field_index,
                role=role,
                status="multiple",
                has_subfield6=True,
                link_count=len(link_subfields),
                raw_values=values,
                link_subfield=link_subfields[0],
            )
            links[marc_field.field_index] = info
            diagnostics.append(
                _diagnostic(
                    record.record_index,
                    marc_field,
                    "multiple_link_subfields",
                    "同一字段出现多个 $6；不从中挑选一个用于配对",
                    link_value=" | ".join(values),
                    subfield_index=link_subfields[0].subfield_index,
                    evidence={
                        "subfield_indices": [
                            sf.subfield_index for sf in link_subfields
                        ],
                        "values": values,
                    },
                )
            )
            continue

        link_sf = link_subfields[0]
        target_tag, nn, suffix, error = _parse_link(
            link_sf.value, role, marc_field.tag
        )
        info = FieldLink(
            record_index=record.record_index,
            field_index=marc_field.field_index,
            role=role,
            status="invalid",
            has_subfield6=True,
            link_count=1,
            raw_values=[link_sf.value],
            link_subfield=link_sf,
            target_tag=target_tag,
            nn=nn,
            suffix=suffix,
        )
        links[marc_field.field_index] = info

        if error is not None:
            diagnostics.append(
                _diagnostic(
                    record.record_index,
                    marc_field,
                    "invalid_link_syntax",
                    f"$6 语法不合法：{error}",
                    link_value=link_sf.value,
                    subfield_index=link_sf.subfield_index,
                )
            )
            continue

        assert nn is not None and target_tag is not None
        if nn == "00":
            if role == "880":
                info.status = "orphan_00"
                diagnostics.append(
                    _diagnostic(
                        record.record_index,
                        marc_field,
                        "orphan_880_00",
                        "880 的 $6 编号为 00：孤立替代文字，不参与配对",
                        severity="info",
                        link_value=link_sf.value,
                        subfield_index=link_sf.subfield_index,
                        evidence={"target_tag": target_tag, "nn": nn},
                    )
                )
            else:
                info.status = "invalid"
                diagnostics.append(
                    _diagnostic(
                        record.record_index,
                        marc_field,
                        "link_number_00_on_ordinary_field",
                        "普通字段的 $6 编号为 00；00 仅用于孤立 880 替代文字",
                        link_value=link_sf.value,
                        subfield_index=link_sf.subfield_index,
                    )
                )
            continue

        info.status = "unpaired"
        key = (target_tag, nn)
        if role == "ordinary":
            ordinary_groups.setdefault(key, []).append(info)
        else:
            substitute_groups.setdefault(key, []).append(info)

    # 同一 (TAG, NN) 分组配对。双方都恰好一个出现才建立关系。
    all_keys = sorted(set(ordinary_groups) | set(substitute_groups))
    for key in all_keys:
        target_tag, nn = key
        ordinaries = ordinary_groups.get(key, [])
        substitutes = substitute_groups.get(key, [])
        ordinary_indices = [link.field_index for link in ordinaries]
        substitute_indices = [link.field_index for link in substitutes]

        if len(ordinaries) == 1 and len(substitutes) == 1:
            ordinary = ordinaries[0]
            substitute = substitutes[0]
            ordinary.status = "paired"
            substitute.status = "paired"
            ordinary.pair_field_index = substitute.field_index
            substitute.pair_field_index = ordinary.field_index
            ordinary.candidates = substitute_indices
            substitute.candidates = ordinary_indices
            continue

        if not substitutes:
            # 普通字段声明了链接，但记录内没有对应的 880：缺反向链接。
            for link in ordinaries:
                link.status = "unpaired"
                link.candidates = []
                diagnostics.append(
                    _diagnostic(
                        record.record_index,
                        record.fields[link.field_index],
                        "missing_880_counterpart",
                        (
                            "普通字段声明了 880 链接，但同一记录内没有"
                            " tag 与 NN 都匹配的 880 字段"
                        ),
                        severity="warning",
                        link_value=link.raw,
                        subfield_index=(
                            None
                            if link.link_subfield is None
                            else link.link_subfield.subfield_index
                        ),
                        evidence={"target_tag": target_tag, "nn": nn},
                    )
                )
            if len(ordinaries) > 1:
                diagnostics.append(
                    _diagnostic(
                        record.record_index,
                        None,
                        "duplicate_ordinary_candidates",
                        "同一 (TAG, NN) 下有多个普通字段，候选重复",
                        severity="warning",
                        evidence={
                            "target_tag": target_tag,
                            "nn": nn,
                            "ordinary_field_indices": ordinary_indices,
                        },
                    )
                )
            continue

        if not ordinaries:
            # 880 声明了链接，但没有普通字段承接：缺反向链接。
            for link in substitutes:
                link.status = "unpaired"
                link.candidates = []
                diagnostics.append(
                    _diagnostic(
                        record.record_index,
                        record.fields[link.field_index],
                        "missing_ordinary_counterpart",
                        (
                            "880 字段声明了反向链接，但同一记录内没有"
                            " tag 与 NN 都匹配的普通字段"
                        ),
                        severity="warning",
                        link_value=link.raw,
                        subfield_index=(
                            None
                            if link.link_subfield is None
                            else link.link_subfield.subfield_index
                        ),
                        evidence={"target_tag": target_tag, "nn": nn},
                    )
                )
            if len(substitutes) > 1:
                diagnostics.append(
                    _diagnostic(
                        record.record_index,
                        None,
                        "duplicate_880_candidates",
                        "同一 (TAG, NN) 下有多个 880 字段，候选重复",
                        severity="warning",
                        evidence={
                            "target_tag": target_tag,
                            "nn": nn,
                            "substitute_field_indices": substitute_indices,
                        },
                    )
                )
            continue

        # 双方都有候选，但至少一边不唯一：歧义，不任挑一个。
        for link in ordinaries + substitutes:
            link.status = "ambiguous"
            link.candidates = (
                substitute_indices if link.role == "ordinary" else ordinary_indices
            )
        diagnostics.append(
            _diagnostic(
                record.record_index,
                None,
                "ambiguous_link_candidates",
                (
                    "同一 (TAG, NN) 下普通字段或 880 字段的候选不唯一，"
                    "不建立配对"
                ),
                link_value=f"{target_tag}-{nn}",
                evidence={
                    "target_tag": target_tag,
                    "nn": nn,
                    "ordinary_field_indices": ordinary_indices,
                    "substitute_field_indices": substitute_indices,
                    "ordinary_count": len(ordinaries),
                    "substitute_count": len(substitutes),
                },
            )
        )

    return links, diagnostics
