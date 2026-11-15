"""加载 pieces.json：piece 索引 -> 本地原件路径的映射。

支持两种 JSON 形态：

* 列表（fixtures 使用的形态）：``[{"index": 0, "path": "pieces/a.bin"}, ...]``；
* 字典形态：``{"0": "pieces/a.bin"}``，键为十进制索引。

约束：

* 同一索引不允许重复映射；
* 索引必须是 ``[0, num_pieces)`` 内的整数（布尔值被拒）；
* 路径必须是非空字符串，且文件必须存在、是普通文件且可读；
* 绝不从文件名猜测索引——索引只取自 JSON 字段。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .errors import AssembleError


@dataclass(frozen=True)
class PieceSource:
    index: int
    path: str  # 已解析为绝对路径


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_index(raw_key: object, *, what: str) -> int:
    if _is_plain_int(raw_key):
        return int(raw_key)
    if isinstance(raw_key, str):
        text = raw_key.strip()
        if text and (text.lstrip("-").isdigit()):
            return int(text)
    raise AssembleError(f"{what} 的索引必须是整数，得到 {raw_key!r}")


def load_piece_map(
    json_path: str, num_pieces: int
) -> dict[int, PieceSource]:
    """读取并校验 piece 映射文件。"""
    try:
        with open(json_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise AssembleError(f"无法读取 pieces 映射 {json_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AssembleError(f"pieces 映射不是合法 JSON: {exc}") from exc

    base_dir = os.path.dirname(os.path.abspath(json_path))
    entries: list[tuple[int, str]] = []

    if isinstance(data, list):
        for position, item in enumerate(data):
            what = f"pieces 映射第 {position} 项"
            if not isinstance(item, dict) or "index" not in item or "path" not in item:
                raise AssembleError(f"{what} 必须是含 index 与 path 的对象")
            index = _parse_index(item["index"], what=what)
            raw_path = item["path"]
            if not isinstance(raw_path, str) or not raw_path:
                raise AssembleError(f"{what} 的 path 必须是非空字符串")
            entries.append((index, raw_path))
    elif isinstance(data, dict):
        for raw_key, raw_path in data.items():
            index = _parse_index(raw_key, what=f"pieces 映射键 {raw_key!r}")
            if not isinstance(raw_path, str) or not raw_path:
                raise AssembleError(f"索引 {index} 对应的 path 必须是非空字符串")
            entries.append((index, raw_path))
    else:
        raise AssembleError("pieces 映射顶层必须是列表或对象")

    result: dict[int, PieceSource] = {}
    for index, raw_path in entries:
        if not 0 <= index < num_pieces:
            raise AssembleError(
                f"piece 索引 {index} 越界，合法范围是 [0, {num_pieces})"
            )
        if index in result:
            raise AssembleError(f"piece 索引 {index} 存在重复映射")

        candidate = raw_path
        if not os.path.isabs(candidate):
            candidate = os.path.normpath(os.path.join(base_dir, candidate))
        if not os.path.isfile(candidate):
            raise AssembleError(f"piece {index} 的原件不存在或不是普通文件: {raw_path}")
        if not os.access(candidate, os.R_OK):
            raise AssembleError(f"piece {index} 的原件不可读: {raw_path}")
        result[index] = PieceSource(index=index, path=candidate)

    return result
