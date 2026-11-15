"""拒绝测试：未声明扩展、远程 context、非法构造都必须明确拒绝且不产关系表。"""

import pytest

V = "https://v.test/"


def expect_reject(load, doc, code):
    r = load(doc)
    assert not r.ok, f"应被拒绝但未拒绝：{doc!r}"
    codes = [d.code for d in r.diagnostics if d.severity == "error"]
    assert code in codes, f"期望诊断 {code}，实际 {codes}"
    assert r.conn is None
    return r


# ---------------------------------------------------------------- 远程/非内联 context

def test_remote_context_rejected(load):
    expect_reject(load, {"@context": "https://x.test/c", "a": 1}, "REMOTE_CONTEXT")


def test_nested_remote_context_rejected(load):
    doc = {"@context": {"@vocab": V}, "n": {"@context": "https://x.test/c", "a": 1}}
    expect_reject(load, doc, "REMOTE_CONTEXT")


def test_context_array_rejected(load):
    expect_reject(load, {"@context": [{"@vocab": V}], "a": 1}, "CONTEXT_NOT_OBJECT")


def test_context_null_reset_rejected(load):
    expect_reject(load, {"@context": None, "a": 1}, "CONTEXT_NOT_OBJECT")


# ---------------------------------------------------------------- 未声明关键字

@pytest.mark.parametrize("kw", ["@graph", "@included", "@nest", "@set", "@index"])
def test_unsupported_data_keywords(load, kw):
    doc = {"@context": {"@vocab": V}, kw: {} if kw != "@set" else []}
    expect_reject(load, doc, "DATA_KEYWORD_UNSUPPORTED")


@pytest.mark.parametrize("kw", ["@import", "@version", "@propagate", "@protected", "@direction"])
def test_unsupported_context_keywords(load, kw):
    val = "https://x.test/c" if kw == "@import" else (1.1 if kw == "@version" else True)
    expect_reject(load, {"@context": {kw: val, "@vocab": V}, "a": 1}, "CONTEXT_KEYWORD_UNSUPPORTED")


def test_unknown_at_key_rejected(load):
    expect_reject(load, {"@context": {"@vocab": V}, "@foo": 1}, "DATA_KEYWORD_UNSUPPORTED")


# ---------------------------------------------------------------- 词项定义越界

def test_container_list_rejected(load):
    doc = {"@context": {"@vocab": V, "l": {"@id": "l", "@container": "@list"}}, "l": [1]}
    expect_reject(load, doc, "CONTAINER_UNSUPPORTED")


def test_container_combination_rejected(load):
    doc = {
        "@context": {"@vocab": V, "l": {"@id": "l", "@container": ["@language", "@set"]}},
        "l": {"en": "x"},
    }
    expect_reject(load, doc, "CONTAINER_UNSUPPORTED")


def test_term_type_datatype_coercion_rejected(load):
    doc = {
        "@context": {"@vocab": V, "d": {"@id": "d", "@type": "https://x.test/Date"}},
        "d": "2024-01-01",
    }
    expect_reject(load, doc, "TERM_TYPE_UNSUPPORTED")


def test_term_reverse_rejected(load):
    doc = {"@context": {"@vocab": V, "r": {"@reverse": "p"}}, "r": {"@id": "https://x.test/a"}}
    expect_reject(load, doc, "TERM_DEF_KEY")


def test_keyword_alias_rejected(load):
    doc = {"@context": {"@vocab": V, "type": "@type"}, "type": "A"}
    expect_reject(load, doc, "TERM_ID_KEYWORD")


def test_scoped_context_in_term_rejected(load):
    doc = {"@context": {"@vocab": V, "n": {"@id": "n", "@context": {}}}, "n": 1}
    expect_reject(load, doc, "TERM_DEF_KEY")


# ---------------------------------------------------------------- 数据层面

def test_unmapped_property_rejected(load):
    expect_reject(load, {"@context": {}, "zzz": 1}, "UNMAPPED_PROPERTY")


def test_null_mapped_term_use_rejected(load):
    expect_reject(load, {"@context": {"@vocab": V, "gone": None}, "gone": 1}, "UNMAPPED_PROPERTY")


def test_null_value_rejected(load):
    expect_reject(load, {"@context": {"@vocab": V}, "a": None}, "NULL_VALUE")


def test_null_in_array_rejected(load):
    expect_reject(load, {"@context": {"@vocab": V}, "a": ["x", None]}, "NULL_VALUE")


def test_relative_id_without_base_rejected(load):
    expect_reject(load, {"@context": {"@vocab": V}, "@id": "rel/x", "a": 1}, "RELATIVE_ID_NO_BASE")


def test_relative_base_without_inherit_rejected(load):
    doc = {"@context": {"@base": "rel/", "@vocab": V}, "@id": "x", "a": 1}
    expect_reject(load, doc, "RELATIVE_BASE_NO_INHERIT")


def test_value_object_type_and_language_conflict(load):
    doc = {"@context": {"@vocab": V}, "p": {"@value": "x", "@type": "T", "@language": "en"}}
    expect_reject(load, doc, "VALUE_LANG_TYPE_CONFLICT")


def test_value_object_language_on_number(load):
    doc = {"@context": {"@vocab": V}, "p": {"@value": 5, "@language": "en"}}
    expect_reject(load, doc, "VALUE_LANG_NONSTRING")


def test_reverse_literal_rejected(load):
    doc = {"@context": {"@vocab": V}, "@reverse": {"p": "lit"}}
    expect_reject(load, doc, "REVERSE_VALUE_KIND")


def test_list_of_lists_rejected(load):
    doc = {"@context": {"@vocab": V}, "l": {"@list": [{"@list": [1]}]}}
    expect_reject(load, doc, "LIST_OF_LISTS")


def test_list_array_member_rejected(load):
    doc = {"@context": {"@vocab": V}, "l": {"@list": [[1, 2]]}}
    expect_reject(load, doc, "LIST_OF_LISTS")


def test_language_map_nonstring_rejected(load):
    doc = {
        "@context": {"@vocab": V, "lm": {"@id": "n", "@container": "@language"}},
        "lm": {"en": 5},
    }
    expect_reject(load, doc, "LANGMAP_VALUE_KIND")


def test_max_nodes_rejected(load):
    doc = {
        "@context": {"@vocab": V},
        "items": [{"n": i} for i in range(51)],
    }
    expect_reject(load, doc, "MAX_NODES")


def test_duplicate_json_keys_rejected(load):
    text = '{"@context": {"@vocab": "https://v.test/"}, "a": 1, "a": 2}'
    expect_reject(load, text, "DUPLICATE_KEY")


def test_nan_rejected(load):
    text = '{"@context": {"@vocab": "https://v.test/"}, "a": NaN}'
    expect_reject(load, text, "NAN_INFINITY")


def test_non_string_id_rejected(load):
    expect_reject(load, {"@context": {"@vocab": V}, "@id": 123}, "ID_KIND")


def test_root_value_object_rejected(load):
    expect_reject(load, {"@context": {"@vocab": V}, "@value": "x"}, "ROOT_NOT_NODE")


def test_vocab_not_absolute_rejected(load):
    expect_reject(load, {"@context": {"@vocab": "relative/"}, "a": 1}, "VOCAB_NOT_ABSOLUTE")


def test_reject_is_explicit_not_silent(load):
    """被拒绝时：有明确 error 诊断、无事实表产出。"""
    r = load({"@context": "https://x.test/c", "a": 1})
    assert not r.ok
    errors = [d for d in r.diagnostics if d.severity == "error"]
    assert errors and errors[0].pointer == "/@context"
    assert r.conn is None
