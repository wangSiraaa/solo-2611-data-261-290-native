"""支持范围检查（纯语法层面，只读原文，不求值 context）。

职责：
- 拒绝远程/非内联 context（字符串、数组、null 等）；
- 拒绝未声明的 context 关键字（@import/@version/@propagate/@protected/@direction/@nest 等）；
- 拒绝未声明的数据关键字（@graph/@included/@nest/@set/@index 等）；
- 校验词项定义形状：仅 @id/@type/@language/@container，@container 仅 @language，
  @type 仅 "@id"，@id 不得是关键字别名；
- 拒绝数据中的 null（JSON-LD 会静默丢弃，违反“不静默忽略”）；
- 拒绝 @list 嵌套（列表的列表）；
- 统计节点对象数量上限之外的检查留给 walk.py（那里能精确识别节点）。

凡是本检查放行的文件，walk.py 的独立解释器与 PyLD 标准展开都会再各验证一遍。
"""

from __future__ import annotations

from .errors import Reject
from .model import (
    CONTEXT_META_KEYS,
    DATA_KEYWORDS,
    TERM_DEF_KEYS,
    ptr_join,
)


def check_document(doc) -> None:
    if isinstance(doc, dict):
        _check_data_dict(doc, "")
    elif isinstance(doc, list):
        for i, el in enumerate(doc):
            ptr = ptr_join("", i)
            if not isinstance(el, dict):
                raise Reject("ROOT_ELEMENT_KIND", "根数组元素必须是节点对象", ptr)
            _check_data_dict(el, ptr)
    else:
        raise Reject("ROOT_KIND", "文档根必须是对象或对象数组", "")


def _check_data_dict(d: dict, ptr: str) -> None:
    for key, value in d.items():
        kptr = ptr_join(ptr, key)
        if key == "@context":
            _check_context(value, kptr)
        elif key.startswith("@"):
            if key not in DATA_KEYWORDS:
                raise Reject(
                    "DATA_KEYWORD_UNSUPPORTED",
                    f"不支持的数据关键字 {key}（子集仅支持 {sorted(DATA_KEYWORDS)}）",
                    kptr,
                )
            _check_keyword_value(key, value, kptr)
        else:
            _check_data_value(value, kptr)


def _check_keyword_value(key: str, value, ptr: str) -> None:
    if key == "@id":
        if not isinstance(value, str):
            raise Reject("ID_KIND", "@id 值必须是字符串", ptr)
    elif key == "@type":
        if isinstance(value, str):
            return
        if isinstance(value, list):
            for i, el in enumerate(value):
                if not isinstance(el, str):
                    raise Reject("TYPE_KIND", "@type 数组元素必须是字符串", ptr_join(ptr, i))
            return
        raise Reject("TYPE_KIND", "@type 必须是字符串或字符串数组", ptr)
    elif key == "@value":
        if value is None:
            raise Reject("NULL_VALUE", "@value 不得为 null（展开会静默丢弃）", ptr)
        if isinstance(value, (dict, list)):
            raise Reject("VALUE_KIND", "@value 不得为对象或数组", ptr)
    elif key == "@language":
        if not isinstance(value, str):
            raise Reject("LANGUAGE_KIND", "@language 值必须是字符串", ptr)
    elif key == "@list":
        if not isinstance(value, list):
            raise Reject("LIST_KIND", "@list 值必须是数组", ptr)
        for i, el in enumerate(value):
            eptr = ptr_join(ptr, i)
            if el is None:
                raise Reject("NULL_VALUE", "@list 成员不得为 null", eptr)
            if isinstance(el, list):
                raise Reject("LIST_OF_LISTS", "不支持列表的列表（@list 内嵌数组）", eptr)
            if isinstance(el, dict):
                if "@list" in el:
                    raise Reject("LIST_OF_LISTS", "不支持列表的列表（@list 内嵌 @list）", eptr)
                _check_data_dict(el, eptr)
            # 标量成员合法
    elif key == "@reverse":
        if not isinstance(value, dict):
            raise Reject("REVERSE_KIND", "@reverse 值必须是对象", ptr)
        _check_reverse_map(value, ptr)


def _check_reverse_map(d: dict, ptr: str) -> None:
    for key, value in d.items():
        kptr = ptr_join(ptr, key)
        if key == "@context":
            _check_context(value, kptr)
            continue
        if key.startswith("@"):
            raise Reject(
                "KEYWORD_IN_REVERSE",
                f"@reverse 映射内不允许关键字 {key}（仅可含 @context 与属性）",
                kptr,
            )
        for j, item in enumerate(_arrayify(value)):
            iptr = ptr_join(kptr, j) if isinstance(value, list) else kptr
            if item is None:
                raise Reject("NULL_VALUE", "@reverse 属性值不得为 null", iptr)
            if not isinstance(item, dict):
                raise Reject("REVERSE_VALUE_KIND", "@reverse 属性值必须是节点对象（不得为字面量）", iptr)
            if "@value" in item or "@list" in item:
                raise Reject("REVERSE_VALUE_KIND", "@reverse 属性值不得为 @value/@list 对象", iptr)
            _check_data_dict(item, iptr)


def _check_data_value(value, ptr: str) -> None:
    """普通属性值：标量 / 数组 / 对象。null 一律拒绝。"""
    if value is None:
        raise Reject("NULL_VALUE", "属性值不得为 null（JSON-LD 会静默丢弃，本工具明确拒绝）", ptr)
    if isinstance(value, list):
        for i, el in enumerate(value):
            _check_data_value(el, ptr_join(ptr, i))
    elif isinstance(value, dict):
        _check_data_dict(value, ptr)
    # 标量（str/num/bool）合法


def _check_context(value, ptr: str) -> None:
    if not isinstance(value, dict):
        if isinstance(value, str):
            raise Reject(
                "REMOTE_CONTEXT",
                f"拒绝远程 context 引用 {value!r}：仅支持内联对象 context，不访问任何远程资源",
                ptr,
            )
        raise Reject(
            "CONTEXT_NOT_OBJECT",
            f"@context 必须是内联对象（拒绝 {type(value).__name__} 形式，含数组链与 null 重置）",
            ptr,
        )
    for key, val in value.items():
        kptr = ptr_join(ptr, key)
        if key in CONTEXT_META_KEYS:
            if val is not None and not isinstance(val, str):
                raise Reject(
                    "CONTEXT_META_KIND",
                    f"context 中 {key} 必须是字符串或 null",
                    kptr,
                )
        elif key.startswith("@"):
            raise Reject(
                "CONTEXT_KEYWORD_UNSUPPORTED",
                f"context 中出现未声明的关键字 {key}（子集仅支持 @base/@vocab/@language 与词项定义）",
                kptr,
            )
        else:
            _check_term_def(key, val, kptr)


def _check_term_def(term: str, val, ptr: str) -> None:
    if term == "":
        raise Reject("TERM_DEF_KIND", "词项名不得为空字符串", ptr)
    if val is None:
        return  # 显式 null 映射（使用时由 walk.py 拒绝）
    if isinstance(val, str):
        if val.startswith("@"):
            raise Reject("TERM_ID_KEYWORD", "不支持词项别名到关键字（映射值以 @ 开头）", ptr)
        return  # 简写 IRI/前缀映射
    if not isinstance(val, dict):
        raise Reject("TERM_DEF_KIND", "词项定义必须是字符串、null 或对象", ptr)
    for key, tval in val.items():
        kptr = ptr_join(ptr, key)
        if key not in TERM_DEF_KEYS:
            raise Reject(
                "TERM_DEF_KEY",
                f"词项定义含未声明的键 {key}（子集仅支持 {sorted(TERM_DEF_KEYS)}）",
                kptr,
            )
        if key == "@id":
            if tval is not None and not isinstance(tval, str):
                raise Reject("TERM_ID_KIND", "词项 @id 必须是字符串或 null", kptr)
            if isinstance(tval, str) and tval.startswith("@"):
                raise Reject("TERM_ID_KEYWORD", "不支持词项别名到关键字（@id 以 @ 开头）", kptr)
        elif key == "@type":
            if tval != "@id":
                raise Reject(
                    "TERM_TYPE_UNSUPPORTED",
                    f"词项 @type 仅支持 \"@id\" 类型强制，拒绝 {tval!r}",
                    kptr,
                )
        elif key == "@language":
            if tval is not None and not isinstance(tval, str):
                raise Reject("TERM_LANGUAGE_KIND", "词项 @language 必须是字符串或 null", kptr)
        elif key == "@container":
            if tval != "@language":
                raise Reject(
                    "CONTAINER_UNSUPPORTED",
                    f"@container 仅支持 \"@language\"，拒绝 {tval!r}（不支持容器组合）",
                    kptr,
                )


def _arrayify(value):
    return value if isinstance(value, list) else [value]
