"""测试辅助：合成 torrent/pieces 场景，以及独立的参考实现验证器。

参考验证器刻意不导入 bep3asm.assemble 的任何内部函数，而是直接依据
BEP3 规则重新做一遍“按元数据字节区间拼接”，用于交叉核对输出。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil

from bep3asm import bencode

SHA1 = hashlib.sha1().digest_size


def write_torrent(
    directory: str,
    files: list[tuple[str, int]],
    piece_length: int,
    name: str = "pkg",
) -> dict[str, object]:
    """在 directory 下生成一个多文件 .torrent，返回其解析前结构。"""
    info_files = []
    for rel_path, length in files:
        info_files.append(
            {b"length": length, b"path": [p.encode("utf-8") for p in rel_path.split("/")]}
        )
    total = sum(length for _, length in files)
    num_pieces = (total + piece_length - 1) // piece_length if total else 0
    # 哈希占位；调用方通常在生成 piece 原件后再用 build_pieces 重写。
    info = {
        b"name": name.encode("utf-8"),
        b"piece length": piece_length,
        b"pieces": b"\x00" * (SHA1 * num_pieces),
        b"files": info_files,
    }
    torrent = {b"info": info, b"announce": b"http://invalid.local/announce"}
    path = os.path.join(directory, "package.torrent")
    with open(path, "wb") as handle:
        handle.write(bencode.encode(torrent))
    return torrent


def logical_stream(files: list[tuple[str, int]], payload: bytes) -> bytes:
    """按元数据顺序把 payload 视作连续逻辑字节流（长度需匹配 total）。"""
    total = sum(length for _, length in files)
    assert len(payload) == total
    return payload


def build_pieces(
    base: str,
    files: list[tuple[str, int]],
    payload: bytes,
    piece_length: int,
    *,
    corrupt: set[int] | None = None,
    bad_length: set[int] | None = None,
    omit: set[int] | None = None,
    name_hint: str = "blob",
) -> tuple[str, list[str]]:
    """切分逻辑流为 piece 原件并写 pieces.json，返回 (json 路径, 原件路径列表)。"""
    corrupt = corrupt or set()
    bad_length = bad_length or set()
    omit = omit or set()
    total = len(payload)
    num = (total + piece_length - 1) // piece_length if total else 0
    piece_dir = os.path.join(base, "pieces")
    os.makedirs(piece_dir, exist_ok=True)
    hashes = b""
    mapping: list[dict[str, object]] = []
    written: list[str] = []
    for index in range(num):
        start = index * piece_length
        chunk = payload[start:min(start + piece_length, total)]
        hashes += hashlib.sha1(chunk).digest()
        if index in bad_length:
            disk_chunk = chunk + b"X"
        elif index in corrupt:
            disk_chunk = chunk[:-1] + bytes([chunk[-1] ^ 0xFF]) if chunk else b"\xff"
        else:
            disk_chunk = chunk
        rel = f"pieces/{name_hint}-{index:03d}.bin"  # 文件名与索引解耦测试靠 JSON
        with open(os.path.join(base, rel), "wb") as handle:
            handle.write(disk_chunk)
        written.append(os.path.join(base, rel))
        if index not in omit:
            mapping.append({"index": index, "path": rel})

    # 用真实哈希重写 torrent。
    torrent_path = os.path.join(base, "package.torrent")
    torrent = bencode.decode(open(torrent_path, "rb").read())
    torrent[b"info"][b"pieces"] = hashes
    with open(torrent_path, "wb") as handle:
        handle.write(bencode.encode(torrent))

    json_path = os.path.join(base, "pieces.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle)
    return json_path, written


def reference_assemble(
    files: list[tuple[str, int]],
    payload: bytes,
    piece_length: int,
    good_pieces: set[int],
) -> dict[str, dict[str, object]]:
    """纯函数参考实现：返回每个文件的期望覆盖区间与期望字节。

    完全独立地依据 BEP3 区间规则推导，不接触磁盘上的恢复目录。
    """
    total = len(payload)
    num = (total + piece_length - 1) // piece_length if total else 0
    # 已验证逻辑字节集合（用区间表示）。
    good_ranges: list[tuple[int, int]] = []
    for index in sorted(good_pieces):
        start = index * piece_length
        good_ranges.append((start, min(start + piece_length, total)))

    expected: dict[str, dict[str, object]] = {}
    offset = 0
    for rel_path, length in files:
        file_end = offset + length
        covered: list[tuple[int, int]] = []
        for lo, hi in good_ranges:
            overlap_lo = max(lo, offset)
            overlap_hi = min(hi, file_end)
            if overlap_hi > overlap_lo:
                covered.append(
                    (overlap_lo - offset, overlap_hi - offset)
                )
        # 合并。
        merged: list[tuple[int, int]] = []
        for start, end in sorted(covered):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        expected_bytes = bytearray(length)
        for start, end in merged:
            expected_bytes[start:end] = payload[offset + start:offset + end]
        expected[rel_path] = {
            "length": length,
            "covered": merged,
            "bytes": bytes(expected_bytes),
            "complete": sum(e - s for s, e in merged) == length,
        }
        offset = file_end
    return expected


def make_workspace(root: str) -> str:
    os.makedirs(root, exist_ok=True)
    return root


def reset(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
