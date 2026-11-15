"""inspect：对导入产物的查询 API（关系表 + 来源映射 + 词项解析解释）。"""

from __future__ import annotations

import sqlite3
from typing import Optional

FACT_COLS = "fact_id, subject, predicate, object_kind, object_iri, lexical, datatype, language, native_json"


def _fact_dict(row) -> dict:
    keys = ("fact_id", "subject", "predicate", "object_kind", "object_iri",
            "lexical", "datatype", "language", "native_json")
    return dict(zip(keys, row))


def _under(pointer: str, prefix: str) -> bool:
    """JSON Pointer 前缀包含：prefix='' 表示根，匹配一切。"""
    if prefix == "":
        return True
    return pointer == prefix or pointer.startswith(prefix + "/")


class Inspector:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------ 总览

    def summary(self) -> dict:
        meta = dict(self.conn.execute("SELECT key, value FROM meta"))
        counts = {
            t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("nodes", "facts", "fact_sources", "lists", "list_members", "expansions")
        }
        return {"meta": meta, "counts": counts}

    def diagnostics(self) -> list[dict]:
        return [
            {"severity": s, "code": c, "pointer": p, "message": m}
            for s, c, p, m in self.conn.execute(
                "SELECT severity, code, pointer, message FROM diagnostics ORDER BY seq"
            )
        ]

    # ------------------------------------------------------------ 实体

    def entity(self, node_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT node_id, kind, occurrences FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if not row:
            return None
        sources = [
            {"pointer": p, "role": r}
            for p, r in self.conn.execute(
                "SELECT pointer, role FROM node_sources WHERE node_id = ? ORDER BY pointer", (node_id,)
            )
        ]
        outgoing = [
            _fact_dict(r)
            for r in self.conn.execute(f"SELECT {FACT_COLS} FROM facts WHERE subject = ?", (node_id,))
        ]
        incoming = [
            _fact_dict(r)
            for r in self.conn.execute(
                f"SELECT {FACT_COLS} FROM facts WHERE object_iri = ? AND object_kind IN ('iri','list')",
                (node_id,),
            )
        ]
        lists = [
            {"list_id": l, "pointer": p, "member_count": c}
            for l, p, c in self.conn.execute(
                "SELECT list_id, pointer, member_count FROM lists WHERE list_id = ?", (node_id,)
            )
        ]
        return {
            "node_id": row[0],
            "kind": row[1],
            "occurrences": row[2],
            "sources": sources,
            "outgoing": outgoing,
            "incoming": incoming,
            "lists": lists,
        }

    # ------------------------------------------------------------ 事实 / 列表

    def fact_sources(self, fact_id: int) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT pointer FROM fact_sources WHERE fact_id = ? ORDER BY pointer", (fact_id,)
            )
        ]

    def facts_under(self, pointer_prefix: str) -> list[dict]:
        """来源指针位于 pointer_prefix 之下的事实（含各自全部来源）。"""
        out = []
        for row in self.conn.execute(f"SELECT {FACT_COLS} FROM facts"):
            fact = _fact_dict(row)
            ptrs = self.fact_sources(fact["fact_id"])
            if any(_under(p, pointer_prefix) for p in ptrs):
                fact["sources"] = ptrs
                out.append(fact)
        return out

    def list_members(self, list_id: str) -> list[dict]:
        return [
            {
                "idx": i, "member_kind": k, "member_iri": mi, "lexical": lex,
                "datatype": dt, "language": lg, "native_json": nj, "pointer": p,
            }
            for i, k, mi, lex, dt, lg, nj, p in self.conn.execute(
                "SELECT idx, member_kind, member_iri, lexical, datatype, language,"
                " native_json, pointer FROM list_members WHERE list_id = ? ORDER BY idx",
                (list_id,),
            )
        ]

    def lists(self) -> list[dict]:
        return [
            {"list_id": l, "pointer": p, "member_count": c}
            for l, p, c in self.conn.execute("SELECT list_id, pointer, member_count FROM lists")
        ]

    # ------------------------------------------------------------ 来源映射

    def pointer_map(self) -> dict[str, list[dict]]:
        """源 JSON Pointer → 所产事实/列表成员/节点/列表 的完整映射。"""
        out: dict[str, list[dict]] = {}

        def add(ptr: str, target: dict):
            out.setdefault(ptr, []).append(target)

        for fid, ptr in self.conn.execute("SELECT fact_id, pointer FROM fact_sources"):
            add(ptr, {"target": "fact", "fact_id": fid})
        for lid, idx, ptr in self.conn.execute("SELECT list_id, idx, pointer FROM list_members"):
            add(ptr, {"target": "list_member", "list_id": lid, "idx": idx})
        for nid, ptr, role in self.conn.execute("SELECT node_id, pointer, role FROM node_sources"):
            add(ptr, {"target": "node", "node_id": nid, "role": role})
        for lid, ptr in self.conn.execute("SELECT list_id, pointer FROM lists"):
            add(ptr, {"target": "list", "list_id": lid})
        return dict(sorted(out.items()))

    # ------------------------------------------------------------ 词项解析解释

    def explain(self, pointer_prefix: str) -> list[dict]:
        """解释 pointer 处的键/值为何展开成该 IRI（含生效 context 的来源指针）。

        例：同一个 name 键，在 /parts/0 下由局部 context 的词项映射解释，
        在 /parts/1 下由根 context 的 @vocab 拼接解释——两者成为不同 predicate。
        """
        rows = self.conn.execute(
            "SELECT kind, pointer, raw, expanded, via, context_pointer FROM expansions ORDER BY pointer"
        ).fetchall()
        out = []
        for kind, ptr, raw, expanded, via, ctx_ptr in rows:
            if not _under(ptr, pointer_prefix):
                continue
            out.append(
                {
                    "kind": kind,
                    "pointer": ptr,
                    "raw": raw,
                    "expanded": expanded,
                    "via": via,
                    "context_pointer": ctx_ptr,
                    "explanation": _explain_text(kind, raw, expanded, via, ctx_ptr),
                }
            )
        return out


_VIA_TEXT = {
    "term": "由该处生效 context 的词项映射定义",
    "prefix": "由该处生效 context 的前缀词项拼接",
    "vocab": "由该处生效 context 的 @vocab 拼接",
    "absolute": "本身即绝对 IRI/空白标识，原样使用",
    "base": "按该处生效的 @base 解析",
    "keyword": "JSON-LD 关键字",
    "blank": "输入给定的空白节点标识",
}


def _explain_text(kind, raw, expanded, via, ctx_ptr) -> str:
    how = _VIA_TEXT.get(via, via)
    where = f"（context 来源：{ctx_ptr}）" if ctx_ptr else ""
    kind_text = {"predicate": "属性", "type": "@type 值", "id": "@id 值"}.get(kind, kind)
    return f"{kind_text} {raw!r} → {expanded}：{how}{where}"
