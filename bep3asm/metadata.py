"""读取并严格校验 BEP3 多文件模式的 ``.torrent`` 元数据。

本工具只支持 ``info`` 中的四个键：``name``、``piece length``、``pieces``、
``files``；不实现单文件模式（含 ``length`` 键的 info）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import bencode
from .errors import AssembleError
from .paths import check_tree_conflicts, validate_label, validate_relative_path

_SHA1_LEN = hashlib.sha1().digest_size
_ALLOWED_INFO_KEYS = frozenset(
    {b"name", b"piece length", b"pieces", b"files"}
)
_ALLOWED_FILE_KEYS = frozenset({b"length", b"path"})


@dataclass(frozen=True)
class FileMeta:
    """files 列表中的一项，``offset`` 是其在逻辑字节流中的起点。"""

    length: int
    parts: tuple[str, ...]
    offset: int

    @property
    def logical_end(self) -> int:
        return self.offset + self.length

    @property
    def rel_path(self) -> str:
        return "/".join(self.parts)


@dataclass(frozen=True)
class TorrentMeta:
    name: str
    piece_length: int
    piece_hashes: tuple[bytes, ...]
    files: tuple[FileMeta, ...]
    total_length: int

    @property
    def num_pieces(self) -> int:
        return len(self.piece_hashes)

    def expected_piece_length(self, index: int) -> int:
        """piece i 覆盖 [i*L, min((i+1)*L, total))，尾片可短。"""
        if not 0 <= index < self.num_pieces:
            raise AssembleError(f"piece 索引越界: {index}")
        start = index * self.piece_length
        return min(self.piece_length, self.total_length - start)


def _require_int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssembleError(f"{what} 必须是整数")
    return value


def load_torrent(path: str) -> TorrentMeta:
    """从磁盘读取并校验 torrent 文件，返回结构化元数据。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise AssembleError(f"无法读取 torrent 文件 {path}: {exc}") from exc

    try:
        root = bencode.decode(raw)
    except bencode.BencodeError as exc:
        raise AssembleError(f"torrent 不是合法 bencode: {exc}") from exc

    if not isinstance(root, dict) or b"info" not in root:
        raise AssembleError("torrent 顶层必须是包含 info 的字典")
    info = root[b"info"]
    if not isinstance(info, dict):
        raise AssembleError("info 必须是字典")

    unknown = set(info) - _ALLOWED_INFO_KEYS
    if unknown:
        raise AssembleError(
            "info 含本工具不支持的键: "
            + ", ".join(sorted(k.decode("latin1") for k in unknown))
        )
    if b"length" in info:
        raise AssembleError("不支持单文件模式（info.length），仅支持多文件 files 模式")
    missing = _ALLOWED_INFO_KEYS - set(info)
    if missing:
        raise AssembleError(
            "info 缺少必需键: "
            + ", ".join(sorted(k.decode("latin1") for k in missing))
        )

    name = validate_label(info[b"name"])
    piece_length = _require_int(info[b"piece length"], "info['piece length']")
    if piece_length <= 0:
        raise AssembleError("piece length 必须为正整数")

    pieces_blob = info[b"pieces"]
    if not isinstance(pieces_blob, bytes) or len(pieces_blob) % _SHA1_LEN != 0:
        raise AssembleError("pieces 必须是 20 字节 SHA1 顺序连接而成的字节串")
    piece_hashes = tuple(
        pieces_blob[i:i + _SHA1_LEN]
        for i in range(0, len(pieces_blob), _SHA1_LEN)
    )

    raw_files = info[b"files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise AssembleError("files 必须是非空列表")

    files: list[FileMeta] = []
    offset = 0
    for position, entry in enumerate(raw_files):
        if not isinstance(entry, dict):
            raise AssembleError(f"files[{position}] 必须是字典")
        bad_keys = set(entry) - _ALLOWED_FILE_KEYS
        if bad_keys:
            raise AssembleError(
                f"files[{position}] 含不支持的键: "
                + ", ".join(sorted(k.decode("latin1") for k in bad_keys))
            )
        if b"length" not in entry or b"path" not in entry:
            raise AssembleError(f"files[{position}] 必须同时含 length 与 path")

        length = _require_int(entry[b"length"], f"files[{position}].length")
        if length < 0:
            raise AssembleError(f"files[{position}].length 不能为负")
        parts = tuple(
            validate_relative_path(entry[b"path"], what=f"files[{position}]")
        )
        # 零长文件同样登记，但不消耗逻辑字节、不改变后续偏移。
        files.append(FileMeta(length=length, parts=parts, offset=offset))
        offset += length

    check_tree_conflicts([list(f.parts) for f in files])

    total_length = offset
    expected_count = (total_length + piece_length - 1) // piece_length
    if len(piece_hashes) != expected_count:
        raise AssembleError(
            f"pieces 哈希数量 {len(piece_hashes)} 与总长度 {total_length}、"
            f"piece length {piece_length} 推算的 {expected_count} 不一致"
        )

    return TorrentMeta(
        name=name,
        piece_length=piece_length,
        piece_hashes=piece_hashes,
        files=tuple(files),
        total_length=total_length,
    )
