"""canon.py 单元测试：空白节点无关比较的正确性。"""

from jsonldrel import canon, walk
from jsonldrel.api import digest_of
from jsonldrel.model import FactOcc, ListRec, Records

V = "https://v.test/"


def recs(doc):
    return walk.Walker(digest_of(doc)[:8]).run(doc)


def test_isomorphic_blank_labels_match():
    d1 = {"@context": {"@vocab": V}, "p": {"n": "x"}}
    d2 = {"@context": {"@vocab": V}, "p": {"n": "x"}}
    same, _ = canon.compare(recs(d1), recs(d2))
    assert same


def test_list_order_swap_mismatch():
    d1 = {"@context": {"@vocab": V}, "l": {"@list": ["a", "b"]}}
    d2 = {"@context": {"@vocab": V}, "l": {"@list": ["b", "a"]}}
    same, detail = canon.compare(recs(d1), recs(d2))
    assert not same, detail


def test_list_duplicate_removal_mismatch():
    d1 = {"@context": {"@vocab": V}, "l": {"@list": ["a", "a"]}}
    d2 = {"@context": {"@vocab": V}, "l": {"@list": ["a"]}}
    same, _ = canon.compare(recs(d1), recs(d2))
    assert not same


def test_reverse_direction_mismatch():
    d1 = {"@context": {"@vocab": V, "@base": "https://b.test/"},
          "@id": "me", "@reverse": {"p": {"@id": "you"}}}
    d2 = {"@context": {"@vocab": V, "@base": "https://b.test/"},
          "@id": "me", "p": {"@id": "you"}}
    same, _ = canon.compare(recs(d1), recs(d2))
    assert not same  # 正反向边不得混淆


def test_literal_datatype_mismatch():
    d1 = {"@context": {"@vocab": V}, "p": 5}
    d2 = {"@context": {"@vocab": V}, "p": "5"}
    same, _ = canon.compare(recs(d1), recs(d2))
    assert not same  # 数字 5 与字符串 "5" 不得混淆


def test_empty_list_vs_no_list_mismatch():
    d1 = {"@context": {"@vocab": V}, "l": {"@list": []}}
    d2 = {"@context": {"@vocab": V}, "p": "x"}
    same, _ = canon.compare(recs(d1), recs(d2))
    assert not same


def test_manual_records_compare():
    """直接构造 Records：同构应通过，不同构应失败。"""
    r1 = Records(
        facts=[FactOcc("_:b0", V + "p", ("iri", "_:b1")), FactOcc("_:b1", V + "q", ("iri", "https://x.test/a"))],
        lists=[ListRec("_:l0", None, [(("lit", "http://www.w3.org/2001/XMLSchema#string", None, "a", None), None)])],
    )
    r2 = Records(
        facts=[FactOcc("_:x9", V + "p", ("iri", "_:x8")), FactOcc("_:x8", V + "q", ("iri", "https://x.test/a"))],
        lists=[ListRec("_:l9", None, [(("lit", "http://www.w3.org/2001/XMLSchema#string", None, "a", None), None)])],
    )
    # r1 与 r2 结构相同（仅标签不同），但 r1 的列表未挂到任何节点——两边都一样，应通过
    same, _ = canon.compare(r1, r2)
    assert same

    r3 = Records(
        facts=[FactOcc("_:x9", V + "p", ("iri", "_:x8")), FactOcc("_:x8", V + "q", ("iri", "https://x.test/a"))],
        lists=[ListRec("_:l9", None, [(("lit", "http://www.w3.org/2001/XMLSchema#string", None, "b", None), None)])],
    )
    same, _ = canon.compare(r1, r3)
    assert not same  # 列表成员词法不同
