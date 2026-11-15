"""空白节点无关的规范形式核对（1-WL 颜色精化）。

两条读取路径（PyLD 展开归约 / 独立子集解释器）各自产出 Records；
空白节点与列表的标签只在各自记录集内部自洽，因此比较时：
- IRI 节点与字面量按精确值参与（词法、datatype、language 完全一致）；
- 空白节点与列表节点只做结构等价（1-WL 迭代精化到不动点）；
- 列表成员边携带从零开始的下标，重复成员与顺序都被严格比较；
- 事实与节点签名都按多重集合比较。

返回 (是否一致, 说明)。不一致即整体拒绝（CHECK_MISMATCH）。
"""

from __future__ import annotations

import hashlib
from collections import Counter

from .model import Records


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class _Form:
    def __init__(self, records: Records):
        self.list_ids = {lst.list_id for lst in records.lists}
        # 边：(subject_id, 标签, 对象)；对象为 ('N', node_id) 或 ('L', dt, lang, lexical)
        self.edges: list[tuple[str, str, tuple]] = []
        for f in records.facts:
            self.edges.append((f.subject, "F\x00" + f.predicate, self._token(f.obj)))
        for lst in records.lists:
            for i, (member, _ptr) in enumerate(lst.members):
                self.edges.append((lst.list_id, f"M\x00{i}", self._token(member)))

        self.nodes: set[str] = set()
        for occ in records.nodes:
            self.nodes.add(occ.node_id)
        for s, _label, o in self.edges:
            self.nodes.add(s)
            if o[0] == "N":
                self.nodes.add(o[1])

    def _token(self, obj: tuple) -> tuple:
        kind = obj[0]
        if kind in ("iri", "list"):
            return ("N", obj[1])
        # 字面量：('lit', datatype, language, lexical, native_json)
        return ("L", obj[1], obj[2], obj[3])

    def kind(self, node_id: str) -> str:
        if node_id in self.list_ids:
            return "list"
        if node_id.startswith("_:"):
            return "blank"
        return "iri"

    def signatures(self) -> dict[str, str]:
        out_edges: dict[str, list] = {n: [] for n in self.nodes}
        in_edges: dict[str, list] = {n: [] for n in self.nodes}
        for s, label, o in self.edges:
            out_edges[s].append((label, o))
            if o[0] == "N":
                in_edges[o[1]].append((label, s))

        sig: dict[str, str] = {}
        for n in self.nodes:
            k = self.kind(n)
            sig[n] = ("I\x00" + n) if k == "iri" else ("L" if k == "list" else "B")

        def obj_sig(current: dict[str, str], tok) -> str:
            if tok[0] == "N":
                return current[tok[1]]
            return "T\x00" + repr(tok)

        # 同步（Jacobi）更新：第 i 轮签名只依赖第 i-1 轮，
        # 结果与节点迭代顺序无关，两次运行/两个记录集之间确定可比。
        for _ in range(len(self.nodes) + 2):
            new: dict[str, str] = {}
            for n in self.nodes:
                if self.kind(n) == "iri":
                    new[n] = sig[n]
                    continue
                out_s = sorted(f"{lbl}\x00{obj_sig(sig, o)}" for lbl, o in out_edges[n])
                in_s = sorted(f"{lbl}\x00{sig[s]}" for lbl, s in in_edges[n])
                new[n] = _h(
                    self.kind(n) + "\x00" + "\x01".join(out_s) + "\x02" + "\x01".join(in_s)
                )
            if new == sig:
                break
            sig = new
        return sig

    def canonical(self) -> tuple:
        sig = self.signatures()
        node_ms = Counter(sig[n] for n in self.nodes)
        edge_ms = Counter()
        for s, label, o in self.edges:
            o_sig = sig[o[1]] if o[0] == "N" else "T\x00" + repr(o)
            edge_ms[(sig[s], label, o_sig)] += 1
        return (frozenset(node_ms.items()), frozenset(edge_ms.items()))


def compare(a: Records, b: Records) -> tuple[bool, str]:
    """比较两个记录集的关系内容（忽略空白节点/列表的具体标签与来源指针）。"""
    cheap_a = (len(a.facts), len(a.lists), sum(len(l.members) for l in a.lists), len(a.nodes))
    cheap_b = (len(b.facts), len(b.lists), sum(len(l.members) for l in b.lists), len(b.nodes))
    if cheap_a[:3] != cheap_b[:3]:
        return False, f"计数不一致：事实/列表/成员 {cheap_a[:3]} != {cheap_b[:3]}"
    fa, fb = _Form(a), _Form(b)
    ca, cb = fa.canonical(), fb.canonical()
    if ca == cb:
        return True, "两路读取的关系内容一致（空白节点结构同构）"
    na, nb = len(ca[0]), len(cb[0])
    ea, eb = len(ca[1]), len(cb[1])
    return False, f"规范形式不一致：节点签名 {na} vs {nb}，边签名 {ea} vs {eb}"
