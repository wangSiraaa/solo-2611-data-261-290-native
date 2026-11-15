"""命令行入口：

  python -m marctools.cli extract fixtures/catalog.mrc --out out/
  python -m marctools.cli inspect fixtures/catalog.mrc --record 0 --field 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extract import (
    extract_file,
    record_summary_lines,
    summary_row,
    write_csv,
    write_json,
)
from .inspect import format_inspection, inspect_file


def _cmd_extract(args: argparse.Namespace) -> int:
    result = extract_file(
        args.record_file,
        scope_path=args.scope,
    )
    if args.out:
        write_csv(result, args.out)
        write_json(result, Path(args.out) / "result.json")
        print(f"已写出 CSV/JSON 到 {args.out}")
    else:
        print(json.dumps(summary_row(result), ensure_ascii=False, indent=2))
    for line in record_summary_lines(result):
        print(line)
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    payload = inspect_file(
        args.record_file,
        args.record,
        args.field,
        scope_path=args.scope,
    )
    print(format_inspection(payload))
    return 0 if payload.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marctools",
        description="MARC21 880 替代文字互链抽取（UTF-8，不访问在线目录）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    extract_p = sub.add_parser("extract", help="输出四张基础表、配对表与诊断")
    extract_p.add_argument("record_file", type=Path)
    extract_p.add_argument("--out", type=Path, help="CSV/JSON 输出目录")
    extract_p.add_argument("--scope", type=Path, help="scope.json 路径")
    extract_p.set_defaults(func=_cmd_extract)

    inspect_p = sub.add_parser("inspect", help="按配对回查双方原始字段与 $6 证据")
    inspect_p.add_argument("record_file", type=Path)
    inspect_p.add_argument("--record", type=int, required=True, help="0 起记录序号")
    inspect_p.add_argument("--field", type=int, required=True, help="0 起字段序号")
    inspect_p.add_argument("--scope", type=Path)
    inspect_p.set_defaults(func=_cmd_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
