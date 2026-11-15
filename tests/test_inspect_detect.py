"""inspect 必须能真正发现输出目录被篡改或不自洽。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from bep3asm.assemble import run_assemble
from bep3asm.inspect import inspect_package, inspect_torrent

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "fixtures")
TORRENT = os.path.join(FIXTURES, "package.torrent")


class InspectDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="bep3-detect-")
        self.good = os.path.join(self.tmp, "good")
        run_assemble(TORRENT, os.path.join(FIXTURES, "pieces.json"), self.good)
        self.partial = os.path.join(self.tmp, "partial")
        run_assemble(
            TORRENT, os.path.join(FIXTURES, "pieces-missing.json"), self.partial
        )

    def test_clean_dirs_have_no_issues(self) -> None:
        self.assertEqual(inspect_package(self.good, TORRENT)["issues"], [])
        self.assertEqual(inspect_package(self.partial, TORRENT)["issues"], [])

    def test_tampered_final_file_detected_via_sha1(self) -> None:
        target = os.path.join(self.good, "final", "sub", "b.bin")
        with open(target, "r+b") as handle:
            handle.seek(0)
            handle.write(b"Z")
        report = inspect_package(self.good, TORRENT)
        self.assertTrue(any("SHA1" in issue for issue in report["issues"]))
        self.assertEqual(report["exit_code"], 2)

    def test_nonzero_bytes_in_uncovered_range_detected(self) -> None:
        # 往 partial 文件的未覆盖空洞写非零字节，模拟零填充冒充覆盖。
        target = os.path.join(self.partial, "partial", "tail.txt")
        with open(target, "r+b") as handle:
            handle.seek(0)
            handle.write(b"\x01")
        report = inspect_package(self.partial, TORRENT)
        self.assertTrue(any("非零字节" in issue for issue in report["issues"]))

    def test_extra_undeclared_file_detected(self) -> None:
        extra = os.path.join(self.good, "final", "rogue.bin")
        with open(extra, "wb") as handle:
            handle.write(b"x")
        report = inspect_package(self.good, TORRENT)
        self.assertTrue(any("rogue.bin" in issue for issue in report["issues"]))

    def test_complete_flag_inconsistency_detected(self) -> None:
        manifest_path = os.path.join(self.good, "manifest.json")
        manifest = json.load(open(manifest_path, encoding="utf-8"))
        manifest["complete"] = False
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        report = inspect_package(self.good, TORRENT)
        self.assertTrue(any("complete" in issue for issue in report["issues"]))

    def test_wrong_file_size_detected(self) -> None:
        target = os.path.join(self.good, "final", "a.txt")
        with open(target, "ab") as handle:
            handle.write(b"extra")
        report = inspect_package(self.good, TORRENT)
        self.assertTrue(any("长度" in issue for issue in report["issues"]))

    def test_reassembled_piece_hash_mismatch_detected(self) -> None:
        # 直接篡改 manifest 的 piece 状态（声称 verified 但磁盘重组不过哈希）。
        manifest_path = os.path.join(self.partial, "manifest.json")
        manifest = json.load(open(manifest_path, encoding="utf-8"))
        manifest["pieces"][1]["status"] = "verified"
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        report = inspect_package(self.partial, TORRENT)
        self.assertTrue(
            any("piece 1" in issue and "SHA1" in issue for issue in report["issues"])
        )

    def test_inspect_torrent_summary(self) -> None:
        summary = inspect_torrent(TORRENT)
        self.assertEqual(summary["name"], "package")
        self.assertEqual(summary["piece_length"], 8)
        self.assertEqual(summary["num_pieces"], 3)
        self.assertEqual([f["path"] for f in summary["files"]],
                         ["a.txt", "empty", "sub/b.bin", "tail.txt"])
        self.assertEqual([f["logical_offset"] for f in summary["files"]],
                         [0, 3, 3, 13])

    def test_cli_inspect_torrent(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "bep3asm", "inspect-torrent", TORRENT],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn('"name": "package"', proc.stdout)


class DictFormPiecesJsonTest(unittest.TestCase):
    """pieces.json 的字典形态与“索引不依赖文件名”语义。"""

    def test_dict_form_and_misleading_filenames(self) -> None:
        tmp = tempfile.mkdtemp(prefix="bep3-dict-")
        # 用误导性文件名复制原件：名字里的编号与实际索引故意错位。
        mapping = {}
        for real_index, chunk_name in enumerate(
            ("chunk-0.bin", "chunk-1.bin", "chunk-2.bin")
        ):
            misleading = os.path.join(tmp, f"piece-{9 - real_index}.bin")
            with open(misleading, "wb") as out, open(
                os.path.join(FIXTURES, "pieces", chunk_name), "rb"
            ) as src:
                out.write(src.read())
            mapping[str(real_index)] = misleading
        json_path = os.path.join(tmp, "pieces.json")
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(mapping, handle)
        out_dir = os.path.join(tmp, "out")
        manifest = run_assemble(TORRENT, json_path, out_dir)
        self.assertTrue(manifest["complete"])
        self.assertEqual(inspect_package(out_dir, TORRENT)["issues"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
