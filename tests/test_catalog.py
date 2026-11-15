"""fixtures 测试：catalog.jsonld 全量核对 + bad-remote.jsonld 拒绝。"""

from pathlib import Path

from jsonldrel import Inspector, import_document

from conftest import (
    LANGSTRING,
    RDF_TYPE,
    XSD_STRING,
    fact_sources,
    facts,
    members,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

A = "https://data.test/items/a"
B = "https://data.test/items/b"
C = "https://data.test/items/c"
P = "https://data.test/parts/p"
Q = "https://data.test/parts/q"
S = "https://schema.test/"


def load_catalog():
    return import_document(FIXTURES / "catalog.jsonld")


def test_catalog_ok():
    r = load_catalog()
    assert r.ok, [d.message for d in r.diagnostics]
    assert r.counts["facts"] == 11
    assert r.counts["fact_occurrences"] == 12  # Tea/Tea 两次出现归并为一条事实
    assert r.counts["lists"] == 2
    assert r.counts["list_members"] == 3


def test_catalog_facts():
    r = load_catalog()
    assert r.ok
    got = facts(r.conn)
    want = {
        (A, RDF_TYPE, "iri", S + "Product", None, None, None, None),
        (A, S + "name", "literal", None, "Tea", LANGSTRING, "en", None),
        (A, S + "name", "literal", None, "茶", LANGSTRING, "zh", None),
        (A, S + "related", "iri", B, None, None, None, None),
        (A, S + "parts", "iri", P, None, None, None, None),
        (A, S + "parts", "iri", Q, None, None, None, None),
        (P, "https://local.test/name", "literal", None, "局部名", XSD_STRING, None, None),
        (Q, S + "name", "literal", None, "默认名", XSD_STRING, None, None),
        # @reverse：边方向为 items/c -> related -> items/a
        (C, S + "related", "iri", A, None, None, None, None),
    }
    assert want <= got
    # 两条列表事实
    list_facts = [f for f in got if f[2] == "list"]
    assert len(list_facts) == 2
    assert {f[1] for f in list_facts} == {S + "steps", S + "empty"}


def test_catalog_duplicate_literal_set_semantics():
    """语言映射中的重复值 Tea/Tea：事实集合保留一次，来源保留两次。"""
    r = load_catalog()
    assert r.ok
    src = fact_sources(r.conn)
    tea_fids = [
        fid
        for fid, in r.conn.execute(
            "SELECT fact_id FROM facts WHERE lexical = 'Tea' AND language = 'en'"
        )
    ]
    assert len(tea_fids) == 1
    assert sorted(src[tea_fids[0]]) == ["/labels/en/0", "/labels/en/1"]


def test_catalog_lists():
    r = load_catalog()
    assert r.ok
    lists = {
        row[0]: row[1:]
        for row in r.conn.execute("SELECT list_id, pointer, member_count FROM lists")
    }
    by_ptr = {v[0]: (k, v[1]) for k, v in lists.items()}
    steps_id, steps_n = by_ptr["/steps"]
    empty_id, empty_n = by_ptr["/empty"]
    assert steps_n == 3 and empty_n == 0
    # 重复成员 heat 不去重，下标从零开始
    assert [m[3] for m in members(r.conn, steps_id)] == ["wash", "heat", "heat"]
    assert [m[0] for m in members(r.conn, steps_id)] == [0, 1, 2]
    # 空列表明确存在且无成员
    assert members(r.conn, empty_id) == []


def test_catalog_context_override_no_leak():
    """局部 context 覆盖 name，且不向兄弟对象泄漏。"""
    r = load_catalog()
    assert r.ok
    insp = Inspector(r.conn)
    e0 = insp.explain("/parts/0/name")[0]
    e1 = insp.explain("/parts/1/name")[0]
    assert e0["expanded"] == "https://local.test/name"
    assert e0["via"] == "term"
    assert e0["context_pointer"] == "/parts/0/@context"
    assert e1["expanded"] == "https://schema.test/name"
    assert e1["via"] == "vocab"
    assert e1["context_pointer"] == "/@context/@vocab"


def test_catalog_pointer_map():
    r = load_catalog()
    assert r.ok
    insp = Inspector(r.conn)
    pm = insp.pointer_map()
    # 反向边的来源指针指向 @reverse 映射中的值
    rev_targets = pm["/@reverse/related"]
    assert any(t["target"] == "fact" for t in rev_targets)
    assert any(t["target"] == "node" for t in rev_targets)
    # 列表成员指针
    assert any(t["target"] == "list_member" for t in pm["/steps/@list/2"])


def test_catalog_same_entity_provenance():
    """parts/p 的节点来源记录在案。"""
    r = load_catalog()
    assert r.ok
    insp = Inspector(r.conn)
    ent = insp.entity(P)
    assert ent["occurrences"] == 1
    assert ent["sources"][0]["pointer"] == "/parts/0"
    # items/c 仅作为 @reverse 值出现一次
    assert insp.entity(C)["sources"][0]["pointer"] == "/@reverse/related"


def test_bad_remote_rejected():
    r = import_document(FIXTURES / "bad-remote.jsonld")
    assert not r.ok
    assert any(d.code == "REMOTE_CONTEXT" for d in r.diagnostics)
    assert r.conn is None  # 拒绝时不产关系表
