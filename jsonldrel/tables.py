"""关系表设计与落库（SQLite）。

表结构：
- meta(key, value)                       输入摘要、计数、核对结论
- nodes(node_id PK, kind, occurrences)   实体表：IRI / 空白 / 列表头
- node_sources(node_id, pointer, role)   实体来源（同一 @id 多处描述不丢来源）
- facts(fact_id PK, subject, predicate, object_kind,
        object_iri, lexical, datatype, language, native_json)
                                         事实集合：同一展开事实只保留一行
- fact_sources(fact_id, pointer)         事实来源（多重：同一事实的每次出现）
- lists(list_id PK, pointer, member_count)
                                         列表身份：每次 @list 出现都是独立列表，空列表明确存在
- list_members(list_id, idx, member_kind, member_iri,
               lexical, datatype, language, native_json, pointer, PK(list_id, idx))
                                         成员顺序从零开始，重复成员不去重
- expansions(kind, pointer, raw, expanded, via, context_pointer)
                                         键/值 → IRI 的解析记录（解释 name 为何成为不同 predicate）
- diagnostics(seq PK, severity, code, pointer, message)

事实集合的“唯一性”在 Python 侧按键去重（SQLite 的 UNIQUE 对 NULL 不等价），
落库后再用 GROUP BY 自检一次，确保集合口径成立。
"""

from __future__ import annotations

import sqlite3

from .errors import Diagnostic
from .model import Records, node_kind

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE nodes (
  node_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('iri','blank','list')),
  occurrences INTEGER NOT NULL
);
CREATE TABLE node_sources (
  node_id TEXT NOT NULL REFERENCES nodes(node_id),
  pointer TEXT NOT NULL,
  role TEXT NOT NULL
);
CREATE TABLE facts (
  fact_id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_kind TEXT NOT NULL CHECK (object_kind IN ('iri','literal','list')),
  object_iri TEXT,
  lexical TEXT,
  datatype TEXT,
  language TEXT,
  native_json TEXT
);
CREATE TABLE fact_sources (
  fact_id INTEGER NOT NULL REFERENCES facts(fact_id),
  pointer TEXT NOT NULL
);
CREATE TABLE lists (
  list_id TEXT PRIMARY KEY,
  pointer TEXT NOT NULL,
  member_count INTEGER NOT NULL
);
CREATE TABLE list_members (
  list_id TEXT NOT NULL REFERENCES lists(list_id),
  idx INTEGER NOT NULL,
  member_kind TEXT NOT NULL CHECK (member_kind IN ('iri','literal')),
  member_iri TEXT,
  lexical TEXT,
  datatype TEXT,
  language TEXT,
  native_json TEXT,
  pointer TEXT NOT NULL,
  PRIMARY KEY (list_id, idx)
);
CREATE TABLE expansions (
  kind TEXT NOT NULL CHECK (kind IN ('predicate','type','id')),
  pointer TEXT NOT NULL,
  raw TEXT NOT NULL,
  expanded TEXT NOT NULL,
  via TEXT NOT NULL,
  context_pointer TEXT
);
CREATE TABLE diagnostics (
  seq INTEGER PRIMARY KEY,
  severity TEXT NOT NULL,
  code TEXT NOT NULL,
  pointer TEXT,
  message TEXT NOT NULL
);
CREATE INDEX idx_facts_subject ON facts(subject);
CREATE INDEX idx_facts_object ON facts(object_iri);
CREATE INDEX idx_fact_sources_pointer ON fact_sources(pointer);
CREATE INDEX idx_node_sources_pointer ON node_sources(pointer);
"""

_OBJ_COLS = ("object_kind", "object_iri", "lexical", "datatype", "language", "native_json")


def _obj_columns(obj: tuple) -> tuple:
    """记录元组 → facts/list_members 列。"""
    kind = obj[0]
    if kind in ("iri", "list"):
        return ("iri" if kind == "iri" else "list", obj[1], None, None, None, None)
    # ('lit', datatype, language, lexical, native_json)
    return ("literal", None, obj[3], obj[1], obj[2], obj[4])


def build_tables(
    conn: sqlite3.Connection,
    records: Records,
    diagnostics: list[Diagnostic],
    meta: dict[str, str],
) -> None:
    conn.executescript(SCHEMA)

    # ---- facts：集合去重，来源多重保留
    fact_ids: dict[tuple, int] = {}
    for f in records.facts:
        key = (f.subject, f.predicate) + f.obj
        fid = fact_ids.get(key)
        if fid is None:
            cols = _obj_columns(f.obj)
            cur = conn.execute(
                "INSERT INTO facts(subject, predicate, object_kind, object_iri, lexical,"
                " datatype, language, native_json) VALUES (?,?,?,?,?,?,?,?)",
                (f.subject, f.predicate) + cols,
            )
            fid = cur.lastrowid
            fact_ids[key] = fid
        if f.pointer is not None:
            conn.execute("INSERT INTO fact_sources(fact_id, pointer) VALUES (?,?)", (fid, f.pointer))

    # ---- lists：每次出现独立身份；成员保序、重复保留
    for lst in records.lists:
        conn.execute(
            "INSERT INTO lists(list_id, pointer, member_count) VALUES (?,?,?)",
            (lst.list_id, lst.pointer, len(lst.members)),
        )
        for idx, (member, mptr) in enumerate(lst.members):
            kind, iri, lexical, dt, lang, native = _obj_columns(member)
            conn.execute(
                "INSERT INTO list_members(list_id, idx, member_kind, member_iri, lexical,"
                " datatype, language, native_json, pointer) VALUES (?,?,?,?,?,?,?,?,?)",
                (lst.list_id, idx, kind, iri, lexical, dt, lang, native, mptr),
            )

    # ---- nodes：出现归并到同一实体，来源逐条保留
    occurrence_count: dict[str, int] = {}
    kinds: dict[str, str] = {}
    for occ in records.nodes:
        kinds.setdefault(occ.node_id, occ.kind)
        occurrence_count[occ.node_id] = occurrence_count.get(occ.node_id, 0) + 1
    # 列表头也登记为节点（kind='list'）
    for lst in records.lists:
        kinds.setdefault(lst.list_id, "list")
        occurrence_count.setdefault(lst.list_id, 0)
    # 事实中出现但未登记出现的实体（防御：理论上 read B 都会登记）
    for (subject, _pred, obj) in ((f.subject, f.predicate, f.obj) for f in records.facts):
        kinds.setdefault(subject, node_kind(subject))
        occurrence_count.setdefault(subject, 0)
        if obj[0] in ("iri", "list"):
            kinds.setdefault(obj[1], "list" if obj[0] == "list" else node_kind(obj[1]))
            occurrence_count.setdefault(obj[1], 0)
    for nid, kind in sorted(kinds.items()):
        conn.execute(
            "INSERT INTO nodes(node_id, kind, occurrences) VALUES (?,?,?)",
            (nid, kind, occurrence_count[nid]),
        )
    for occ in records.nodes:
        if occ.pointer is not None:
            conn.execute(
                "INSERT INTO node_sources(node_id, pointer, role) VALUES (?,?,?)",
                (occ.node_id, occ.pointer, occ.role or ""),
            )

    # ---- 词项/值解析记录
    for e in records.expansions:
        conn.execute(
            "INSERT INTO expansions(kind, pointer, raw, expanded, via, context_pointer)"
            " VALUES (?,?,?,?,?,?)",
            (e.kind, e.pointer, e.raw, e.expanded, e.via, e.context_pointer),
        )

    # ---- 诊断
    for d in diagnostics:
        conn.execute(
            "INSERT INTO diagnostics(severity, code, pointer, message) VALUES (?,?,?,?)",
            (d.severity, d.code, d.pointer, d.message),
        )

    for k, v in meta.items():
        conn.execute("INSERT INTO meta(key, value) VALUES (?,?)", (k, v))

    # ---- 集合口径自检：事实键不得有重复
    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT subject, predicate, object_kind,"
        " ifnull(object_iri,''), ifnull(lexical,''), ifnull(datatype,''),"
        " ifnull(language,''), ifnull(native_json,'') AS n"
        " FROM facts GROUP BY 1,2,3,4,5,6,7,8 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if dup:
        raise AssertionError(f"facts 集合口径被破坏：{dup} 组重复")
    # 列表成员下标连续且从零开始
    bad = conn.execute(
        "SELECT l.list_id FROM lists l WHERE l.member_count <>"
        " (SELECT COUNT(*) FROM list_members m WHERE m.list_id = l.list_id)"
    ).fetchall()
    if bad:
        raise AssertionError(f"lists.member_count 与成员行数不一致：{bad}")
