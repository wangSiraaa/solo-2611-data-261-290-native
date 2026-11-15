#!/usr/bin/env python3
"""跑题目自带的三份清单（完整 / 缺一片 / 坏一片）并打印核验结论。

用法（在仓库根目录）::

    python3 run_demo.py
"""

from __future__ import annotations

import json
import os
import shutil

from bep3asm.assemble import run_assemble
from bep3asm.inspect import inspect_package, inspect_torrent

ROOT = os.path.dirname(os.path.abspath(__file__))
TORRENT = os.path.join(ROOT, "fixtures", "package.torrent")
SCENARIOS = [
    ("完整清单", "fixtures/pieces.json", "demo-out/full"),
    ("缺一片", "fixtures/pieces-missing.json", "demo-out/missing"),
    ("坏一片", "fixtures/pieces-corrupt.json", "demo-out/bad"),
]


def main() -> int:
    print("== inspect-torrent ==")
    print(json.dumps(inspect_torrent(TORRENT), ensure_ascii=False, indent=2))

    shutil.rmtree(os.path.join(ROOT, "demo-out"), ignore_errors=True)
    for title, pieces_json, out_rel in SCENARIOS:
        out_dir = os.path.join(ROOT, out_rel)
        print(f"\n== {title} -> {out_rel} ==")
        manifest = run_assemble(
            TORRENT, os.path.join(ROOT, pieces_json), out_dir
        )
        print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
        report = inspect_package(out_dir, TORRENT)
        print(f"inspect: complete={report['complete']} "
              f"exit_code={report['exit_code']} issues={report['issues']}")
        for entry in manifest["files"]:
            print(f"  - {entry['path']}: {entry['status']} "
                  f"coverage={entry['coverage']['covered_bytes']}/"
                  f"{entry['coverage']['length']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
