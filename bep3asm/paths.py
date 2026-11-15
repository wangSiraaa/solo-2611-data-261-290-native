"""路径组件安全校验与文件树冲突检测。

规则（对 ``info.name`` 与每个文件的每个 ``path`` 组件统一适用）：

* 组件必须是非空 UTF-8 文本；
* 拒绝 NUL、``.``、``..``；
* 拒绝任何分隔符（POSIX ``/`` 与 Windows ``\\``）以及 ``\\0``；
* 拒绝绝对路径（含 Windows 盘符 ``C:`` 与 UNC 形式）。

另在写入前检测：

* 重复的完整文件路径；
* 文件/目录前缀冲突——一个文件路径是另一个文件路径的前缀，
  例如 ``a/b`` 与 ``a/b/c`` 在真实文件系统上无法同时存在。
"""

from __future__ import annotations

from .errors import AssembleError

# 同时拒绝两类分隔符，保证在任何宿主平台上行为一致。
_FORBIDDEN_COMPONENTS = frozenset({"", ".", ".."})
_SEPARATORS = ("/", "\\")


def validate_component(component: object, *, what: str) -> str:
    """校验单个路径组件并返回其字符串形式。"""
    if not isinstance(component, bytes):
        raise AssembleError(f"{what} 的路径组件必须是字节串（UTF-8）")
    try:
        text = component.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssembleError(f"{what} 的路径组件不是合法 UTF-8: {exc}") from exc

    if text in _FORBIDDEN_COMPONENTS:
        raise AssembleError(f"{what} 含空、'.' 或 '..' 路径组件: {text!r}")
    if "\x00" in text:
        raise AssembleError(f"{what} 的路径组件含 NUL 字符: {text!r}")
    for sep in _SEPARATORS:
        if sep in text:
            raise AssembleError(f"{what} 的路径组件含分隔符 {sep!r}: {text!r}")
    if len(text) >= 2 and text[1] == ":":
        # Windows 盘符（如 C:）——组件内不允许出现，杜绝绝对/盘符路径。
        raise AssembleError(f"{what} 的路径组件疑似 Windows 盘符: {text!r}")
    return text


def validate_relative_path(components: object, *, what: str) -> list[str]:
    """校验完整的相对路径组件序列。"""
    if not isinstance(components, list) or not components:
        raise AssembleError(f"{what} 的 path 必须是非空组件列表")
    return [validate_component(part, what=what) for part in components]


def validate_label(name: object) -> str:
    """校验 ``info.name``——它只作为恢复目录标签，不参与文件拼接。"""
    if not isinstance(name, bytes):
        raise AssembleError("info.name 必须是字节串（UTF-8）")
    return validate_component(name, what="info.name")


def check_tree_conflicts(file_paths: list[list[str]]) -> None:
    """检测重复完整路径与文件/目录前缀冲突。"""
    seen: set[tuple[str, ...]] = set()
    for path in file_paths:
        node = tuple(path)
        if node in seen:
            raise AssembleError(f"重复的完整文件路径: {'/'.join(node)}")
        for earlier in seen:
            if node[: len(earlier)] == earlier:
                raise AssembleError(
                    f"文件/目录前缀冲突: {'/'.join(earlier)} 与 {'/'.join(node)}"
                )
            if earlier[: len(node)] == node:
                raise AssembleError(
                    f"文件/目录前缀冲突: {'/'.join(node)} 与 {'/'.join(earlier)}"
                )
        seen.add(node)
