"""read B：独立子集解释器。

不使用 PyLD 的展开，自行完成：
- 内联 context 求值（@base/@vocab/@language/词项/前缀/@id 强制/@container:@language），
  嵌套 context 沿子树生效、不向兄弟泄漏；
- 数据展开（@id/@type/普通对象/多值数组/@value/@language/@type/@list/@reverse）；
- 全程记录每个事实、列表成员、节点出现的源 JSON Pointer；
- 对子集之外的构造一律 Reject（绝不静默忽略）。

输出 Records（事实/列表/节点/词项解析记录），随后：
- canon.py 与 read A（PyLD 展开归约）做空白节点无关核对；
- tables.py 落成关系表（事实集合去重、来源多重保留）。
"""

from __future__ import annotations

from urllib.parse import urljoin

from .errors import Diagnostic, Reject
from .model import (
    Ctx,
    ExpansionRec,
    FactOcc,
    IriVal,
    ListRec,
    ListVal,
    LitVal,
    MAX_NODES,
    NodeExp,
    NodeOcc,
    RDF_TYPE,
    Records,
    TermDef,
    KEYWORD_PATTERN,
    TERM_LOOKS_LIKE_IRI,
    GEN_DELIM_END,
    _MISSING,
    is_absolute_iri,
    is_blank_id,
    literal_obj_tuple,
    node_kind,
    ptr_join,
)


class Walker:
    def __init__(self, digest8: str):
        self.digest8 = digest8
        self.notes: list[Diagnostic] = []
        self.expansions: list[ExpansionRec] = []
        self.node_count = 0
        self._blank_n = 0
        self._blank_l = 0
        self._user_blanks: set[str] = set()
        self._out_nodes: list[NodeOcc] = []
        self._out_facts: list[FactOcc] = []
        self._out_lists: list[ListRec] = []

    # ------------------------------------------------------------ 入口

    def run(self, doc) -> Records:
        self._prescan_user_blanks(doc)
        roots: list[NodeExp] = []
        ctx = Ctx()
        if isinstance(doc, list):
            for i, el in enumerate(doc):
                ptr = ptr_join("", i)
                roots.append(self._expand_root_element(el, ctx, ptr))
        else:
            roots.append(self._expand_root_element(doc, ctx, ""))

        kept = []
        for n in roots:
            if not n.types and not n.forward and not n.reverse:
                # 与 PyLD 一致：根位置仅含 @id 或完全为空的对象展开后不存在；
                # 不同之处在于我们显式记录一条诊断，而不是无声消失。
                self.notes.append(
                    Diagnostic("note", "ROOT_NODE_DROPPED", n.ptr,
                               "根位置的纯 @id 引用/空对象按 JSON-LD 展开规则不产生任何事实")
                )
                continue
            kept.append(n)

        for n in kept:
            self._emit_node(n)

        return Records(
            nodes=self._out_nodes,
            facts=self._out_facts,
            lists=self._out_lists,
            expansions=self.expansions,
        )

    def _expand_root_element(self, el, ctx: Ctx, ptr: str) -> NodeExp:
        if not isinstance(el, dict):
            raise Reject("ROOT_ELEMENT_KIND", "根元素必须是节点对象", ptr)
        if "@value" in el or "@list" in el:
            raise Reject(
                "ROOT_NOT_NODE",
                "根位置不允许 @value/@list 对象（JSON-LD 会静默丢弃，本工具明确拒绝）",
                ptr,
            )
        return self._expand_node(el, ctx, ptr, "root")

    # ------------------------------------------------------------ context 求值

    def _process_context(self, parent: Ctx, cobj: dict, ptr: str) -> Ctx:
        new = parent.derive()

        if "@base" in cobj:
            v = cobj["@base"]
            if v is None:
                new.base, new.base_origin = None, None
            elif is_absolute_iri(v):
                new.base, new.base_origin = v, ptr_join(ptr, "@base")
            elif parent.base is not None:
                new.base = urljoin(parent.base, v)
                new.base_origin = ptr_join(ptr, "@base")
            else:
                raise Reject(
                    "RELATIVE_BASE_NO_INHERIT",
                    f"@base {v!r} 为相对 IRI 且无继承 base，无法确定有效 base",
                    ptr_join(ptr, "@base"),
                )

        if "@vocab" in cobj:
            v = cobj["@vocab"]
            if v is None:
                new.vocab, new.vocab_origin = None, None
            elif is_absolute_iri(v):
                new.vocab, new.vocab_origin = v, ptr_join(ptr, "@vocab")
            else:
                raise Reject(
                    "VOCAB_NOT_ABSOLUTE",
                    f"@vocab 必须是绝对 IRI，拒绝 {v!r}",
                    ptr_join(ptr, "@vocab"),
                )

        if "@language" in cobj:
            v = cobj["@language"]
            if v is None:
                new.language, new.language_origin = None, None
            else:
                new.language = v.lower()
                new.language_origin = ptr_join(ptr, "@language")

        defined: dict[str, bool] = {}

        def define(term: str) -> None:
            if term in defined:
                if defined[term]:
                    return
                raise Reject("CYCLIC_CONTEXT", f"词项定义循环引用：{term!r}", ptr)
            defined[term] = False
            raw = cobj.get(term)
            simple = raw is None or isinstance(raw, str)
            d = {"@id": raw} if simple else raw

            mapping = TermDef(origin=ptr)
            has_id = "@id" in d
            if has_id:
                idv = d["@id"]
                if idv is None:
                    mapping.id = None
                elif idv != term:
                    expanded = expand_local(idv)
                    if expanded is None or not is_absolute_iri(expanded):
                        raise Reject(
                            "TERM_ID_NOT_ABSOLUTE",
                            f"词项 {term!r} 的 @id 无法展开为绝对 IRI：{idv!r}",
                            ptr_join(ptr, term),
                        )
                    if TERM_LOOKS_LIKE_IRI.match(term):
                        # PyLD 规则：形如 IRI 的词项必须展开到其定义本身
                        saved = defined[term]
                        defined[term] = True
                        term_iri = expand_local(term)
                        defined[term] = saved
                        if term_iri != expanded:
                            raise Reject(
                                "TERM_IRI_MISMATCH",
                                f"词项 {term!r} 形如 IRI，但其 @id 展开 {expanded!r} 与词项自身展开不一致",
                                ptr_join(ptr, term),
                            )
                    mapping.id = expanded
                    mapping.prefix = (
                        simple
                        and ":" not in term
                        and (expanded.startswith("_:") or bool(GEN_DELIM_END.match(expanded)))
                    )
                # idv == term：视同未提供 @id，走下方 vocab/前缀规则

            if mapping.id is None and not (has_id and d["@id"] is None):
                if ":" in term and not term.startswith(":"):
                    prefix, suffix = term.split(":", 1)
                    if prefix in cobj:
                        define(prefix)
                    pm = new.terms.get(prefix)
                    if pm is not None:
                        mapping.id = (pm.id or "") + suffix
                    else:
                        mapping.id = term  # 词项本身即绝对 IRI 形
                else:
                    if new.vocab is None:
                        raise Reject(
                            "TERM_NO_ID",
                            f"词项 {term!r} 未提供 @id 且无 @vocab 可拼接",
                            ptr_join(ptr, term),
                        )
                    mapping.id = new.vocab + term

            if has_id and d["@id"] is None:
                new.terms[term] = None  # 显式 null 映射：使用时拒绝
            else:
                if d.get("@type") == "@id":
                    mapping.type_coerce = "@id"
                if d.get("@container") == "@language":
                    mapping.container = "@language"
                if "@language" in d and "@type" not in d:
                    lang = d["@language"]
                    mapping.language = lang.lower() if isinstance(lang, str) else None
                elif "@language" in d and "@type" in d:
                    self.notes.append(
                        Diagnostic("note", "TERM_LANGUAGE_IGNORED", ptr_join(ptr, term),
                                   "词项同时声明 @type 与 @language，@language 按 JSON-LD 规则不生效")
                    )
                new.terms[term] = mapping
            defined[term] = True

        def expand_local(value: str):
            """context 内部的 IRI 展开（vocab 规则 + 本地词项按需定义，不做 base 解析）。"""
            if KEYWORD_PATTERN.match(value):
                return None
            if value in cobj and defined.get(value) is not True:
                define(value)
            if value in new.terms:
                m = new.terms[value]
                return None if m is None else m.id
            colon = value.find(":")
            if colon > 0:
                prefix, suffix = value[:colon], value[colon + 1:]
                if prefix == "_" or suffix.startswith("//"):
                    return value
                if prefix in cobj and not defined.get(prefix):
                    define(prefix)
                pm = new.terms.get(prefix)
                if pm is not None and pm.prefix:
                    return (pm.id or "") + suffix
                if is_absolute_iri(value):
                    return value
                return None
            if new.vocab is not None:
                return new.vocab + value
            return None

        for key in cobj:
            if not key.startswith("@"):
                define(key)
        return new

    # ------------------------------------------------------------ 数据展开用的 IRI 展开

    def _expand_property(self, key: str, ctx: Ctx, ptr: str):
        """属性键展开（vocab 规则，不做 base 解析）。返回 (iri, via, origin)。"""
        if KEYWORD_PATTERN.match(key):
            raise Reject("DATA_KEYWORD_UNSUPPORTED", f"未支持的关键字形键 {key!r}", ptr)
        if key in ctx.terms:
            m = ctx.terms[key]
            if m is None or m.id is None:
                raise Reject(
                    "UNMAPPED_PROPERTY",
                    f"属性 {key!r} 被 context 显式映射为 null，使用即拒绝（不静默丢弃字段）",
                    ptr,
                )
            return m.id, "term", m.origin
        colon = key.find(":")
        if colon > 0:
            prefix, suffix = key[:colon], key[colon + 1:]
            if prefix == "_" or suffix.startswith("//"):
                return key, "absolute", None
            pm = ctx.terms.get(prefix)
            if pm is not None and pm.prefix:
                return (pm.id or "") + suffix, "prefix", pm.origin
            if is_absolute_iri(key):
                return key, "absolute", None
            raise Reject(
                "UNMAPPED_PROPERTY",
                f"属性 {key!r} 使用未定义前缀且非绝对 IRI，展开失败（不静默丢弃字段）",
                ptr,
            )
        if ctx.vocab is not None:
            return ctx.vocab + key, "vocab", ctx.vocab_origin
        raise Reject(
            "UNMAPPED_PROPERTY",
            f"属性 {key!r} 既非词项/绝对 IRI，又无 @vocab 可拼接（不静默丢弃字段）",
            ptr,
        )

    def _expand_id_value(self, value: str, ctx: Ctx, ptr: str) -> str:
        """@id 及 @id 强制值的展开（document-relative）。"""
        if KEYWORD_PATTERN.match(value):
            raise Reject("ID_NOT_ABSOLUTE", f"@id 值不得为关键字形 {value!r}", ptr)
        colon = value.find(":")
        if colon > 0:
            prefix, suffix = value[:colon], value[colon + 1:]
            if prefix == "_" or suffix.startswith("//"):
                return value
            pm = ctx.terms.get(prefix)
            if pm is not None and pm.prefix:
                return (pm.id or "") + suffix
            if is_absolute_iri(value):
                return value
        if ctx.base is not None:
            return urljoin(ctx.base, value)
        raise Reject(
            "RELATIVE_ID_NO_BASE",
            f"相对 @id {value!r} 无有效 @base 可解析",
            ptr,
        )

    def _expand_type_value(self, value: str, ctx: Ctx, ptr: str):
        """@type 值展开（vocab 规则 + document-relative）。返回 (iri, via, origin)。"""
        if KEYWORD_PATTERN.match(value):
            raise Reject("TYPE_NOT_ABSOLUTE", f"@type 值不得为关键字形 {value!r}", ptr)
        if value in ctx.terms:
            m = ctx.terms[value]
            if m is None or m.id is None:
                raise Reject("TYPE_NOT_ABSOLUTE", f"@type 值 {value!r} 映射为 null", ptr)
            return m.id, "term", m.origin
        colon = value.find(":")
        if colon > 0:
            prefix, suffix = value[:colon], value[colon + 1:]
            if prefix == "_" or suffix.startswith("//"):
                return value, "absolute", None
            pm = ctx.terms.get(prefix)
            if pm is not None and pm.prefix:
                return (pm.id or "") + suffix, "prefix", pm.origin
            if is_absolute_iri(value):
                return value, "absolute", None
        if ctx.vocab is not None:
            return ctx.vocab + value, "vocab", ctx.vocab_origin
        if ctx.base is not None:
            return urljoin(ctx.base, value), "base", ctx.base_origin
        raise Reject("TYPE_NOT_ABSOLUTE", f"@type 值 {value!r} 无法展开为绝对 IRI", ptr)

    # ------------------------------------------------------------ 节点与值

    def _expand_node(self, obj: dict, ctx: Ctx, ptr: str, role: str) -> NodeExp:
        self.node_count += 1
        if self.node_count > MAX_NODES:
            raise Reject("MAX_NODES", f"节点对象数量超过上限 {MAX_NODES}", ptr)

        if "@context" in obj:
            ctx = self._process_context(ctx, obj["@context"], ptr_join(ptr, "@context"))

        node = NodeExp(ptr=ptr, role=role)

        if "@id" in obj:
            raw_id = obj["@id"]
            node.id = self._expand_id_value(raw_id, ctx, ptr_join(ptr, "@id"))
            via = "blank" if is_blank_id(node.id) else (
                "absolute" if raw_id == node.id else (
                    "prefix" if ":" in raw_id and not raw_id.startswith(node.id) else "base"
                )
            )
            origin = ctx.base_origin if via == "base" else None
            if via == "prefix":
                origin = ctx.terms[raw_id.split(":", 1)[0]].origin
            self.expansions.append(
                ExpansionRec("id", ptr_join(ptr, "@id"), raw_id, node.id, via, origin)
            )

        if "@type" in obj:
            raw = obj["@type"]
            items = [(raw, ptr_join(ptr, "@type"))] if isinstance(raw, str) else [
                (t, ptr_join(ptr_join(ptr, "@type"), i)) for i, t in enumerate(raw)
            ]
            for tval, tptr in items:
                tiri, via, origin = self._expand_type_value(tval, ctx, tptr)
                node.types.append((tiri, tptr))
                self.expansions.append(ExpansionRec("type", tptr, tval, tiri, via, origin))

        for key, value in obj.items():
            if key in ("@context", "@id", "@type"):
                continue
            kptr = ptr_join(ptr, key)
            if key == "@reverse":
                self._expand_reverse_map(node, value, ctx, kptr)
                continue
            if key.startswith("@"):
                raise Reject(
                    "NODE_KEYWORD_MISPLACED",
                    f"关键字 {key} 不得出现在节点对象中（@value/@list 仅用于值位置）",
                    kptr,
                )
            pred, via, origin = self._expand_property(key, ctx, kptr)
            term = ctx.terms.get(key)
            self.expansions.append(ExpansionRec("predicate", kptr, key, pred, via, origin))

            if term is not None and term.container == "@language" and isinstance(value, dict):
                for lit, lptr in self._expand_language_map(value, kptr):
                    node.forward.append((pred, lit, lptr))
                continue

            for item, iptr in self._iter_values(value, kptr):
                node.forward.append((pred, self._expand_value(item, ctx, term, iptr), iptr))

        return node

    def _expand_reverse_map(self, node: NodeExp, rmap: dict, ctx: Ctx, ptr: str) -> None:
        rctx = ctx
        if "@context" in rmap:
            rctx = self._process_context(ctx, rmap["@context"], ptr_join(ptr, "@context"))
        for key, value in rmap.items():
            if key == "@context":
                continue
            kptr = ptr_join(ptr, key)
            pred, via, origin = self._expand_property(key, rctx, kptr)
            self.expansions.append(ExpansionRec("predicate", kptr, key, pred, via, origin))
            for item, iptr in self._iter_values(value, kptr):
                # scope 已保证 item 为节点对象（非 @value/@list）
                sub = self._expand_node(item, rctx, iptr, "reverse")
                node.reverse.append((pred, sub, iptr))

    def _iter_values(self, value, ptr: str):
        """多值数组：普通数组为无序多值；嵌套数组按 JSON-LD 规则拍平，各自保留指针。"""
        if isinstance(value, list):
            for i, el in enumerate(value):
                yield from self._iter_values(el, ptr_join(ptr, i))
        else:
            yield value, ptr

    def _expand_value(self, value, ctx: Ctx, term, ptr: str):
        if isinstance(value, dict):
            if "@value" in value:
                return self._expand_value_object(value, ctx, ptr)
            if "@list" in value:
                return self._expand_list(value, ctx, term, ptr)
            return self._expand_node(value, ctx, ptr, "property")
        if isinstance(value, str):
            if term is not None and term.type_coerce == "@id":
                iri = self._expand_id_value(value, ctx, ptr)
                return IriVal(iri)
            language = None
            if term is not None and term.language is not _MISSING:
                language = term.language  # 可能为 None（显式无语言）
            else:
                language = ctx.language
            return LitVal(value, None, language)
        # 数字/布尔：保留原生类型；@id 强制仅作用于字符串（与 PyLD 一致）
        return LitVal(value, None, None)

    def _expand_value_object(self, d: dict, ctx: Ctx, ptr: str) -> LitVal:
        extra = set(d) - {"@value", "@type", "@language"}
        if extra:
            raise Reject(
                "INVALID_VALUE_OBJECT",
                f"值对象只允许 @value/@type/@language，多余键：{sorted(extra)}",
                ptr,
            )
        native = d["@value"]
        datatype = None
        language = None
        if "@type" in d and d["@type"] is not None:
            tval = d["@type"]
            if not isinstance(tval, str):
                raise Reject("VALUE_TYPE_KIND", "值对象的 @type 必须是单个字符串", ptr_join(ptr, "@type"))
            tiri, _, _ = self._expand_type_value(tval, ctx, ptr_join(ptr, "@type"))
            if is_blank_id(tiri):
                raise Reject("VALUE_TYPE_NOT_ABSOLUTE", "值对象 @type 不得为空白节点标识", ptr_join(ptr, "@type"))
            datatype = tiri
        if "@language" in d:
            if datatype is not None:
                raise Reject("VALUE_LANG_TYPE_CONFLICT", "值对象不得同时含 @type 与 @language", ptr)
            if not isinstance(native, str):
                raise Reject("VALUE_LANG_NONSTRING", "只有字符串可带 @language", ptr_join(ptr, "@language"))
            language = d["@language"].lower()
        return LitVal(native, datatype, language)

    def _expand_list(self, d: dict, ctx: Ctx, term, ptr: str) -> ListVal:
        extra = set(d) - {"@list"}
        if extra:
            raise Reject("LIST_EXTRA_KEY", f"@list 对象不允许其他键：{sorted(extra)}", ptr)
        members = []
        for i, el in enumerate(d["@list"]):
            mptr = ptr_join(ptr_join(ptr, "@list"), i)
            members.append((self._expand_value(el, ctx, term, mptr), mptr))
        return ListVal(ptr=ptr, members=members)

    def _expand_language_map(self, d: dict, ptr: str):
        out = []
        for tag, values in d.items():
            tptr = ptr_join(ptr, tag)
            # 与 PyLD 一致：语言映射值只 arrayify 一层，嵌套数组视为非法
            items = values if isinstance(values, list) else [values]
            for j, item in enumerate(items):
                iptr = ptr_join(tptr, j) if isinstance(values, list) else tptr
                if not isinstance(item, str):
                    raise Reject("LANGMAP_VALUE_KIND", "语言映射的值必须是字符串", iptr)
                out.append((LitVal(item, None, tag.lower()), iptr))
        return out

    # ------------------------------------------------------------ 发射记录（含空白节点标记）

    def _new_blank(self, kind: str) -> str:
        while True:
            if kind == "n":
                label = f"_:g{self.digest8}n{self._blank_n}"
                self._blank_n += 1
            else:
                label = f"_:g{self.digest8}l{self._blank_l}"
                self._blank_l += 1
            if label not in self._user_blanks:
                return label

    def _node_id(self, node: NodeExp) -> str:
        if node.id is None:
            node.id = self._new_blank("n")
        return node.id

    def _emit_node(self, node: NodeExp) -> None:
        nid = self._node_id(node)
        self._out_nodes.append(NodeOcc(nid, node_kind(nid), node.ptr, node.role))
        for tiri, tptr in node.types:
            self._out_facts.append(FactOcc(nid, RDF_TYPE, ("iri", tiri), tptr))
        for pred, val, vptr in node.forward:
            self._emit_forward(nid, pred, val, vptr)
        for pred, sub, sptr in node.reverse:
            sid = self._node_id(sub)
            # @reverse：边方向为 sub -> pred -> 当前节点
            self._out_facts.append(FactOcc(sid, pred, ("iri", nid), sptr))
            self._emit_node(sub)

    def _emit_forward(self, subject: str, pred: str, val, ptr: str) -> None:
        if isinstance(val, IriVal):
            self._out_facts.append(FactOcc(subject, pred, ("iri", val.iri), ptr))
        elif isinstance(val, LitVal):
            self._out_facts.append(
                FactOcc(subject, pred, literal_obj_tuple(val.native, val.datatype, val.language), ptr)
            )
        elif isinstance(val, ListVal):
            lid = self._new_blank("l")
            self._out_facts.append(FactOcc(subject, pred, ("list", lid), ptr))
            rec = ListRec(list_id=lid, pointer=val.ptr)
            for member, mptr in val.members:
                if isinstance(member, NodeExp):
                    mid = self._node_id(member)
                    rec.members.append((("iri", mid), mptr))
                    self._emit_node(member)
                elif isinstance(member, IriVal):
                    rec.members.append((("iri", member.iri), mptr))
                else:
                    rec.members.append(
                        (literal_obj_tuple(member.native, member.datatype, member.language), mptr)
                    )
            self._out_lists.append(rec)
        else:  # NodeExp
            oid = self._node_id(val)
            self._out_facts.append(FactOcc(subject, pred, ("iri", oid), ptr))
            self._emit_node(val)

    # ------------------------------------------------------------ 辅助

    def _prescan_user_blanks(self, doc) -> None:
        def walk(v):
            if isinstance(v, dict):
                for k, val in v.items():
                    if k == "@id" and isinstance(val, str) and val.startswith("_:"):
                        self._user_blanks.add(val)
                    walk(val)
            elif isinstance(v, list):
                for el in v:
                    walk(el)
        walk(doc)
