"""extract：把单个 XLIFF 2.0 文件整理为逐 segment 的双语语料结构。

输出（:class:`ExtractionResult`，可 ``to_dict()`` 序列化为 JSON）包含：

* ``manifest``      —— 每个 segment 一条，含两侧 token 序列与入表/排除原因；
* ``pairs``         —— 仅可训练片段的配对表，携带代码 id 与载荷回查信息；
* ``code_table``    —— unit 局部的 originalData 载荷表（跨 unit 不共享）；
* ``diagnostics``   —— 带文件/unit/segment/XML 节点路径的诊断；
* ``file.status``   —— XML 整体损坏或文件级致命问题时为 ``failed``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional
from xml.etree import ElementTree as ET

from . import reasons as R
from .tokenizer import (
    CLOSE,
    OPEN,
    PLACEHOLDER,
    SideParse,
    Token,
    localname,
    pc_containment,
    serialize_tokens,
    tokenize,
)

SCHEMA_VERSION = "1.0"
DEFAULT_SCOPE = {
    "namespace": "urn:oasis:names:tc:xliff:document:2.0",
    "supported": ["unit", "originalData/data", "segment", "source", "target", "ph", "pc"],
    "max_units": 20,
    "accepted_states": ["translated", "final"],
}


def load_scope(path: Optional[str]) -> dict:
    if path is None:
        return dict(DEFAULT_SCOPE)
    with open(path, "r", encoding="utf-8") as fh:
        scope = json.load(fh)
    # 缺字段回退默认值，避免 scope 本身写得简略时崩溃。
    for key, value in DEFAULT_SCOPE.items():
        scope.setdefault(key, value)
    return scope


@dataclass
class ExtractionResult:
    source_file: str
    scope: dict
    file_id: Optional[str]
    file_status: str  # "ok" | "failed"
    code_table: dict = field(default_factory=dict)
    manifest: list[dict] = field(default_factory=list)
    pairs: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_file": self.source_file,
            "scope": self.scope,
            "file": {"id": self.file_id, "status": self.file_status},
            "stats": self.stats,
            "code_table": self.code_table,
            "manifest": self.manifest,
            "pairs": self.pairs,
            "diagnostics": self.diagnostics,
        }


def extract_file(xliff_path: str, scope_path: Optional[str] = None) -> ExtractionResult:
    scope = load_scope(scope_path)
    ns = scope["namespace"]
    accepted_states = set(scope["accepted_states"])
    max_units = int(scope["max_units"])

    result = ExtractionResult(
        source_file=xliff_path, scope=scope, file_id=None, file_status="ok"
    )

    def diag(code: str, message: str, *, severity: str = "error",
             unit_id: Optional[str] = None, segment_id: Optional[str] = None,
             path: Optional[str] = None) -> None:
        result.diagnostics.append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "location": {
                    "file_id": result.file_id,
                    "unit_id": unit_id,
                    "segment_id": segment_id,
                    "path": path,
                },
            }
        )

    # ---- 1. 整体 XML 解析：损坏即文件失败，不宣称成功 ----
    try:
        tree = ET.parse(xliff_path)
    except (ET.ParseError, FileNotFoundError, OSError) as exc:
        result.file_status = "failed"
        diag(R.DIAG_XML_PARSE_ERROR, f"XML 解析失败: {exc}", severity="fatal",
             path=xliff_path)
        _fill_stats(result)
        return result

    root = tree.getroot()
    parent_map = {child: parent for parent in root.iter() for child in parent}

    if localname(root.tag) != "xliff":
        result.file_status = "failed"
        diag(R.DIAG_ROOT_NOT_XLIFF, f"根元素是 {root.tag}，不是 xliff",
             severity="fatal", path="/" + localname(root.tag))
        _fill_stats(result)
        return result
    if root.tag != f"{{{ns}}}xliff":
        result.file_status = "failed"
        diag(R.DIAG_SCOPE_NAMESPACE_MISMATCH,
             f"根命名空间 {root.tag} 与 scope 命名空间 {ns} 不一致",
             severity="fatal", path="/xliff")
        _fill_stats(result)
        return result

    # ---- 2. 只支持单个 file ----
    files = [c for c in root if localname(c.tag) == "file"]
    if len(files) != 1:
        result.file_status = "failed"
        diag(R.DIAG_FILE_COUNT, f"期望恰好 1 个 file，实际 {len(files)} 个",
             severity="fatal", path="/xliff")
        _fill_stats(result)
        return result
    file_el = files[0]
    result.file_id = file_el.get("id")

    def xpath(node: ET.Element) -> str:
        chain: list[str] = []
        cur: Optional[ET.Element] = node
        while cur is not None:
            parent = parent_map.get(cur)
            if parent is None:
                chain.append(localname(cur.tag))
                break
            label = localname(cur.tag)
            nid = cur.get("id")
            if nid is not None:
                label += f"[@id='{nid}']"
            chain.append(f"{label}[{list(parent).index(cur)}]")
            cur = parent
        chain.reverse()
        return "/" + "/".join(chain)

    units = [c for c in file_el if localname(c.tag) == "unit"]
    other_children = [c for c in file_el if localname(c.tag) != "unit"]
    for extra in other_children:
        diag(R.DIAG_UNSUPPORTED_UNIT_CHILD,
             f"file 下不支持的子元素 <{localname(extra.tag)}>（本子集只支持 unit）",
             severity="warning", path=xpath(extra))

    if len(units) > max_units:
        result.file_status = "failed"
        diag(R.DIAG_TOO_MANY_UNITS,
             f"unit 数量 {len(units)} 超过上限 {max_units}", severity="fatal",
             path=xpath(file_el))

    seen_unit_ids: set[str] = set()
    seen_segment_keys: set[tuple[str, str]] = set()

    for unit_ordinal, unit in enumerate(units):
        uid = unit.get("id")
        unit_path = xpath(unit)
        if uid is None:
            result.file_status = "failed"
            diag(R.DIAG_MISSING_UNIT_ID, "unit 缺少 id 属性", severity="fatal",
                 path=unit_path)
            uid = f"__nounitid__{unit_ordinal}"
        elif uid in seen_unit_ids:
            result.file_status = "failed"
            diag(R.DIAG_DUPLICATE_UNIT_ID, f"unit id 重复: {uid}", severity="fatal",
                 unit_id=uid, path=unit_path)
        seen_unit_ids.add(uid)

        # ---- 3. unit/originalData/data：载荷只在本 unit 内解析 ----
        data_map: dict[str, dict] = {}
        od = next((c for c in unit if localname(c.tag) == "originalData"), None)
        if od is not None:
            for data_el in od:
                if localname(data_el.tag) != "data":
                    diag(R.DIAG_UNSUPPORTED_UNIT_CHILD,
                         f"originalData 下不支持的子元素 <{localname(data_el.tag)}>",
                         severity="warning", unit_id=uid, path=xpath(data_el))
                    continue
                did = data_el.get("id")
                # 实体已由 XML 解析器解码；"".join 取全部文本，绝不执行 HTML。
                payload = "".join(data_el.itertext())
                if did is None:
                    diag(R.DIAG_MISSING_REF_ATTR, "data 缺少 id 属性",
                         unit_id=uid, path=xpath(data_el))
                    continue
                if did in data_map:
                    data_map[did]["duplicate"] = True
                    diag(R.DIAG_DUPLICATE_DATA_ID,
                         f"unit {uid} 内 data id 重复: {did}", unit_id=uid,
                         path=xpath(data_el))
                else:
                    data_map[did] = {"payload": payload, "duplicate": False}
        result.code_table[uid] = data_map

        for extra in unit:
            if localname(extra.tag) not in ("originalData", "segment"):
                diag(R.DIAG_UNSUPPORTED_UNIT_CHILD,
                     f"unit 下不支持的子元素 <{localname(extra.tag)}>",
                     severity="warning", unit_id=uid, path=xpath(extra))

        translate = unit.get("translate", "yes")
        segments = [c for c in unit if localname(c.tag) == "segment"]
        for seg_ordinal, seg in enumerate(segments):
            _process_segment(
                seg, uid, translate, unit_ordinal, seg_ordinal, data_map,
                seen_segment_keys, parent_map, root, accepted_states, result,
                diag, xpath,
            )

    # ---- 4. 训练配对表 ----
    for record in result.manifest:
        if record["trainable"]:
            result.pairs.append(_build_pair(record, result.code_table))

    _fill_stats(result)
    return result


def _fill_stats(result: ExtractionResult) -> None:
    seg_diags = [d for d in result.diagnostics if d["location"]["segment_id"] is not None]
    result.stats = {
        "segments_total": len(result.manifest),
        "segments_trainable": len(result.pairs),
        "segments_excluded": len(result.manifest) - len(result.pairs),
        "diagnostics_total": len(result.diagnostics),
        "segment_diagnostics": len(seg_diags),
        "file_fatal": result.file_status == "failed",
    }


def _process_segment(seg, uid, translate, unit_ordinal, seg_ordinal, data_map,
                     seen_segment_keys, parent_map, root, accepted_states,
                     result, diag, xpath) -> None:
    sid = seg.get("id")
    seg_path = xpath(seg)
    if sid is None:
        diag(R.DIAG_MISSING_SEGMENT_ID, "segment 缺少 id 属性",
             unit_id=uid, path=seg_path)
        sid = f"__nosegid__{seg_ordinal}"
    key = (uid, sid)
    if key in seen_segment_keys:
        diag(R.DIAG_DUPLICATE_SEGMENT_ID,
             f"unit {uid} 内 segment id 重复: {sid}", unit_id=uid, segment_id=sid,
             path=seg_path)
    seen_segment_keys.add(key)

    state = seg.get("state", "initial")  # 默认 initial
    source_el = next((c for c in seg if localname(c.tag) == "source"), None)
    target_el = next((c for c in seg if localname(c.tag) == "target"), None)
    for extra in seg:
        if localname(extra.tag) not in ("source", "target"):
            diag(R.DIAG_UNSUPPORTED_INLINE_TAG,
                 f"segment 下不支持的子元素 <{localname(extra.tag)}>",
                 unit_id=uid, segment_id=sid, path=xpath(extra))

    seg_diags: list[str] = []

    if source_el is None:
        diag(R.DIAG_SOURCE_MISSING, "segment 缺少 source",
             unit_id=uid, segment_id=sid, path=seg_path)
        seg_diags.append(R.DIAG_SOURCE_MISSING)
        source_side = None
    else:
        source_side = _build_side(source_el, "source", uid, sid, data_map,
                                  parent_map, root, xpath, diag, seg_diags)

    has_target = target_el is not None
    target_empty = False
    target_side = None
    if has_target:
        target_empty = _is_empty(target_el)
        target_side = _build_side(target_el, "target", uid, sid, data_map,
                                  parent_map, root, xpath, diag, seg_diags)

    # ---- 跨侧代码关系核查（target 缺失则无从比较） ----
    if source_side is not None and has_target:
        _check_cross_side(source_side, target_side, uid, sid, seg_path,
                          diag, seg_diags)

    reasons: list[str] = []
    if translate != "yes":
        reasons.append(R.REASON_UNIT_TRANSLATE_NO)
    if state not in accepted_states:
        reasons.append(R.REASON_STATE_NOT_ACCEPTED)
    if not has_target:
        reasons.append(R.REASON_TARGET_MISSING)
    elif target_empty:
        reasons.append(R.REASON_TARGET_EMPTY)
    if R.DIAG_SOURCE_MISSING in seg_diags:
        reasons.append(R.REASON_SOURCE_MISSING)
    if any(code in R.SEGMENT_CODE_DIAGS for code in seg_diags):
        reasons.append(R.REASON_CODE_MISMATCH)

    trainable = not reasons
    record = {
        "identity": {
            "file_id": result.file_id,
            "unit_id": uid,
            "segment_id": sid,
            "unit_ordinal": unit_ordinal,
            "segment_ordinal": seg_ordinal,
        },
        "unit_translate": translate,
        "state": state,
        "has_target": has_target,
        "target_empty": target_empty,
        "source": None if source_side is None else _side_record(source_side),
        "target": None if target_side is None else _side_record(target_side),
        "code_ids": {
            "source": _code_id_list(source_side),
            "target": _code_id_list(target_side),
        },
        "trainable": trainable,
        "reasons": reasons,
        "diagnostic_codes": seg_diags,
    }
    result.manifest.append(record)


def _is_empty(element: ET.Element) -> bool:
    """有节点但无任何文本与子节点（<target/> 与 <target></target> 同义）。"""

    return (element.text is None or element.text == "") and len(list(element)) == 0


def _build_side(element, side, uid, sid, data_map, parent_map, root,
                xpath, diag, seg_diags) -> SideParse:
    parsed = tokenize(element, xpath(element), parent_map, root)
    base = f"{uid}/{sid}/{side}"

    for kind, payload, path in parsed.events:
        if kind == "unsupported":
            code = R.DIAG_UNSUPPORTED_INLINE_TAG
            diag(code, f"{side} 含不支持的内联标签 <{payload}>",
                 unit_id=uid, segment_id=sid, path=path)
        elif kind == "duplicate":
            code = R.DIAG_DUPLICATE_CODE_ID
            diag(code, f"{side} 内代码 id 重复: {payload}",
                 unit_id=uid, segment_id=sid, path=path)
        else:  # unbalanced
            code = R.DIAG_UNBALANCED_PC
            diag(code, f"{side} 内 pc 配对不平衡: {payload}",
                 unit_id=uid, segment_id=sid, path=path)
        seg_diags.append(code)

    # ---- 引用解析：dataRef / dataRefStart / dataRefEnd 只在本 unit 查找 ----
    for tok in parsed.tokens:
        if tok.type == PLACEHOLDER:
            _check_ref(tok, tok.ref, "dataRef", uid, sid, data_map,
                       diag, seg_diags, base)
        elif tok.type == OPEN:
            _check_ref(tok, tok.ref, "dataRefStart", uid, sid, data_map,
                       diag, seg_diags, base)
        elif tok.type == CLOSE:
            _check_ref(tok, tok.ref, "dataRefEnd", uid, sid, data_map,
                       diag, seg_diags, base)
    return parsed


def _check_ref(tok: Token, ref: Optional[str], attr: str, uid, sid, data_map,
               diag, seg_diags, base: str) -> None:
    if ref is None:
        diag(R.DIAG_MISSING_REF_ATTR,
             f"{base}: {tok.type} 代码 {tok.code_id} 缺少 {attr} 属性",
             unit_id=uid, segment_id=sid, path=tok.path)
        seg_diags.append(R.DIAG_MISSING_REF_ATTR)
        return
    entry = data_map.get(ref)
    if entry is None:
        diag(R.DIAG_MISSING_DATA_REFERENCE,
             f"{base}: 代码 {tok.code_id} 引用的 data id {ref} 在本 unit 不存在",
             unit_id=uid, segment_id=sid, path=tok.path)
        seg_diags.append(R.DIAG_MISSING_DATA_REFERENCE)
    elif entry["duplicate"]:
        # 重复 data id 要能定位到引用它的具体片段。
        diag(R.DIAG_DUPLICATE_DATA_ID,
             f"{base}: 代码 {tok.code_id} 引用了重复定义的 data id {ref}",
             unit_id=uid, segment_id=sid, path=tok.path)
        seg_diags.append(R.DIAG_DUPLICATE_DATA_ID)


def _check_cross_side(src: SideParse, tgt: SideParse, uid, sid, seg_path,
                      diag, seg_diags) -> None:
    def index(parsed: SideParse) -> dict[str, Token]:
        out: dict[str, Token] = {}
        for tok in parsed.code_tokens():
            # ph 一个 id 一个 token；pc 有 open/close 两个，open 优先。
            if tok.code_id is not None and tok.code_id not in out:
                out[tok.code_id] = tok
        return out

    def ref_map(parsed: SideParse) -> dict[str, tuple]:
        refs: dict[str, list[tuple[str, str]]] = {}
        for tok in parsed.code_tokens():
            if tok.code_id is None:
                continue
            attr = {
                PLACEHOLDER: "dataRef",
                OPEN: "dataRefStart",
                CLOSE: "dataRefEnd",
            }[tok.type]
            refs.setdefault(tok.code_id, []).append((attr, tok.ref))
        return {cid: tuple(sorted(v)) for cid, v in refs.items()}

    s_idx, t_idx = index(src), index(tgt)
    s_refs, t_refs = ref_map(src), ref_map(tgt)

    for cid in s_idx:
        if cid not in t_idx:
            diag(R.DIAG_TARGET_CODE_MISSING,
                 f"target 缺少 source 代码 id {cid}", unit_id=uid, segment_id=sid,
                 path=seg_path)
            seg_diags.append(R.DIAG_TARGET_CODE_MISSING)
            continue
        if s_idx[cid].type != t_idx[cid].type:
            diag(R.DIAG_CODE_KIND_MISMATCH,
                 f"代码 {cid} 类型不一致: source={s_idx[cid].type} "
                 f"target={t_idx[cid].type}",
                 unit_id=uid, segment_id=sid, path=seg_path)
            seg_diags.append(R.DIAG_CODE_KIND_MISMATCH)
        elif s_refs.get(cid) != t_refs.get(cid):
            diag(R.DIAG_CODE_REF_MISMATCH,
                 f"代码 {cid} 的 data 引用不一致: source={s_refs.get(cid)} "
                 f"target={t_refs.get(cid)}",
                 unit_id=uid, segment_id=sid, path=seg_path)
            seg_diags.append(R.DIAG_CODE_REF_MISMATCH)

    for cid in t_idx:
        if cid not in s_idx:
            diag(R.DIAG_TARGET_CODE_EXTRA,
                 f"target 出现 source 没有的代码 id {cid}",
                 unit_id=uid, segment_id=sid, path=seg_path)
            seg_diags.append(R.DIAG_TARGET_CODE_EXTRA)

    # ---- pc 后代代码集合必须不变（换序可以，移出/移入 pc 不行） ----
    s_cont = pc_containment(src.tokens)
    t_cont = pc_containment(tgt.tokens)
    for cid in s_cont:
        if cid in t_cont and s_cont[cid] != t_cont[cid]:
            diag(R.DIAG_PC_CONTAINMENT_MISMATCH,
                 f"pc {cid} 内代码集合变化: source={s_cont[cid]} target={t_cont[cid]}",
                 unit_id=uid, segment_id=sid, path=seg_path)
            seg_diags.append(R.DIAG_PC_CONTAINMENT_MISMATCH)


def _code_id_list(parsed: Optional[SideParse]) -> list[str]:
    if parsed is None:
        return []
    seen: list[str] = []
    for tok in parsed.code_tokens():
        if tok.code_id is not None and tok.code_id not in seen:
            seen.append(tok.code_id)
    return seen


def _side_record(parsed: SideParse) -> dict:
    tokens = [t.to_dict() for t in parsed.tokens]
    return {
        "tokens": tokens,
        "serialized": serialize_tokens(tokens),
        "pc_containment": pc_containment(tokens),
    }


def _build_pair(record: dict, code_table: dict) -> dict:
    uid = record["identity"]["unit_id"]
    sid = record["identity"]["segment_id"]
    source_ids = record["code_ids"]["source"]

    # 配对表自身带载荷回查：代码 id → 引用的 data id → 原始载荷。
    payloads: dict[str, dict] = {}
    for side_name in ("source", "target"):
        side = record[side_name]
        if side is None:
            continue
        for tok in side["tokens"]:
            if tok["type"] not in (PLACEHOLDER, OPEN, CLOSE):
                continue
            cid = tok.get("code_id")
            ref = tok.get("ref")
            entry = payloads.setdefault(cid, {"kind": tok["type"], "refs": {}})
            if ref is not None:
                data_entry = code_table.get(uid, {}).get(ref)
                entry["refs"][ref] = None if data_entry is None else data_entry["payload"]

    return {
        "pair_id": f"{uid}/{sid}",
        "identity": record["identity"],
        "state": record["state"],
        "source_tokens": record["source"]["tokens"],
        "target_tokens": record["target"]["tokens"],
        "source_serialized": record["source"]["serialized"],
        "target_serialized": record["target"]["serialized"],
        "code_ids": source_ids,
        "code_payloads": payloads,
    }
