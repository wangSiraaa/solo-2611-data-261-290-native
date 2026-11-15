"""共享数据模型：上下文、值描述符、关系记录、JSON Pointer 与 IRI 工具。

read A（PyLD 展开结果）与 read B（独立子集解释器）都归约到本模块定义的记录形状，
再由 canon.py 做空白节点无关的核对。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------- 常量

RDF_TYPE = "http://www.w3.org/1999/02/22/07-rdf-syntax-ns#type"
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
XSD_DOUBLE = "http://www.w3.org/2001/XMLSchema#double"
RDF_LANGSTRING = "http://www.w3.org/1999/02/22/07-rdf-syntax-ns#langString"

MAX_NODES = 50

# 与 PyLD 保持一致的两个正则
KEYWORD_PATTERN = re.compile(r"^@[a-zA-Z]+$")
ABSOLUTE_IRI_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9+-.]*|_):[^\s]*$")
# PyLD 中 “term 形如 IRI” 的判定（含 :非: 或 /）
TERM_LOOKS_LIKE_IRI = re.compile(r".*((:[^:])|/)")
# 前缀映射要求 @id 以 gen-delim 结尾
GEN_DELIM_END = re.compile(r".*[:/\?#\[\]@]$")

DATA_KEYWORDS = {"@context", "@id", "@type", "@value", "@language", "@list", "@reverse"}
CONTEXT_META_KEYS = {"@base", "@vocab", "@language"}
TERM_DEF_KEYS = {"@id", "@type", "@language", "@container"}


def is_absolute_iri(value: Any) -> bool:
    return isinstance(value, str) and bool(ABSOLUTE_IRI_PATTERN.match(value))


def is_blank_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("_:")


# ---------------------------------------------------------------- JSON Pointer

def ptr_escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def ptr_join(base: str, segment) -> str:
    return base + "/" + ptr_escape(str(segment))


# ---------------------------------------------------------------- 上下文模型

_MISSING = object()  # 区分“未设置”与“显式 null”


@dataclass
class TermDef:
    id: Optional[str] = None          # None 表示映射到 null（使用时拒绝）
    type_coerce: Optional[str] = None  # 子集内仅 '@id'
    language: Any = _MISSING           # _MISSING=未设置；None=显式无语言；str=语言标签
    container: Optional[str] = None    # 子集内仅 '@language'
    prefix: bool = False               # 是否可作为前缀（PyLD 的 _prefix 规则）
    origin: Optional[str] = None       # 定义该词项的 @context 的 JSON Pointer


@dataclass
class Ctx:
    base: Optional[str] = None
    vocab: Optional[str] = None
    language: Optional[str] = None
    terms: dict = field(default_factory=dict)  # str -> TermDef | None（None=映射为 null）
    vocab_origin: Optional[str] = None
    base_origin: Optional[str] = None
    language_origin: Optional[str] = None

    def derive(self) -> "Ctx":
        return Ctx(
            base=self.base,
            vocab=self.vocab,
            language=self.language,
            terms=dict(self.terms),
            vocab_origin=self.vocab_origin,
            base_origin=self.base_origin,
            language_origin=self.language_origin,
        )


# ---------------------------------------------------------------- 值描述符（read B 中间树）

@dataclass
class IriVal:
    iri: str


@dataclass
class LitVal:
    native: Any                      # str/int/float/bool，保留原生类型，绝不先字符串化再猜型
    datatype: Optional[str] = None   # 显式 @type（值对象）；其余在归约时推导
    language: Optional[str] = None


@dataclass
class ListVal:
    ptr: str
    members: list = field(default_factory=list)  # [(Val, pointer)]


@dataclass
class NodeExp:
    """展开中的节点（带来源指针），forward/reverse 尚未定向为边。"""

    ptr: str
    role: str                        # root | property | reverse | list-member
    id: Optional[str] = None
    types: list = field(default_factory=list)      # [(type_iri, pointer)]
    forward: list = field(default_factory=list)    # [(predicate, Val, pointer)]
    reverse: list = field(default_factory=list)    # [(predicate, NodeExp, pointer)]


# ---------------------------------------------------------------- 关系记录（两条读取路径的统一形状）
# obj/member 元组：
#   ('iri', iri)
#   ('lit', datatype, language, lexical, native_json)
#   ('list', list_id)                仅作为事实对象出现

@dataclass
class FactOcc:
    subject: str
    predicate: str
    obj: tuple
    pointer: Optional[str] = None    # read A 无来源，为 None


@dataclass
class ListRec:
    list_id: str
    pointer: Optional[str]
    members: list = field(default_factory=list)  # [(obj_tuple, pointer)]


@dataclass
class NodeOcc:
    node_id: str
    kind: str                        # iri | blank | list
    pointer: Optional[str] = None
    role: Optional[str] = None


@dataclass
class ExpansionRec:
    kind: str                        # predicate | type | id
    pointer: str
    raw: str
    expanded: str
    via: str                         # term | prefix | vocab | absolute | keyword
    context_pointer: Optional[str]


@dataclass
class Records:
    nodes: list = field(default_factory=list)       # [NodeOcc]
    facts: list = field(default_factory=list)       # [FactOcc]
    lists: list = field(default_factory=list)       # [ListRec]
    expansions: list = field(default_factory=list)  # [ExpansionRec]（仅 read B）


# ---------------------------------------------------------------- 字面量归约

def literal_obj_tuple(native: Any, datatype: Optional[str], language: Optional[str]) -> tuple:
    """把 (原生值, datatype, language) 归约为 ('lit', datatype, language, lexical, native_json)。

    数字/布尔的 datatype 由 Python 原生类型直接决定，词法形式由原生值规范导出，
    不存在“先变成展示字符串再猜回类型”的环节。
    """
    if language is not None:
        dt = RDF_LANGSTRING
        lexical = native
        native_json = None
    elif datatype is not None:
        dt = datatype
        lexical = native if isinstance(native, str) else _canonical_lexical(native)
        native_json = None if isinstance(native, str) else json.dumps(native)
    elif isinstance(native, bool):
        dt = XSD_BOOLEAN
        lexical = "true" if native else "false"
        native_json = json.dumps(native)
    elif isinstance(native, int):
        dt = XSD_INTEGER
        lexical = str(native)
        native_json = json.dumps(native)
    elif isinstance(native, float):
        dt = XSD_DOUBLE
        lexical = repr(native)
        native_json = json.dumps(native)
    else:
        dt = XSD_STRING
        lexical = native
        native_json = None
    return ("lit", dt, language, lexical, native_json)


def _canonical_lexical(native: Any) -> str:
    if isinstance(native, bool):
        return "true" if native else "false"
    if isinstance(native, int):
        return str(native)
    if isinstance(native, float):
        return repr(native)
    return str(native)


def node_kind(node_id: str) -> str:
    return "blank" if is_blank_id(node_id) else "iri"
