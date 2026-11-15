"""命令行：python -m jsonldrel import FILE [--db DB] / inspect DB ..."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from .api import import_document
from .inspect import Inspector


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_import(args) -> int:
    result = import_document(args.file, db_path=args.db)
    for d in result.diagnostics:
        loc = f" @ {d.pointer}" if d.pointer else ""
        print(f"{d.severity.upper():5} [{d.code}]{loc} {d.message}", file=sys.stderr)
    if not result.ok:
        print("导入被拒绝。", file=sys.stderr)
        return 1
    print(f"导入成功 sha256={result.digest[:16]}… 计数={result.counts}", file=sys.stderr)
    insp = Inspector(result.conn)
    if args.json:
        _print_json(insp.summary())
    elif not args.db:
        # 未指定落库路径时把主要表打印到 stdout，便于查看
        for table in ("nodes", "facts", "fact_sources", "lists", "list_members", "expansions"):
            print(f"== {table} ==")
            for row in result.conn.execute(f"SELECT * FROM {table}"):
                print(" ", row)
    else:
        print(f"已写入 {args.db}", file=sys.stderr)
    return 0


def cmd_inspect(args) -> int:
    conn = sqlite3.connect(args.db)
    insp = Inspector(conn)
    what = args.what
    if what == "summary":
        _print_json(insp.summary())
    elif what == "diagnostics":
        _print_json(insp.diagnostics())
    elif what == "entity":
        _print_json(insp.entity(args.arg))
    elif what == "facts":
        _print_json(insp.facts_under(args.arg or ""))
    elif what == "list":
        _print_json(insp.list_members(args.arg))
    elif what == "lists":
        _print_json(insp.lists())
    elif what == "pointers":
        _print_json(insp.pointer_map())
    elif what == "explain":
        _print_json(insp.explain(args.arg or ""))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jsonldrel", description="离线 JSON-LD 子集 → 关系事实表")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("import", help="导入 JSON-LD 文件")
    p_import.add_argument("file")
    p_import.add_argument("--db", help="SQLite 输出路径（缺省内存库并打印表内容）")
    p_import.add_argument("--json", action="store_true", help="打印摘要 JSON")
    p_import.set_defaults(func=cmd_import)

    p_inspect = sub.add_parser("inspect", help="查询已导入的 SQLite 产物")
    p_inspect.add_argument("db")
    p_inspect.add_argument("what", choices=["summary", "diagnostics", "entity", "facts", "list", "lists", "pointers", "explain"])
    p_inspect.add_argument("arg", nargs="?", default="")
    p_inspect.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
