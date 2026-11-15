"""装配核心：校验 piece、按区间相交写入文件、生成 manifest。

写入策略
--------
所有非零长文件先以**完整逻辑长度**在 ``partial/`` 下建立（文件系统支持时
天然形成稀疏文件），已验证 piece 只写入与之相交的区间。处理结束后：

* 覆盖区间的并集等于文件长度 -> 移动到 ``final/`` 并标记 complete；
* 否则保留在 ``partial/``，manifest 明确记录 coverage 与未覆盖范围。

零长文件不消耗任何字节，直接在 ``final/`` 建立空文件，天然完整。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import BinaryIO

from .errors import AssembleError
from .metadata import FileMeta, TorrentMeta, load_torrent
from .piecesmap import PieceSource, load_piece_map

_MANIFEST_NAME = "manifest.json"
_FINAL_DIR = "final"
_PARTIAL_DIR = "partial"


@dataclass
class Fragment:
    """一个已验证 piece 对某个文件的一次区间贡献。"""

    piece_index: int
    piece_start: int  # 在该 piece 内的源区间（含）
    piece_end: int  # 不含
    file_start: int  # 目标文件内偏移（含）
    file_end: int  # 不含
    logical_start: int  # 全局逻辑字节流偏移（含）
    logical_end: int  # 不含

    def to_dict(self) -> dict[str, int]:
        return {
            "piece_index": self.piece_index,
            "source_piece_range": [self.piece_start, self.piece_end],
            "target_file_range": [self.file_start, self.file_end],
            "logical_range": [self.logical_start, self.logical_end],
        }


@dataclass
class PieceRecord:
    index: int
    status: str  # "verified" | "hash_mismatch" | "bad_length" | "missing"
    expected_length: int
    actual_length: int | None = None
    expected_sha1: str = ""
    actual_sha1: str | None = None
    source_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "index": self.index,
            "status": self.status,
            "expected_length": self.expected_length,
            "actual_length": self.actual_length,
            "expected_sha1": self.expected_sha1,
            "actual_sha1": self.actual_sha1,
            "source_path": self.source_path,
        }
        return data


@dataclass
class _FileState:
    meta: FileMeta
    fragments: list[Fragment] = field(default_factory=list)
    handle: BinaryIO | None = None
    partial_path: str = ""
    final_path: str = ""
    covered: list[tuple[int, int]] = field(default_factory=list)
    complete: bool = False
    sha1: str | None = None


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把若干 ``[start, end)`` 区间合并为互不相交、升序的并集。"""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _uncovered(length: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """根据已覆盖并集求未覆盖区间。"""
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in covered:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < length:
        gaps.append((cursor, length))
    return gaps


def _intersecting_files(
    files: tuple[FileMeta, ...], starts: list[int], lo: int, hi: int
) -> list[FileMeta]:
    """返回与逻辑区间 ``[lo, hi)`` 相交的所有文件（保持元数据顺序）。"""
    idx = bisect.bisect_right(starts, lo) - 1
    if idx < 0:
        idx = 0
    result: list[FileMeta] = []
    while idx < len(files):
        candidate = files[idx]
        if candidate.offset >= hi:
            break
        if candidate.length > 0 and candidate.logical_end > lo:
            result.append(candidate)
        idx += 1
    return result


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)


def _prune_empty_dirs(root: str) -> None:
    """删除 root 之下所有空目录（root 本身保留）。"""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if not dirnames and not filenames:
            os.rmdir(dirpath)


def run_assemble(
    torrent_path: str,
    pieces_json_path: str,
    out_dir: str | None = None,
) -> dict[str, object]:
    """执行完整装配流程，返回 manifest 字典并同时写入 ``out_dir``。"""
    meta = load_torrent(torrent_path)
    sources = load_piece_map(pieces_json_path, meta.num_pieces)

    if out_dir is None:
        out_dir = os.path.join(os.getcwd(), meta.name)
    if os.path.exists(out_dir):
        raise AssembleError(f"输出目录已存在，拒绝覆盖: {out_dir}")

    final_root = os.path.join(out_dir, _FINAL_DIR)
    partial_root = os.path.join(out_dir, _PARTIAL_DIR)

    # ---- 阶段一：建立工作文件（非零长文件在 partial 中按全长截断）----
    states: list[_FileState] = []
    handles: list[BinaryIO] = []
    try:
        os.makedirs(out_dir, exist_ok=False)
        os.makedirs(final_root, exist_ok=True)
        os.makedirs(partial_root, exist_ok=True)

        for file_meta in meta.files:
            state = _FileState(meta=file_meta)
            rel = os.path.join(*file_meta.parts)
            state.final_path = os.path.join(final_root, rel)
            state.partial_path = os.path.join(partial_root, rel)
            if file_meta.length == 0:
                # 零长文件不消耗字节，直接在 final 建立空文件。
                _ensure_parent(state.final_path)
                open(state.final_path, "wb").close()
                state.complete = True
                state.sha1 = hashlib.sha1(b"").hexdigest()
            else:
                _ensure_parent(state.partial_path)
                handle = open(state.partial_path, "wb")
                handle.truncate(file_meta.length)  # 支持稀疏文件的 FS 上形成空洞
                state.handle = handle
                handles.append(handle)
            states.append(state)

        starts = [f.offset for f in meta.files]
        state_by_id = {id(f.meta): f for f in states}
        piece_records: list[PieceRecord] = []

        # ---- 阶段二：逐片整体校验，通过后才允许写入任何相交区间 ----
        for index in range(meta.num_pieces):
            expected_len = meta.expected_piece_length(index)
            expected_hash = meta.piece_hashes[index]
            source = sources.get(index)

            if source is None:
                piece_records.append(
                    PieceRecord(
                        index=index,
                        status="missing",
                        expected_length=expected_len,
                        expected_sha1=expected_hash.hex(),
                    )
                )
                continue

            try:
                with open(source.path, "rb") as piece_handle:
                    data = piece_handle.read()
            except OSError as exc:
                raise AssembleError(
                    f"读取 piece {index} 原件失败 {source.path}: {exc}"
                ) from exc

            actual_hash = hashlib.sha1(data).digest()
            record = PieceRecord(
                index=index,
                status="verified",
                expected_length=expected_len,
                actual_length=len(data),
                expected_sha1=expected_hash.hex(),
                actual_sha1=actual_hash.hex(),
                source_path=source.path,
            )

            if len(data) != expected_len:
                # 坏片整体拒收：长度不对就不进入哈希写入流程。
                record.status = "bad_length"
                piece_records.append(record)
                continue
            if actual_hash != expected_hash:
                # 同样整体拒收，哪怕其中某一段“看起来正确”也不部分接收。
                record.status = "hash_mismatch"
                piece_records.append(record)
                continue

            piece_records.append(record)
            logical_lo = index * meta.piece_length
            logical_hi = logical_lo + expected_len

            for file_meta in _intersecting_files(
                meta.files, starts, logical_lo, logical_hi
            ):
                overlap_lo = max(logical_lo, file_meta.offset)
                overlap_hi = min(logical_hi, file_meta.logical_end)
                piece_start = overlap_lo - logical_lo
                piece_end = overlap_hi - logical_lo
                file_start = overlap_lo - file_meta.offset
                file_end = overlap_hi - file_meta.offset

                state = state_by_id[id(file_meta)]
                assert state.handle is not None
                state.handle.seek(file_start)
                state.handle.write(data[piece_start:piece_end])
                state.covered.append((file_start, file_end))
                state.fragments.append(
                    Fragment(
                        piece_index=index,
                        piece_start=piece_start,
                        piece_end=piece_end,
                        file_start=file_start,
                        file_end=file_end,
                        logical_start=overlap_lo,
                        logical_end=overlap_hi,
                    )
                )
    except BaseException:
        # 任何意外失败都清理半成品，保证输出目录要么不存在、要么完整。
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    for handle in handles:
        handle.close()

    # ---- 阶段三：判定完整性，完整文件移入 final，构建 manifest ----
    file_entries: list[dict[str, object]] = []
    all_complete = True
    for state in states:
        file_meta = state.meta
        if file_meta.length == 0:
            file_entries.append(
                _file_entry(state, merged=[], status="complete")
            )
            continue

        merged = merge_ranges(state.covered)
        covered_bytes = sum(end - start for start, end in merged)
        is_complete = covered_bytes == file_meta.length
        state.complete = is_complete
        all_complete = all_complete and is_complete

        if is_complete:
            _ensure_parent(state.final_path)
            shutil.move(state.partial_path, state.final_path)
            state.sha1 = _sha1_of_file(state.final_path)
            file_entries.append(
                _file_entry(state, merged=merged, status="complete")
            )
        else:
            file_entries.append(
                _file_entry(state, merged=merged, status="partial")
            )

    _prune_empty_dirs(partial_root)

    manifest: dict[str, object] = {
        "schema_version": 1,
        "torrent": {
            "name": meta.name,
            "piece_length": meta.piece_length,
            "num_pieces": meta.num_pieces,
            "total_length": meta.total_length,
        },
        "complete": all_complete,
        "summary": {
            "files_total": len(meta.files),
            "files_complete": sum(1 for s in states if s.complete),
            "pieces_total": meta.num_pieces,
            "pieces_verified": sum(
                1 for r in piece_records if r.status == "verified"
            ),
            "pieces_missing": sum(
                1 for r in piece_records if r.status == "missing"
            ),
            "pieces_bad": sum(
                1 for r in piece_records if r.status in ("bad_length", "hash_mismatch")
            ),
        },
        "pieces": [record.to_dict() for record in piece_records],
        "files": file_entries,
    }

    manifest_path = os.path.join(out_dir, _MANIFEST_NAME)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return manifest


def _sha1_of_file(path: str) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_entry(
    state: _FileState,
    merged: list[tuple[int, int]],
    status: str,
) -> dict[str, object]:
    meta = state.meta
    covered_bytes = sum(end - start for start, end in merged)
    entry: dict[str, object] = {
        "path": meta.rel_path,
        "length": meta.length,
        "status": status,
        "location": "final" if status == "complete" else "partial",
        "coverage": {
            "covered_bytes": covered_bytes,
            "length": meta.length,
            "fraction": (covered_bytes / meta.length) if meta.length else 1.0,
            "covered_ranges": [[s, e] for s, e in merged],
            "uncovered_ranges": [
                [s, e] for s, e in _uncovered(meta.length, merged)
            ],
        },
        "fragments": [fragment.to_dict() for fragment in sorted(
            state.fragments, key=lambda f: (f.piece_index, f.file_start)
        )],
        "sha1": state.sha1,
    }
    return entry
