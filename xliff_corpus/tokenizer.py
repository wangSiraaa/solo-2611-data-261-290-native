"""把 XLIFF source/target 的混合 XML 内容切成四类 token。

四类 token：

* ``text``        —— 文本节点，按 XML 解析后的字符原样保存，不 trim、不补空格；
* ``placeholder`` —— ``<ph>``，一个独立占位 token；
* ``open``        —— ``<pc>`` 的开标签；
* ``close``       —— 与 ``open`` 配对的 ``<pc>`` 闭标签。

节点 ``text`` 与 ``tail`` 的真实先后通过对 ElementTree 树的递归顺序保留：
父节点 text → 子节点（及其 tail）… → 最后一个子节点的 tail。
XML 实体（如 ``&lt;br/&gt;``）由解析器解码，本模块不执行其中的 HTML。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from xml.etree import ElementTree as ET

TEXT = "text"
PLACEHOLDER = "placeholder"
OPEN = "open"
CLOSE = "close"

CODE_KINDS = {PLACEHOLDER, OPEN, CLOSE}


@dataclass
class Token:
    """单个 token；``path`` 为其来源 XML 节点路径（text 节点挂在宿主元素上）。"""

    type: str
    value: str
    path: str
    code_id: Optional[str] = None
    # ph: dataRef 的值；pc open: dataRefStart；pc close: dataRefEnd。
    # 属性缺失时为 None，并由上层登记 missing_ref_attribute 诊断。
    ref: Optional[str] = None
    # close 指向其 open 的 token 序号，用于 read-back 时校验配对。
    pair_index: Optional[int] = None

    def to_dict(self) -> dict:
        out = {"type": self.type, "value": self.value, "path": self.path}
        if self.code_id is not None:
            out["code_id"] = self.code_id
        if self.ref is not None:
            out["ref"] = self.ref
        if self.pair_index is not None:
            out["pair_index"] = self.pair_index
        return out


@dataclass
class SideParse:
    """单侧（source 或 target）的解析结果。"""

    tokens: list[Token] = field(default_factory=list)
    # 结构问题在 tokenize 阶段只能做局部判断，统一以事件形式上交：
    # ("unsupported", tag, path) / ("duplicate", code_id, path) /
    # ("unbalanced", pc_id, path)
    events: list[tuple] = field(default_factory=list)

    def code_tokens(self) -> list[Token]:
        return [t for t in self.tokens if t.type in CODE_KINDS]


def localname(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def build_parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def node_path(node: ET.Element, parent_map: dict[ET.Element, ET.Element], root: ET.Element) -> str:
    """构造带本地名、id 与同序位置的节点路径，如 /xliff/file[0]/unit[@id='u1'][0]。"""

    chain: list[str] = []
    cur: Optional[ET.Element] = node
    while cur is not None:
        parent = parent_map.get(cur)
        if parent is None:
            chain.append(localname(cur.tag))
            break
        idx = list(parent).index(cur)
        label = localname(cur.tag)
        nid = cur.get("id")
        if nid is not None:
            label += f"[@id='{nid}']"
        chain.append(f"{label}[{idx}]")
        cur = parent
    chain.reverse()
    return "/" + "/".join(chain)


def tokenize(
    element: Optional[ET.Element],
    base_path: str,
    parent_map: Optional[dict[ET.Element, ET.Element]] = None,
    root: Optional[ET.Element] = None,
) -> SideParse:
    """递归把 ``element`` 的全部内容（含 text/tail）切成 token。

    ``element`` 为 None 时返回空结果（调用方负责区分“缺 target”和“空 target”）。
    """

    result = SideParse()
    if element is None:
        return result

    parent_map = parent_map or {}
    seen_ids: set[str] = set()
    open_stack: list[int] = []  # open token 的序号栈

    def path_of(node: ET.Element) -> str:
        if root is not None and node in parent_map:
            return node_path(node, parent_map, root)
        return base_path

    def emit_text(chars: Optional[str], host_path: str) -> None:
        # ET 对不存在的 text/tail 给 None；空串不携带字符故不产生 token，
        # 但任何非空字符串（含纯空白、尾部空格）一律原样保留。
        if chars is None or chars == "":
            return
        result.tokens.append(Token(TEXT, chars, host_path))

    def note_id(code_id: Optional[str], p: str) -> None:
        if code_id is None:
            return
        if code_id in seen_ids:
            result.events.append(("duplicate", code_id, p))
        seen_ids.add(code_id)

    def walk(node: ET.Element) -> None:
        emit_text(node.text, path_of(node))
        for child in list(node):
            name = localname(child.tag)
            child_path = path_of(child)
            if name == "ph":
                cid = child.get("id")
                note_id(cid, child_path)
                result.tokens.append(
                    Token(
                        PLACEHOLDER,
                        f"ph[{cid}]" if cid is not None else "ph[?]",
                        child_path,
                        code_id=cid,
                        ref=child.get("dataRef"),
                    )
                )
            elif name == "pc":
                cid = child.get("id")
                note_id(cid, child_path)
                open_idx = len(result.tokens)
                result.tokens.append(
                    Token(
                        OPEN,
                        f"pc[{cid}].open" if cid is not None else "pc[?].open",
                        child_path,
                        code_id=cid,
                        ref=child.get("dataRefStart"),
                    )
                )
                open_stack.append(open_idx)
                walk(child)
                if open_stack and open_stack[-1] == open_idx:
                    open_stack.pop()
                    pair = open_idx
                else:
                    pair = None
                    result.events.append(("unbalanced", cid, child_path))
                result.tokens.append(
                    Token(
                        CLOSE,
                        f"pc[{cid}].close" if cid is not None else "pc[?].close",
                        child_path,
                        code_id=cid,
                        ref=child.get("dataRefEnd"),
                        pair_index=pair,
                    )
                )
            else:
                # 只支持 ph/pc；其它内联标签不静默忽略，仍递归内部以保留文本来源。
                result.events.append(("unsupported", name, child_path))
                walk(child)
            # tail 是该子节点之后、父节点之内的真实文本。
            emit_text(child.tail, child_path)

    emit_text(element.text, base_path)
    for child in list(element):
        name = localname(child.tag)
        child_path = path_of(child)
        if name == "ph":
            cid = child.get("id")
            note_id(cid, child_path)
            result.tokens.append(
                Token(
                    PLACEHOLDER,
                    f"ph[{cid}]" if cid is not None else "ph[?]",
                    child_path,
                    code_id=cid,
                    ref=child.get("dataRef"),
                )
            )
        elif name == "pc":
            cid = child.get("id")
            note_id(cid, child_path)
            open_idx = len(result.tokens)
            result.tokens.append(
                Token(
                    OPEN,
                    f"pc[{cid}].open" if cid is not None else "pc[?].open",
                    child_path,
                    code_id=cid,
                    ref=child.get("dataRefStart"),
                )
            )
            open_stack.append(open_idx)
            walk(child)
            if open_stack and open_stack[-1] == open_idx:
                open_stack.pop()
                pair = open_idx
            else:
                pair = None
                result.events.append(("unbalanced", cid, child_path))
            result.tokens.append(
                Token(
                    CLOSE,
                    f"pc[{cid}].close" if cid is not None else "pc[?].close",
                    child_path,
                    code_id=cid,
                    ref=child.get("dataRefEnd"),
                    pair_index=pair,
                )
            )
        else:
            result.events.append(("unsupported", name, child_path))
            walk(child)
        emit_text(child.tail, child_path)

    if open_stack:
        leftover = result.tokens[open_stack[0]]
        result.events.append(("unbalanced", leftover.code_id, leftover.path))
    return result


def serialize_tokens(tokens: list[dict | Token]) -> str:
    """规范序列化：text 直接拼字符，代码 token 用带分隔的标记拼接。

    该字符串仅用于 read-back 比对；text 原样保证尾部空白等不被吞掉。
    """

    parts: list[str] = []
    for tok in tokens:
        t = tok["type"] if isinstance(tok, dict) else tok.type
        value = tok["value"] if isinstance(tok, dict) else tok.value
        if t == TEXT:
            parts.append(value)
        else:
            parts.append("⟦" + value + "⟧")
    return "".join(parts)


def pc_containment(tokens: list[dict | Token]) -> dict[str, list[str]]:
    """独立重建每个 pc 内直接/间接包含的代码 id 集合（含嵌套 pc 与 ph）。

    用 open/close 栈扫描，返回 ``{pc_id: [去重后的后代代码 id, 按出现序]}``。
    非平衡输入按“遇 close 弹到匹配 id”容错，并由调用方结合诊断判废。
    """

    stack: list[str] = []
    contained: dict[str, list[str]] = {}
    for tok in tokens:
        ttype = tok["type"] if isinstance(tok, dict) else tok.type
        cid = tok.get("code_id") if isinstance(tok, dict) else tok.code_id
        if ttype == OPEN:
            contained.setdefault(cid, [])
            for ancestor in stack:
                if cid is not None and cid not in contained[ancestor]:
                    contained[ancestor].append(cid)
            stack.append(cid)
        elif ttype == CLOSE:
            while stack and stack[-1] != cid:  # 平衡文档里栈顶即匹配
                stack.pop()
            if stack:
                stack.pop()
        elif ttype == PLACEHOLDER:
            for ancestor in stack:
                if cid is not None and cid not in contained[ancestor]:
                    contained[ancestor].append(cid)
    return contained
