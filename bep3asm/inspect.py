"""只读检查：``.torrent`` 元数据概览与恢复目录一致性核验。

inspect 不联网、不启动任何 BitTorrent 客户端，也不修改磁盘内容。

恢复目录核验内容：

* manifest 结构、``complete`` 标志与逐文件状态是否自洽；
* final/partial 下文件是否齐备、长度是否等于元数据长度；
* complete 文件的整文件 SHA1 是否与 manifest 登记一致；
* partial 文件的覆盖/未覆盖区间是否与 fragments 自洽，且未覆盖区间
  里没有任何非零字节（防止零填充被误当成覆盖证据）；
* final/partial 中是否存在 manifest 未登记的多余文件；
* 若提供 ``.torrent``：按元数据字节顺序把磁盘文件重新拼成逻辑流，
  对每个标记为 verified 的 piece 重新计算整片 SHA1 比对，并核对文件
  路径、长度与顺序。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os

from .errors import AssembleError
from .metadata import FileMeta, TorrentMeta, load_torrent

_MANIFEST_NAME = "manifest.json"
_FINAL_DIR = "final"
_PARTIAL_DIR = "partial"
_BAD_PIECE_STATUSES = frozenset({"bad_length", "hash_mismatch"})


def inspect_torrent(torrent_path: str) -> dict[str, object]:
    """返回 torrent 的人类可读摘要（不做任何写操作）。"""
    meta = load_torrent(torrent_path)
    return {
        "name": meta.name,
        "piece_length": meta.piece_length,
        "num_pieces": meta.num_pieces,
        "total_length": meta.total_length,
        "files": [
            {
                "path": f.rel_path,
                "length": f.length,
                "logical_offset": f.offset,
            }
            for f in meta.files
        ],
        "piece_hashes_sha1": [h.hex() for h in meta.piece_hashes],
    }


def _read_range(path: str, start: int, end: int) -> bytes:
    with open(path, "rb") as handle:
        handle.seek(start)
        data = handle.read(end - start)
    if len(data) != end - start:
        raise AssembleError(f"文件 {path} 短读，期望 [{start}, {end})")
    return data


def _is_sorted_disjoint(ranges: list[list[int]], length: int) -> bool:
    cursor = 0
    for start, end in ranges:
        if not (isinstance(start, int) and isinstance(end, int)):
            return False
        if start < cursor or end <= start or end > length:
            return False
        cursor = end
    return True


def _check_file_entry(
    out_dir: str, entry: dict[str, object], issues: list[str]
) -> None:
    path = entry.get("path")
    length = entry.get("length")
    status = entry.get("status")
    location = entry.get("location")
    if not isinstance(path, str) or not isinstance(length, int) or length < 0:
        issues.append(f"文件条目非法: {entry!r}")
        return

    if status == "complete" and location != "final":
        issues.append(f"{path}: complete 文件必须位于 final")
    if status == "partial" and location != "partial":
        issues.append(f"{path}: partial 文件必须位于 partial")
    if status not in ("complete", "partial"):
        issues.append(f"{path}: 未知状态 {status!r}")
        return

    disk_path = os.path.join(out_dir, location, os.path.join(*path.split("/")))
    if not os.path.isfile(disk_path):
        issues.append(f"{path}: 声明位于 {location} 但磁盘上不存在")
        return
    actual_size = os.path.getsize(disk_path)
    if actual_size != length:
        issues.append(f"{path}: 磁盘长度 {actual_size} 与 manifest 长度 {length} 不符")
        return

    coverage = entry.get("coverage")
    if not isinstance(coverage, dict):
        issues.append(f"{path}: 缺少 coverage 块")
        return
    covered_ranges = coverage.get("covered_ranges")
    uncovered_ranges = coverage.get("uncovered_ranges")
    if not isinstance(covered_ranges, list) or not isinstance(uncovered_ranges, list):
        issues.append(f"{path}: coverage 区间格式非法")
        return
    if not _is_sorted_disjoint(covered_ranges, length):
        issues.append(f"{path}: covered_ranges 不是 [0, length) 内有序互斥区间")
        return

    covered_bytes = sum(e - s for s, e in covered_ranges)
    if coverage.get("covered_bytes") != covered_bytes:
        issues.append(f"{path}: covered_bytes 与区间求和不一致")
    if length == 0:
        expected_fraction = 1.0
    else:
        expected_fraction = covered_bytes / length
    if abs(float(coverage.get("fraction", -1)) - expected_fraction) > 1e-12:
        issues.append(f"{path}: coverage.fraction 与区间不一致")

    # 未覆盖区间必须真的没有可信内容：只允许零（稀疏空洞读出来也是零）。
    for start, end in uncovered_ranges:
        if any(_read_range(disk_path, start, end)):
            issues.append(
                f"{path}: 未覆盖区间 [{start}, {end}) 出现非零字节，"
                "零字节不能充当覆盖证据"
            )

    if status == "complete":
        if covered_ranges != ([[0, length]] if length else []):
            issues.append(f"{path}: 标记 complete 但覆盖区间不等于全长")
        recorded = entry.get("sha1")
        if not isinstance(recorded, str):
            issues.append(f"{path}: complete 文件缺少 sha1")
        else:
            digest = hashlib.sha1()
            with open(disk_path, "rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            if digest.hexdigest() != recorded:
                issues.append(f"{path}: 整文件 SHA1 与 manifest 登记不符")
    else:
        if covered_bytes == length:
            issues.append(f"{path}: 已全覆盖却标记为 partial")


def _collect_disk_files(root: str) -> set[str]:
    found: set[str] = set()
    if not os.path.isdir(root):
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            absolute = os.path.join(dirpath, name)
            found.add(os.path.relpath(absolute, root).replace(os.sep, "/"))
    return found


def _logical_read(
    files: tuple[FileMeta, ...],
    disk_lookup: dict[str, str],
    start: int,
    end: int,
) -> bytes:
    """按元数据顺序从磁盘文件读取逻辑字节流的 [start, end) 区间。"""
    offsets = [f.offset for f in files]
    idx = bisect.bisect_right(offsets, start) - 1
    parts: list[bytes] = []
    cursor = start
    while cursor < end:
        meta = files[idx]
        if meta.length == 0:
            idx += 1
            continue
        overlap_hi = min(end, meta.logical_end)
        file_start = cursor - meta.offset
        file_end = overlap_hi - meta.offset
        disk_path = disk_lookup[meta.rel_path]
        parts.append(_read_range(disk_path, file_start, file_end))
        cursor = overlap_hi
        idx += 1
    return b"".join(parts)


def _cross_check_torrent(
    meta: TorrentMeta,
    manifest: dict[str, object],
    disk_lookup: dict[str, str],
    issues: list[str],
) -> None:
    declared = manifest.get("files")
    if not isinstance(declared, list) or len(declared) != len(meta.files):
        issues.append("manifest 文件数量与 torrent 元数据不符")
        return
    for entry, file_meta in zip(declared, meta.files):
        if entry.get("path") != file_meta.rel_path:
            issues.append(
                f"文件路径/顺序与元数据不符: {entry.get('path')} != {file_meta.rel_path}"
            )
        if entry.get("length") != file_meta.length:
            issues.append(f"{file_meta.rel_path}: 长度与元数据不符")
        if file_meta.rel_path not in disk_lookup:
            issues.append(f"{file_meta.rel_path}: 磁盘上找不到对应文件")

    torrent_info = manifest.get("torrent")
    if isinstance(torrent_info, dict):
        if torrent_info.get("piece_length") != meta.piece_length:
            issues.append("manifest 的 piece_length 与 torrent 不符")
        if torrent_info.get("total_length") != meta.total_length:
            issues.append("manifest 的 total_length 与 torrent 不符")

    piece_entries = manifest.get("pieces")
    by_index = {p["index"]: p for p in piece_entries} if isinstance(piece_entries, list) else {}
    for index, expected_hash in enumerate(meta.piece_hashes):
        record = by_index.get(index)
        if record is None:
            issues.append(f"manifest 缺少 piece {index} 的登记")
            continue
        if record.get("status") != "verified":
            continue
        lo = index * meta.piece_length
        hi = min(lo + meta.piece_length, meta.total_length)
        try:
            data = _logical_read(meta.files, disk_lookup, lo, hi)
        except (KeyError, AssembleError, OSError) as exc:
            issues.append(f"piece {index}: 无法从磁盘重组字节: {exc}")
            continue
        if hashlib.sha1(data).digest() != expected_hash:
            issues.append(f"piece {index}: 标记 verified 但重组后 SHA1 不匹配")


def inspect_package(
    out_dir: str, torrent_path: str | None = None
) -> dict[str, object]:
    """核验恢复目录，返回报告；``exit_code`` 0/1/2 见模块文档。"""
    manifest_path = os.path.join(out_dir, _MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise AssembleError(f"恢复目录中找不到 {_MANIFEST_NAME}: {out_dir}")
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    issues: list[str] = []
    files_section = manifest.get("files")
    if not isinstance(files_section, list):
        raise AssembleError("manifest 缺少合法的 files 列表")

    declared_paths: set[str] = set()
    for entry in files_section:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            declared_paths.add(entry["path"])
        _check_file_entry(out_dir, entry, issues)

    # 多余文件检测（manifest 本身不在 final/partial 内，无需排除）。
    final_files = _collect_disk_files(os.path.join(out_dir, _FINAL_DIR))
    partial_files = _collect_disk_files(os.path.join(out_dir, _PARTIAL_DIR))
    disk_lookup: dict[str, str] = {}
    for rel in final_files:
        disk_lookup[rel] = os.path.join(out_dir, _FINAL_DIR, rel)
    for rel in partial_files:
        disk_lookup[rel] = os.path.join(out_dir, _PARTIAL_DIR, rel)
    extras = (final_files | partial_files) - declared_paths
    for rel in sorted(extras):
        issues.append(f"存在 manifest 未登记的多余文件: {rel}")

    all_marked_complete = all(
        e.get("status") == "complete" for e in files_section
    )
    if manifest.get("complete") is not all_marked_complete:
        issues.append("manifest 顶层 complete 标志与逐文件状态不一致")

    meta: TorrentMeta | None = None
    if torrent_path is not None:
        meta = load_torrent(torrent_path)
        _cross_check_torrent(meta, manifest, disk_lookup, issues)

    if issues:
        exit_code = 2
    elif manifest.get("complete") is True:
        exit_code = 0
    else:
        exit_code = 1

    return {
        "out_dir": out_dir,
        "complete": bool(manifest.get("complete")),
        "num_files": len(files_section),
        "num_complete": sum(
            1 for e in files_section if e.get("status") == "complete"
        ),
        "issues": issues,
        "exit_code": exit_code,
    }
