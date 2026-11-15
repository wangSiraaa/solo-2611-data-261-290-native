"""assemble/inspect 端到端测试。

所有用例合成数据均远小于 1MiB，覆盖：
一片跨文件、一个文件跨多片、零长文件、短尾片、缺片、坏片、
限同子集的长度/顺序/piece 长度/目录名变化，以及全部写入前拒绝规则。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from bep3asm import bencode
from bep3asm.assemble import merge_ranges, run_assemble
from bep3asm.errors import AssembleError
from bep3asm.inspect import inspect_package, inspect_torrent
from bep3asm.metadata import load_torrent

from helpers import (
    build_pieces,
    reference_assemble,
    write_torrent,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "fixtures")


def _read_bytes(*parts: str) -> bytes:
    with open(os.path.join(*parts), "rb") as handle:
        return handle.read()


class FixtureScenarioTest(unittest.TestCase):
    """题目自带的三份清单：完整 / 缺一片 / 坏一片。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="bep3-fixture-")
        self.meta = load_torrent(os.path.join(FIXTURES, "package.torrent"))

    def _run(self, json_name: str, out_name: str) -> dict[str, object]:
        out_dir = os.path.join(self.tmp, out_name)
        return run_assemble(
            os.path.join(FIXTURES, "package.torrent"),
            os.path.join(FIXTURES, json_name),
            out_dir,
        )

    def test_full_package_is_complete(self) -> None:
        manifest = self._run("pieces.json", "full")
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["summary"]["files_complete"], 4)
        out_dir = os.path.join(self.tmp, "full")
        # 独立按已知原件拼接，逐字节核对 final 文件。
        stream = b"".join(
            _read_bytes(FIXTURES, "pieces", f"chunk-{i}.bin")
            for i in range(3)
        )
        offset = 0
        for file_meta in self.meta.files:
            path = os.path.join(out_dir, "final", *file_meta.parts)
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(_read_bytes(path), stream[offset:offset + file_meta.length])
            offset += file_meta.length
        # partial 目录保留但为空。
        self.assertEqual(os.listdir(os.path.join(out_dir, "partial")), [])
        report = inspect_package(out_dir, os.path.join(FIXTURES, "package.torrent"))
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["issues"], [])

    def test_missing_one_piece_only_hurts_intersecting_files(self) -> None:
        manifest = self._run("pieces-missing.json", "missing")
        self.assertFalse(manifest["complete"])
        statuses = {f["path"]: f["status"] for f in manifest["files"]}
        # piece1 缺失只影响 b.bin 与 tail.txt；a.txt 与零长文件不受牵连。
        self.assertEqual(
            statuses,
            {"a.txt": "complete", "empty": "complete",
             "sub/b.bin": "partial", "tail.txt": "partial"},
        )
        piece1 = manifest["pieces"][1]
        self.assertEqual(piece1["status"], "missing")
        self.assertIsNone(piece1["actual_sha1"])
        out_dir = os.path.join(self.tmp, "missing")
        b_bin = _read_bytes(out_dir, "partial/sub/b.bin")
        tail = _read_bytes(out_dir, "partial/tail.txt")
        # 缺片区间是空洞零字节，且未被计入覆盖。
        self.assertEqual(b_bin, b"01234" + b"\x00" * 5)
        self.assertEqual(tail, b"\x00\x00\x00!")
        report = inspect_package(out_dir, os.path.join(FIXTURES, "package.torrent"))
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["issues"], [])

    def test_corrupt_piece_is_wholly_rejected(self) -> None:
        manifest = self._run("pieces-corrupt.json", "bad")
        self.assertFalse(manifest["complete"])
        record = manifest["pieces"][1]
        self.assertEqual(record["status"], "hash_mismatch")
        # 坏片不得部分接收：两个相交文件都不含 piece1 的任何片段。
        for entry in manifest["files"]:
            for fragment in entry["fragments"]:
                self.assertNotEqual(fragment["piece_index"], 1)
        out_dir = os.path.join(self.tmp, "bad")
        # tail.txt 只保留来自 piece2（短尾片）的最后 1 字节。
        tail = _read_bytes(out_dir, "partial/tail.txt")
        self.assertEqual(tail, b"\x00\x00\x00!")
        report = inspect_package(out_dir, os.path.join(FIXTURES, "package.torrent"))
        self.assertEqual(report["exit_code"], 1)

    def test_output_dir_must_not_preexist(self) -> None:
        out_dir = os.path.join(self.tmp, "exists")
        os.makedirs(out_dir)
        with self.assertRaises(AssembleError):
            self._run("pieces.json", "exists")


class SyntheticLayoutTest(unittest.TestCase):
    """合成布局：强制出现跨文件片、多片文件、零长文件与短尾片。"""

    L = 7
    # 总长 23，L=7 -> 4 片，最后一片长 2；含两个零长文件。
    FILES = [
        ("a/one.dat", 6),    # 跨 piece0/piece1
        ("a/empty1", 0),     # 零长，不消耗偏移
        ("b/two.dat", 9),    # 跨 piece1/piece2
        ("c/three.dat", 8),  # 跨 piece2/piece3（尾片）
        ("empty2", 0),       # 末尾零长文件
    ]

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="bep3-syn-")
        self.total = sum(n for _, n in self.FILES)
        # 确定性可打印 payload。
        self.payload = bytes((i * 37 + 11) % 251 + 5 for i in range(self.total))
        write_torrent(self.tmp, self.FILES, self.L, name="synpkg")
        self.json_path, _ = build_pieces(
            self.tmp, self.FILES, self.payload, self.L, name_hint="blob"
        )
        self.torrent_path = os.path.join(self.tmp, "package.torrent")

    def _assemble(self, out_name: str) -> tuple[str, dict[str, object]]:
        out_dir = os.path.join(self.tmp, out_name)
        manifest = run_assemble(self.torrent_path, self.json_path, out_dir)
        return out_dir, manifest

    def test_full_layout_against_independent_reference(self) -> None:
        num = (self.total + self.L - 1) // self.L
        out_dir, manifest = self._assemble("full")
        self.assertTrue(manifest["complete"])
        expected = reference_assemble(
            self.FILES, self.payload, self.L, set(range(num))
        )
        for rel_path, length in self.FILES:
            disk = _read_bytes(out_dir, "final", *rel_path.split("/"))
            self.assertEqual(disk, expected[rel_path]["bytes"])
            self.assertEqual(len(disk), length)
        # inspect 重组逻辑流复核所有 piece 哈希。
        report = inspect_package(out_dir, self.torrent_path)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["exit_code"], 0)

    def test_piece_spanning_two_files_is_recorded_twice(self) -> None:
        out_dir, manifest = self._assemble("full2")
        by_path = {f["path"]: f for f in manifest["files"]}
        # piece0 = 逻辑 [0,7)：one.dat 占 [0,6)，two.dat 占 [6,7)。
        one = by_path["a/one.dat"]
        two = by_path["b/two.dat"]
        self.assertIn(
            {"piece_index": 0, "source_piece_range": [0, 6],
             "target_file_range": [0, 6], "logical_range": [0, 6]},
            one["fragments"],
        )
        self.assertIn(
            {"piece_index": 0, "source_piece_range": [6, 7],
             "target_file_range": [0, 1], "logical_range": [6, 7]},
            two["fragments"],
        )

    def test_one_file_spans_multiple_pieces(self) -> None:
        _, manifest = self._assemble("full3")
        two = next(f for f in manifest["files"] if f["path"] == "b/two.dat")
        indices = sorted({fr["piece_index"] for fr in two["fragments"]})
        # two.dat 逻辑区间 [6,15)，与 piece0/1/2 都相交。
        self.assertEqual(indices, [0, 1, 2])

    def test_short_tail_piece_length(self) -> None:
        _, manifest = self._assemble("full4")
        tail_record = manifest["pieces"][-1]
        self.assertEqual(tail_record["expected_length"], 2)
        self.assertEqual(tail_record["status"], "verified")

    def test_zero_length_files_complete_independently(self) -> None:
        _, manifest = self._assemble("full5")
        for rel in ("a/empty1", "empty2"):
            entry = next(f for f in manifest["files"] if f["path"] == rel)
            self.assertEqual(entry["status"], "complete")
            self.assertEqual(entry["coverage"]["covered_bytes"], 0)
            self.assertEqual(entry["coverage"]["uncovered_ranges"], [])
            self.assertEqual(entry["sha1"], hashlib.sha1(b"").hexdigest())

    def test_missing_middle_piece_matches_reference(self) -> None:
        # 重新构造一份缺 piece1 的映射。
        omit_dir = os.path.join(self.tmp, "omit")
        os.makedirs(omit_dir, exist_ok=True)
        write_torrent(omit_dir, self.FILES, self.L, name="synpkg")
        omit_json, _ = build_pieces(
            omit_dir, self.FILES, self.payload, self.L, omit={1}, name_hint="x"
        )
        out_dir = os.path.join(self.tmp, "out-omit")
        manifest = run_assemble(
            os.path.join(omit_dir, "package.torrent"), omit_json, out_dir
        )
        num = (self.total + self.L - 1) // self.L
        expected = reference_assemble(
            self.FILES, self.payload, self.L, set(range(num)) - {1}
        )
        self.assertFalse(manifest["complete"])
        for entry in manifest["files"]:
            rel = entry["path"]
            want = expected[rel]
            got_ranges = [tuple(r) for r in entry["coverage"]["covered_ranges"]]
            self.assertEqual(got_ranges, want["covered"], rel)
            self.assertEqual(entry["status"], "complete" if want["complete"] else "partial")
            disk = _read_bytes(out_dir, entry["location"], *rel.split("/"))
            self.assertEqual(disk, want["bytes"], rel)
        report = inspect_package(
            out_dir, os.path.join(omit_dir, "package.torrent")
        )
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["issues"], [])

    def test_corrupt_piece_matches_reference_and_keeps_holes(self) -> None:
        corrupt_dir = os.path.join(self.tmp, "corrupt")
        os.makedirs(corrupt_dir, exist_ok=True)
        write_torrent(corrupt_dir, self.FILES, self.L, name="synpkg")
        corrupt_json, _ = build_pieces(
            corrupt_dir, self.FILES, self.payload, self.L,
            corrupt={2}, name_hint="y",
        )
        out_dir = os.path.join(self.tmp, "out-corrupt")
        manifest = run_assemble(
            os.path.join(corrupt_dir, "package.torrent"), corrupt_json, out_dir
        )
        num = (self.total + self.L - 1) // self.L
        expected = reference_assemble(
            self.FILES, self.payload, self.L, set(range(num)) - {2}
        )
        for entry in manifest["files"]:
            rel = entry["path"]
            disk = _read_bytes(out_dir, entry["location"], *rel.split("/"))
            self.assertEqual(disk, expected[rel]["bytes"], rel)
        self.assertEqual(manifest["pieces"][2]["status"], "hash_mismatch")

    def test_bad_length_piece_rejected(self) -> None:
        bad_dir = os.path.join(self.tmp, "badlen")
        os.makedirs(bad_dir, exist_ok=True)
        write_torrent(bad_dir, self.FILES, self.L, name="synpkg")
        bad_json, _ = build_pieces(
            bad_dir, self.FILES, self.payload, self.L,
            bad_length={1}, name_hint="z",
        )
        out_dir = os.path.join(self.tmp, "out-badlen")
        manifest = run_assemble(
            os.path.join(bad_dir, "package.torrent"), bad_json, out_dir
        )
        self.assertEqual(manifest["pieces"][1]["status"], "bad_length")
        num = (self.total + self.L - 1) // self.L
        expected = reference_assemble(
            self.FILES, self.payload, self.L, set(range(num)) - {1}
        )
        for entry in manifest["files"]:
            rel = entry["path"]
            disk = _read_bytes(out_dir, entry["location"], *rel.split("/"))
            self.assertEqual(disk, expected[rel]["bytes"], rel)


class MergeRangesTest(unittest.TestCase):
    def test_merge_and_gaps(self) -> None:
        self.assertEqual(merge_ranges([]), [])
        self.assertEqual(merge_ranges([(1, 3), (3, 5)]), [(1, 5)])
        self.assertEqual(merge_ranges([(0, 2), (6, 7), (1, 3)]), [(0, 3), (6, 7)])


class ValidationTest(unittest.TestCase):
    """写入前拒绝规则：任何一项失败都不得创建输出目录。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="bep3-val-")

    def _make(self, files: list[tuple[str, int]], payload: bytes, L: int = 8,
              name: str = "pkg") -> tuple[str, str]:
        write_torrent(self.tmp, files, L, name=name)
        json_path, _ = build_pieces(self.tmp, files, payload, L)
        return os.path.join(self.tmp, "package.torrent"), json_path

    def _assert_no_output(self, torrent: str, json_path: str) -> None:
        out_dir = os.path.join(self.tmp, "never")
        with self.assertRaises(AssembleError):
            run_assemble(torrent, json_path, out_dir)
        self.assertFalse(os.path.exists(out_dir))

    def test_duplicate_index_rejected(self) -> None:
        files = [("a", 10), ("b", 6)]
        payload = b"k" * 16
        torrent, _ = self._make(files, payload)
        dup = os.path.join(self.tmp, "dup.json")
        with open(dup, "w", encoding="utf-8") as handle:
            json.dump([
                {"index": 0, "path": "pieces/blob-000.bin"},
                {"index": 0, "path": "pieces/blob-001.bin"},
            ], handle)
        self._assert_no_output(torrent, dup)

    def test_out_of_range_index_rejected(self) -> None:
        files = [("a", 10)]
        payload = b"k" * 10
        torrent, _ = self._make(files, payload, L=8)
        bad = os.path.join(self.tmp, "oor.json")
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump([
                {"index": 0, "path": "pieces/blob-000.bin"},
                {"index": 5, "path": "pieces/blob-001.bin"},
            ], handle)
        self._assert_no_output(torrent, bad)

    def test_negative_index_rejected(self) -> None:
        files = [("a", 4)]
        torrent, _ = self._make(files, b"abcd", L=8)
        bad = os.path.join(self.tmp, "neg.json")
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump([{"index": -1, "path": "pieces/blob-000.bin"}], handle)
        self._assert_no_output(torrent, bad)

    def test_unsafe_path_components_rejected(self) -> None:
        payload = b"abcd"
        unsafe_lists = [
            [("../escape", 4)],
            [("a/./b", 4)],
            [("a/b", 2), ("a/b/c", 2)],       # 文件/目录前缀冲突
            [("same", 2), ("same", 2)],        # 重复完整路径
            [("ab/nul\x00x", 4)],
            [("a/b", 2), ("a", 2)],            # 反向前缀冲突
        ]
        for files in unsafe_lists:
            write_torrent(self.tmp, files, 8, name="pkg")
            build_pieces(self.tmp, files, payload, 8)
            with self.assertRaises(AssembleError):
                load_torrent(os.path.join(self.tmp, "package.torrent"))

    def test_unsafe_name_rejected(self) -> None:
        for bad_name in ("..", ".", "a/b", "x\\y", "with\x00nul", ""):
            write_torrent(self.tmp, [("a", 3)], 8, name=bad_name)
            build_pieces(self.tmp, [("a", 3)], b"abc", 8)
            with self.assertRaises(AssembleError):
                load_torrent(os.path.join(self.tmp, "package.torrent"))

    def test_single_file_mode_rejected(self) -> None:
        info = {
            b"name": b"single", b"piece length": 8,
            b"pieces": hashlib.sha1(b"abcdefgh").digest(),
            b"length": 8,
        }
        path = os.path.join(self.tmp, "single.torrent")
        with open(path, "wb") as _h:
            _h.write(bencode.encode({b"info": info}))
        with self.assertRaises(AssembleError):
            load_torrent(path)

    def test_negative_file_length_rejected(self) -> None:
        info = {
            b"name": b"p", b"piece length": 8, b"pieces": b"",
            b"files": [{b"length": -1, b"path": [b"a"]}],
        }
        path = os.path.join(self.tmp, "neglen.torrent")
        with open(path, "wb") as _h:
            _h.write(bencode.encode({b"info": info}))
        with self.assertRaises(AssembleError):
            load_torrent(path)

    def test_pieces_hash_count_mismatch_rejected(self) -> None:
        info = {
            b"name": b"p", b"piece length": 8,
            b"pieces": hashlib.sha1(b"x").digest(),  # 1 个哈希
            b"files": [{b"length": 16, b"path": [b"a"]}],  # 却需要 2 个
        }
        path = os.path.join(self.tmp, "hm.torrent")
        with open(path, "wb") as _h:
            _h.write(bencode.encode({b"info": info}))
        with self.assertRaises(AssembleError):
            load_torrent(path)

    def test_boolean_length_rejected(self) -> None:
        # bencode 没有布尔字面量；直接验证元数据解析器的整数守卫拒绝 bool。
        from bep3asm.metadata import _require_int
        with self.assertRaises(AssembleError):
            _require_int(True, "test")
        # 普通整数（i1e）走完整解析路径仍应被接受为合法 length。
        info = {
            b"name": b"p", b"piece length": 8,
            b"pieces": hashlib.sha1(b"x").digest(),
            b"files": [{b"length": 1, b"path": [b"a"]}],
        }
        path = os.path.join(self.tmp, "int.torrent")
        with open(path, "wb") as _h:
            _h.write(bencode.encode({b"info": info}))
        self.assertEqual(load_torrent(path).files[0].length, 1)


class AllZeroTorrentTest(unittest.TestCase):
    """总长度为 0：只有零长文件，不产生任何 piece。"""

    def test_zero_total(self) -> None:
        tmp = tempfile.mkdtemp(prefix="bep3-zero-")
        files = [("x", 0), ("dir/y", 0), ("z", 0)]
        write_torrent(tmp, files, 16, name="nothing")
        json_path = os.path.join(tmp, "pieces.json")
        with open(json_path, "w", encoding="utf-8") as _h:
            json.dump([], _h)
        out_dir = os.path.join(tmp, "out")
        manifest = run_assemble(
            os.path.join(tmp, "package.torrent"), json_path, out_dir
        )
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["summary"]["pieces_total"], 0)
        for rel, _ in files:
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "final", *rel.split("/"))))
        report = inspect_package(out_dir, os.path.join(tmp, "package.torrent"))
        self.assertEqual(report["exit_code"], 0)


class VariantSubsetTest(unittest.TestCase):
    """限同子集的文件长度、顺序、piece 长度、合法目录名变化（均 <1MiB）。"""

    def _scenario(self, files: list[tuple[str, int]], L: int, name: str) -> None:
        tmp = tempfile.mkdtemp(prefix="bep3-var-")
        total = sum(n for _, n in files)
        payload = bytes((i * 13 + 7) % 256 for i in range(total))
        write_torrent(tmp, files, L, name=name)
        json_path, _ = build_pieces(tmp, files, payload, L, name_hint="p")
        out_dir = os.path.join(tmp, "out")
        manifest = run_assemble(
            os.path.join(tmp, "package.torrent"), json_path, out_dir
        )
        self.assertTrue(manifest["complete"])
        num = (total + L - 1) // L if total else 0
        expected = reference_assemble(files, payload, L, set(range(num)))
        for rel, _ in files:
            disk = _read_bytes(out_dir, "final", *rel.split("/"))
            self.assertEqual(disk, expected[rel]["bytes"])
        self.assertEqual(inspect_torrent(os.path.join(tmp, "package.torrent"))["name"], name)

    def test_order_and_length_variants(self) -> None:
        self._scenario([("f", 1), ("g", 100), ("h", 33)], 16, "v1")
        self._scenario([("h", 33), ("f", 1), ("g", 100)], 16, "v2")
        self._scenario([("deep/a/b/c", 250), ("d", 1)], 64, "v3")

    def test_piece_length_variants(self) -> None:
        files = [("a", 50), ("b", 50), ("c", 1)]
        for L in (1, 7, 32, 101, 500):
            self._scenario(files, L, f"L{L}")

    def test_legal_directory_label_variants(self) -> None:
        for name in ("中文目录", "with space", "a.b-c_d", "UPPER"):
            self._scenario([("a", 9), ("b", 9)], 8, name)

    def test_all_under_one_mib(self) -> None:
        # 最大规模也远小于 1MiB。
        files = [(f"f{i}", 4096) for i in range(20)]
        self._scenario(files, 32768, "bulk-small")


class CliTest(unittest.TestCase):
    def test_cli_three_scenarios_exit_codes(self) -> None:
        tmp = tempfile.mkdtemp(prefix="bep3-cli-")
        cases = [
            ("pieces.json", 0, True),
            ("pieces-missing.json", 0, False),   # assemble 本身成功退出 0
            ("pieces-corrupt.json", 0, False),
        ]
        for json_name, assemble_code, complete in cases:
            out_dir = os.path.join(tmp, json_name.replace(".", "_"))
            proc = subprocess.run(
                [sys.executable, "-m", "bep3asm", "assemble",
                 os.path.join(FIXTURES, "package.torrent"),
                 os.path.join(FIXTURES, json_name), "-o", out_dir],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, assemble_code, proc.stderr)
            check = subprocess.run(
                [sys.executable, "-m", "bep3asm", "inspect-package",
                 out_dir, "--torrent", os.path.join(FIXTURES, "package.torrent")],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(check.returncode, 0 if complete else 1, check.stdout)

    def test_cli_error_on_existing_dir(self) -> None:
        tmp = tempfile.mkdtemp(prefix="bep3-cli-err-")
        proc = subprocess.run(
            [sys.executable, "-m", "bep3asm", "assemble",
             os.path.join(FIXTURES, "package.torrent"),
             os.path.join(FIXTURES, "pieces.json"), "-o", tmp],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("已存在", proc.stderr)


class ReadOnlySourceTest(unittest.TestCase):
    def test_sources_never_modified(self) -> None:
        tmp = tempfile.mkdtemp(prefix="bep3-ro-")
        files = [("a", 10), ("b", 6)]
        payload = bytes(range(16))
        write_torrent(tmp, files, 8)
        json_path, written = build_pieces(tmp, files, payload, 8)
        before = {p: _read_bytes(p) for p in written}
        # 去掉读权限也能通过只读打开（root 例外，因此同时校验内容不变）。
        run_assemble(
            os.path.join(tmp, "package.torrent"), json_path,
            os.path.join(tmp, "out"),
        )
        for path, data in before.items():
            self.assertEqual(_read_bytes(path), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
