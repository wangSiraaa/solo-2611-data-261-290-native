"""子集语义测试：context 覆盖、属性重排、列表重复、反向边、类型强制、同 @id 归并等。"""

from jsonldrel import canon, walk
from jsonldrel.api import digest_of

from conftest import (
    LANGSTRING,
    RDF_TYPE,
    XSD_BOOLEAN,
    XSD_DOUBLE,
    XSD_INTEGER,
    XSD_STRING,
    facts,
)

V = "https://v.test/"
B = "https://base.test/"


def doc_with(ctx, **body):
    return {"@context": ctx, **body}


# ---------------------------------------------------------------- context 覆盖

def test_nested_context_overrides_term_and_no_sibling_leak(load):
    doc = doc_with(
        {"@vocab": V},
        parts=[
            {"@context": {"name": "https://local.test/n"}, "name": "内"},
            {"name": "外"},
        ],
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    preds = {f[1] for f in got}
    assert "https://local.test/n" in preds
    assert V + "name" in preds
    # 兄弟对象不受局部 context 泄漏影响：第二个 name 仍走 @vocab
    explain = {e["pointer"]: e for e in __import__("jsonldrel").Inspector(r.conn).explain("")}
    assert explain["/parts/0/name"]["via"] == "term"
    assert explain["/parts/0/name"]["context_pointer"] == "/parts/0/@context"
    assert explain["/parts/1/name"]["via"] == "vocab"
    assert explain["/parts/1/name"]["context_pointer"] == "/@context/@vocab"


def test_nested_base_and_vocab_reset(load):
    doc = doc_with(
        {"@base": B + "a/", "@vocab": V},
        **{"@id": "x"},
        inner={
            "@context": {"@base": "https://other.test/", "@vocab": None},
            "@id": "y",
            "https://abs.test/p": "v",
        },
    )
    r = load(doc)
    assert r.ok
    ids = {f[0] for f in facts(r.conn)} | {f[4] for f in facts(r.conn)}
    assert B + "a/x" in ids
    assert "https://other.test/y" in ids


def test_prefix_and_compact_iri(load):
    doc = doc_with({"ex": "https://ex.test/", "@vocab": V}, **{"ex:p": "v"})
    r = load(doc)
    assert r.ok
    assert (next(iter(facts(r.conn)))[1]) == "https://ex.test/p"


def test_language_container_and_default_language(load):
    doc = doc_with(
        {
            "@vocab": V,
            "@language": "DE",
            "labels": {"@id": "name", "@container": "@language"},
            "plain": {"@id": "plain", "@language": None},
        },
        labels={"EN-gb": ["a", "a"], "zh": "字"},
        plain="x",
        other="y",
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    subj = next(iter({f[0] for f in got}))
    # 语言标签小写化；重复值去重为一个事实、两个来源
    assert (subj, V + "name", "literal", None, "a", LANGSTRING, "en-gb", None) in got
    assert (subj, V + "name", "literal", None, "字", LANGSTRING, "zh", None) in got
    # 词项 @language: null 覆盖 context 默认语言
    assert (subj, V + "plain", "literal", None, "x", XSD_STRING, None, None) in got
    # context 默认语言作用于普通字符串
    assert (subj, V + "other", "literal", None, "y", LANGSTRING, "de", None) in got
    tea = r.conn.execute(
        "SELECT fact_id FROM facts WHERE lexical='a' AND language='en-gb'"
    ).fetchone()[0]
    srcs = [s[0] for s in r.conn.execute("SELECT pointer FROM fact_sources WHERE fact_id=?", (tea,))]
    assert sorted(srcs) == ["/labels/EN-gb/0", "/labels/EN-gb/1"]


# ---------------------------------------------------------------- 属性重排

def test_property_reorder_isomorphic(load):
    doc1 = {
        "@context": {"@base": B, "@vocab": V},
        "@id": "item",
        "a": "1",
        "b": {"n": "x"},
        "c": {"@list": [1, 2, 2]},
    }
    doc2 = {
        "c": {"@list": [1, 2, 2]},
        "b": {"n": "x"},
        "a": "1",
        "@id": "item",
        "@context": {"@vocab": V, "@base": B},
    }
    r1, r2 = load(doc1, "r1"), load(doc2, "r2")
    assert r1.ok and r2.ok
    # 不含空白节点的事实集合完全一致（列表/匿名节点标签允许不同）
    def concrete(conn):
        return {
            f for f in facts(conn)
            if not f[0].startswith("_:") and not (f[3] or "").startswith("_:")
        }
    assert concrete(r1.conn) == concrete(r2.conn)
    # 空白节点标签不要求一致，但两路记录必须结构同构
    w1 = walk.Walker(digest_of(doc1)[:8]).run(doc1)
    w2 = walk.Walker(digest_of(doc2)[:8]).run(doc2)
    same, _ = canon.compare(w1, w2)
    assert same


def test_blank_labels_stable_for_same_input(load):
    doc = doc_with({"@vocab": V}, p={"x": 1})
    r1, r2 = load(doc, "s1"), load(doc, "s2")
    assert r1.ok and r2.ok
    blanks1 = {f[0] for f in facts(r1.conn) if f[0].startswith("_:")}
    blanks2 = {f[0] for f in facts(r2.conn) if f[0].startswith("_:")}
    assert blanks1 and blanks1 == blanks2


# ---------------------------------------------------------------- 列表

def test_list_duplicates_order_and_empty(load):
    doc = doc_with(
        {"@vocab": V},
        steps={"@list": ["a", "b", "a", "a"]},
        empty={"@list": []},
    )
    r = load(doc)
    assert r.ok
    lists = {
        row[0]: row[1:]
        for row in r.conn.execute("SELECT list_id, pointer, member_count FROM lists")
    }
    by_ptr = {v[0]: k for k, v in lists.items()}
    steps = by_ptr["/steps"]
    empty = by_ptr["/empty"]
    got = [
        (m[0], m[3])
        for m in r.conn.execute(
            "SELECT idx, member_kind, member_iri, lexical FROM list_members WHERE list_id=? ORDER BY idx",
            (steps,),
        )
    ]
    assert got == [(0, "a"), (1, "b"), (2, "a"), (3, "a")]  # 重复成员不去重
    assert lists[empty][1] == 0  # 空列表明确存在
    assert r.conn.execute(
        "SELECT COUNT(*) FROM list_members WHERE list_id=?", (empty,)
    ).fetchone()[0] == 0


def test_list_independent_identity_same_content(load):
    """两处内容相同的 @list 是两个独立列表。"""
    doc = doc_with({"@vocab": V}, l1={"@list": [1, 2]}, l2={"@list": [1, 2]})
    r = load(doc)
    assert r.ok
    lids = [row[0] for row in r.conn.execute("SELECT list_id FROM lists")]
    assert len(lids) == 2 and lids[0] != lids[1]


def test_list_mixed_members(load):
    doc = doc_with(
        {"@vocab": V, "@base": B},
        l={"@list": [1, "s", {"@id": "x"}, {"n": "anon"}, True]},
    )
    r = load(doc)
    assert r.ok
    lid = r.conn.execute("SELECT list_id FROM lists").fetchone()[0]
    got = list(
        r.conn.execute(
            "SELECT idx, member_kind, member_iri, lexical, datatype FROM list_members"
            " WHERE list_id=? ORDER BY idx",
            (lid,),
        )
    )
    assert got[0][1:] == ("literal", None, "1", XSD_INTEGER)
    assert got[1][1:] == ("literal", None, "s", XSD_STRING)
    assert got[2][1:3] == ("iri", B + "x")
    assert got[3][1] == "iri" and got[3][2].startswith("_:")
    assert got[4][1:] == ("literal", None, "true", XSD_BOOLEAN)


# ---------------------------------------------------------------- 反向边

def test_reverse_edge_direction(load):
    doc = doc_with(
        {"@vocab": V, "@base": B},
        **{"@id": "me", "@reverse": {"knows": {"@id": "you"}}},
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    assert (B + "you", V + "knows", "iri", B + "me", None, None, None, None) in got
    assert not any(f[0] == B + "me" and f[1] == V + "knows" for f in got)


def test_reverse_array_and_nested_node(load):
    doc = doc_with(
        {"@vocab": V, "@base": B},
        **{
            "@id": "me",
            "@reverse": {"p": [{"@id": "a"}, {"@id": "b", "n": "x"}]},
        },
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    assert (B + "a", V + "p", "iri", B + "me", None, None, None, None) in got
    assert (B + "b", V + "p", "iri", B + "me", None, None, None, None) in got
    assert (B + "b", V + "n", "literal", None, "x", XSD_STRING, None, None) in got


def test_double_reverse_stays_nested(load):
    """@reverse 值节点内部的 @reverse 不再翻转回当前节点（与 PyLD 展开树一致）。"""
    doc = doc_with(
        {"@vocab": V, "@base": B},
        **{
            "@id": "me",
            "@reverse": {"p": {"@id": "x", "@reverse": {"q": {"@id": "y"}}}},
        },
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    assert (B + "x", V + "p", "iri", B + "me", None, None, None, None) in got
    assert (B + "y", V + "q", "iri", B + "x", None, None, None, None) in got
    assert not any(f[0] == B + "me" and f[1] == V + "q" for f in got)


# ---------------------------------------------------------------- 类型强制与 URL 形文字

def test_id_coercion_vs_url_like_literal(load):
    doc = doc_with(
        {"@vocab": V, "@base": B, "link": {"@id": "see", "@type": "@id"}},
        link="https://elsewhere.test/x",
        note="https://elsewhere.test/x",
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    subj = next(iter({f[0] for f in got}))
    # 强制 @id：成为 IRI 对象
    assert (subj, V + "see", "iri", "https://elsewhere.test/x", None, None, None, None) in got
    # 同形字符串但无强制：仍是字面量，不得当成 IRI
    assert (
        subj, V + "note", "literal", None,
        "https://elsewhere.test/x", XSD_STRING, None, None,
    ) in got


def test_typed_value_object(load):
    doc = doc_with(
        {"@vocab": V},
        d={"@value": "2024-01-01", "@type": "Date"},
        n={"@value": 7, "@type": "https://num.test/T"},
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    subj = next(iter({f[0] for f in got}))
    assert (subj, V + "d", "literal", None, "2024-01-01", V + "Date", None, None) in got
    assert (subj, V + "n", "literal", None, "7", "https://num.test/T", None, "7") in got


# ---------------------------------------------------------------- 数字不字符串化猜型

def test_numbers_keep_native_types(load):
    doc = doc_with(
        {"@vocab": V},
        i=5,
        f=1.5,
        b=True,
        big=123456789012345678901234567890,
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    subj = next(iter({f[0] for f in got}))
    assert (subj, V + "i", "literal", None, "5", XSD_INTEGER, None, "5") in got
    assert (subj, V + "f", "literal", None, "1.5", XSD_DOUBLE, None, "1.5") in got
    assert (subj, V + "b", "literal", None, "true", XSD_BOOLEAN, None, "true") in got
    assert (
        subj, V + "big", "literal", None,
        "123456789012345678901234567890", XSD_INTEGER, None, "123456789012345678901234567890",
    ) in got


# ---------------------------------------------------------------- 同 @id 归并

def test_same_id_merges_with_provenance(load):
    doc = doc_with(
        {"@vocab": V, "@base": B},
        first={"@id": "shared", "p": "one"},
        second={"@id": "shared", "q": "two"},
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    shared = B + "shared"
    assert (shared, V + "p", "literal", None, "one", XSD_STRING, None, None) in got
    assert (shared, V + "q", "literal", None, "two", XSD_STRING, None, None) in got
    row = r.conn.execute(
        "SELECT occurrences FROM nodes WHERE node_id=?", (shared,)
    ).fetchone()
    assert row[0] == 2
    srcs = sorted(
        s[0]
        for s in r.conn.execute(
            "SELECT pointer FROM node_sources WHERE node_id=?", (shared,)
        )
    )
    assert srcs == ["/first", "/second"]


def test_user_blank_id_preserved_and_merged(load):
    doc = doc_with(
        {"@vocab": V},
        a={"@id": "_:mine", "p": 1},
        b={"@id": "_:mine", "q": 2},
    )
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    assert ("_:mine", V + "p", "literal", None, "1", XSD_INTEGER, None, "1") in got
    assert ("_:mine", V + "q", "literal", None, "2", XSD_INTEGER, None, "2") in got


# ---------------------------------------------------------------- 其他

def test_type_edges_explicit(load):
    doc = doc_with({"@vocab": V}, **{"@type": ["A", "https://t.test/B"]})
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    subj = next(iter({f[0] for f in got}))
    assert (subj, RDF_TYPE, "iri", V + "A", None, None, None, None) in got
    assert (subj, RDF_TYPE, "iri", "https://t.test/B", None, None, None, None) in got


def test_multi_value_array_dedup_set(load):
    doc = doc_with({"@vocab": V}, p=["x", "x", "y"])
    r = load(doc)
    assert r.ok
    got = facts(r.conn)
    lit_x = [f for f in got if f[4] == "x"]
    assert len(lit_x) == 1  # 同一展开事实只保留一次
    fid = r.conn.execute("SELECT fact_id FROM facts WHERE lexical='x'").fetchone()[0]
    srcs = sorted(s[0] for s in r.conn.execute("SELECT pointer FROM fact_sources WHERE fact_id=?", (fid,)))
    assert srcs == ["/p/0", "/p/1"]  # 来源出现不丢


def test_root_id_only_dropped_with_note(load):
    doc = {"@context": {"@vocab": V}, "@id": "https://x.test/a"}
    r = load(doc)
    assert r.ok
    assert r.counts["facts"] == 0
    assert any(d.code == "ROOT_NODE_DROPPED" for d in r.diagnostics)


def test_anonymous_node_blank_identity(load):
    doc = doc_with({"@vocab": V}, p={"n": "x"}, q={"n": "x"})
    r = load(doc)
    assert r.ok
    blanks = {f[3] for f in facts(r.conn) if f[2] == "iri" and (f[3] or "").startswith("_:")}
    assert len(blanks) == 2  # 两个匿名节点互不相同
