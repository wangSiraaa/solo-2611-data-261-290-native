"""read A：PyLD 标准展开（离线），再把展开树归约为统一关系记录。

- documentLoader 永远抛错：不访问任何远程 context（scope 检查已先行拒绝远程引用，
  这里作为第二道防线）。
- 展开选项保持默认（processingMode json-ld-1.1、默认 base）；经 walk.py 预验证的
  文件不会触及默认 base 分支。
- 归约器只读取 PyLD 展开输出，不引入额外语义；空白节点/列表标签仅在本记录集内部
  自洽即可（canon.py 做空白节点无关比较）。
"""

from __future__ import annotations

from pyld import jsonld

from .errors import Reject
from .model import (
    FactOcc,
    ListRec,
    NodeOcc,
    RDF_TYPE,
    Records,
    literal_obj_tuple,
    node_kind,
)


def _forbidden_loader(*args, **kwargs):
    raise Reject("REMOTE_CONTEXT", "检测到远程资源加载企图：本工具不访问任何远程 context")


def expand_with_pyld(doc):
    """PyLD 标准展开；任何 PyLD 错误包装为显式拒绝。"""
    try:
        return jsonld.expand(doc, options={"documentLoader": _forbidden_loader})
    except Reject:
        raise
    except Exception as e:  # JsonLdError 等
        raise Reject("LIB_ERROR", f"PyLD 标准展开失败：{e}")


class _Reducer:
    """把 PyLD 展开输出（嵌套节点树）归约为 Records。标签仅内部自洽。"""

    def __init__(self):
        self.records = Records()
        self._n = 0
        self._l = 0

    def _blank(self) -> str:
        label = f"_:a{self._n}"
        self._n += 1
        return label

    def _list_label(self) -> str:
        label = f"_:al{self._l}"
        self._l += 1
        return label

    def run(self, expanded) -> Records:
        for node in expanded:
            self._node(node, "root")
        return self.records

    def _node(self, n: dict, role: str) -> str:
        nid = n.get("@id")
        if nid is None:
            nid = self._blank()
        self.records.nodes.append(NodeOcc(nid, node_kind(nid), None, role))
        for t in n.get("@type", []):
            self.records.facts.append(FactOcc(nid, RDF_TYPE, ("iri", t)))
        for key, values in n.items():
            if key in ("@id", "@type"):
                continue
            if key == "@reverse":
                for pred, rvalues in values.items():
                    for item in rvalues:
                        sid = self._node(item, "reverse")
                        # @reverse：边方向为 item -> pred -> 当前节点
                        self.records.facts.append(FactOcc(sid, pred, ("iri", nid)))
                continue
            for v in values:
                self._value(nid, key, v)
        return nid

    def _value(self, subject: str, pred: str, v) -> None:
        if "@value" in v:
            self.records.facts.append(
                FactOcc(
                    subject,
                    pred,
                    literal_obj_tuple(v["@value"], v.get("@type"), v.get("@language")),
                )
            )
        elif "@list" in v:
            lid = self._list_label()
            self.records.facts.append(FactOcc(subject, pred, ("list", lid)))
            rec = ListRec(list_id=lid, pointer=None)
            for member in v["@list"]:
                if "@value" in member:
                    rec.members.append(
                        (
                            literal_obj_tuple(
                                member["@value"], member.get("@type"), member.get("@language")
                            ),
                            None,
                        )
                    )
                elif "@list" in member:
                    raise Reject("LIST_OF_LISTS", "PyLD 展开结果出现列表的列表")
                elif set(member) == {"@id"}:
                    rec.members.append((("iri", member["@id"]), None))
                else:
                    mid = self._node(member, "list-member")
                    rec.members.append((("iri", mid), None))
            self.records.lists.append(rec)
        elif set(v) == {"@id"}:
            self.records.facts.append(FactOcc(subject, pred, ("iri", v["@id"])))
        else:
            oid = self._node(v, "property")
            self.records.facts.append(FactOcc(subject, pred, ("iri", oid)))


def records_from_pyld(doc) -> Records:
    return _Reducer().run(expand_with_pyld(doc))
