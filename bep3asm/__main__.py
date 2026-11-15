"""命令行入口。

用法::

    python -m bep3asm assemble fixtures/package.torrent fixtures/pieces.json [-o OUT_DIR]
    python -m bep3asm inspect-torrent fixtures/package.torrent
    python -m bep3asm inspect-package OUT_DIR [--torrent fixtures/package.torrent]
"""

from __future__ import annotations

import argparse
import json
import sys

from .assemble import run_assemble
from .errors import AssembleError
from .inspect import inspect_package, inspect_torrent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bep3asm",
        description="离线 BEP3 多文件 torrent 分片装配与检查工具（不联网）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    assemble = sub.add_parser("assemble", help="校验分片并装配恢复目录")
    assemble.add_argument("torrent", help="BEP3 多文件 .torrent 路径")
    assemble.add_argument("pieces_json", help="piece 索引到本地原件的映射 JSON")
    assemble.add_argument(
        "-o", "--out", dest="out_dir", default=None,
        help="输出目录（默认：当前目录下以 info.name 命名）；必须原本不存在",
    )

    show = sub.add_parser("inspect-torrent", help="只读查看 torrent 元数据")
    show.add_argument("torrent")

    check = sub.add_parser("inspect-package", help="核验已生成的恢复目录")
    check.add_argument("out_dir")
    check.add_argument(
        "--torrent", dest="torrent", default=None,
        help="可选：提供原始 .torrent 以重组逻辑流复核每片 SHA1",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "assemble":
            manifest = run_assemble(args.torrent, args.pieces_json, args.out_dir)
            print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
            print(f"complete={manifest['complete']}")
            return 0
        if args.command == "inspect-torrent":
            print(json.dumps(inspect_torrent(args.torrent), ensure_ascii=False, indent=2))
            return 0
        report = inspect_package(args.out_dir, args.torrent)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return int(report["exit_code"])
    except AssembleError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
