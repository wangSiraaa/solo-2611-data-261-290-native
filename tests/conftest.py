"""共享测试工具：写临时文件并导入。"""

import json
from pathlib import Path

import pytest

from jsonldrel import import_document


@pytest.fixture
def load(tmp_path):
    """load(doc_or_text, name='x') -> ImportResult"""
    counter = {"n": 0}

    def _load(doc, name=None):
        counter["n"] += 1
        fname = name or f"doc{counter['n']}"
        path = Path(tmp_path) / f"{fname}.jsonld"
        if isinstance(doc, str):
            path.write_text(doc, encoding="utf-8")
        else:
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return import_document(path)

    return _load


def facts(conn):
    """facts 表内容（不含 fact_id）。"""
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT subject, predicate, object_kind, object_iri, lexical, datatype,"
            " language, native_json FROM facts"
        )
    }


def fact_sources(conn):
    out = {}
    for fid, ptr in conn.execute("SELECT fact_id, pointer FROM fact_sources"):
        out.setdefault(fid, []).append(ptr)
    return out


def members(conn, list_id):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT idx, member_kind, member_iri, lexical, datatype, language"
            " FROM list_members WHERE list_id = ? ORDER BY idx",
            (list_id,),
        )
    ]


RDF_TYPE = "http://www.w3.org/1999/02/22/07-rdf-syntax-ns#type"
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
XSD_DOUBLE = "http://www.w3.org/2001/XMLSchema#double"
XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"
LANGSTRING = "http://www.w3.org/1999/02/22/07-rdf-syntax-ns#langString"
