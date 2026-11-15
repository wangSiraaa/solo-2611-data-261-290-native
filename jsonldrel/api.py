"""import 主流程：解析（只读原文）→ 范围检查 → 双路读取 → 核对 → 关系表。

流程：
1. 解析 JSON：拒绝重复键、NaN/Infinity（不静默）；
2. scope.py：语法层支持范围检查（远程 context、未声明关键字等 → 明确拒绝）；
3. walk.py（read B）：独立子集解释器，产出带 JSON Pointer 来源的关系记录；
4. libread.py（read A）：PyLD 标准展开（离线 loader，禁止任何远程访问）并归约；
5. canon.py：两路记录的空白节点无关核对，不一致即拒绝；
6. tables.py：落成 nodes/facts/list_members/expansions/diagnostics 关系表。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from . import canon, libread, scope, tables, walk
from .errors import Diagnostic, Reject


@dataclass
class ImportResult:
    ok: bool
    digest: str = ""
    diagnostics: list[Diagnostic] = field(default_factory=list)
    conn: Optional[sqlite3.Connection] = None
    counts: dict = field(default_factory=dict)

    def raise_if_failed(self) -> None:
        if not self.ok:
            msgs = "; ".join(f"[{d.code}] {d.message}" for d in self.diagnostics if d.severity == "error")
            raise Reject("IMPORT_FAILED", msgs or "导入失败")


def _loads_strict(text: str):
    def no_dupes(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise Reject("DUPLICATE_KEY", f"JSON 对象出现重复键 {k!r}（不静默覆盖）")
            out[k] = v
        return out

    def no_constant(x):
        raise Reject("NAN_INFINITY", f"JSON 含非有限数值 {x}（拒绝）")

    try:
        return json.loads(text, object_pairs_hook=no_dupes, parse_constant=no_constant)
    except Reject:
        raise
    except json.JSONDecodeError as e:
        raise Reject("JSON_PARSE", f"JSON 解析失败：{e}")


def digest_of(doc) -> str:
    canon_text = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon_text.encode("utf-8")).hexdigest()


def import_document(
    source: Union[str, Path],
    db_path: Optional[Union[str, Path]] = None,
) -> ImportResult:
    """导入离线 JSON-LD 文件。原文只读；被拒绝的文件产生带诊断的结果而非异常。"""
    path = Path(source)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return ImportResult(ok=False, diagnostics=[Diagnostic("error", "IO_ERROR", None, str(e))])

    diagnostics: list[Diagnostic] = []
    try:
        doc = _loads_strict(text)
        digest = digest_of(doc)
        scope.check_document(doc)
        walker = walk.Walker(digest[:8])
        records_b = walker.run(doc)
        diagnostics.extend(walker.notes)
        records_a = libread.records_from_pyld(doc)
        same, detail = canon.compare(records_a, records_b)
        if not same:
            diagnostics.append(
                Diagnostic("error", "CHECK_MISMATCH", None, f"双路读取核对失败：{detail}")
            )
            return ImportResult(ok=False, digest=digest, diagnostics=diagnostics)
        check_note = detail
    except Reject as r:
        diagnostics.append(r.to_diagnostic())
        return ImportResult(ok=False, diagnostics=diagnostics)

    conn = sqlite3.connect(str(db_path) if db_path else ":memory:")
    counts = {
        "nodes": len({n.node_id for n in records_b.nodes} | {l.list_id for l in records_b.lists}),
        "facts": len({(f.subject, f.predicate, f.obj) for f in records_b.facts}),
        "fact_occurrences": len(records_b.facts),
        "lists": len(records_b.lists),
        "list_members": sum(len(l.members) for l in records_b.lists),
        "node_objects": walker.node_count,
    }
    meta = {
        "source": path.name,
        "sha256": digest,
        "check": "passed",
        "check_detail": check_note,
        "max_nodes": str(walk.MAX_NODES),
        **{f"count_{k}": str(v) for k, v in counts.items()},
    }
    tables.build_tables(conn, records_b, diagnostics, meta)
    conn.commit()
    return ImportResult(ok=True, digest=digest, diagnostics=diagnostics, conn=conn, counts=counts)
