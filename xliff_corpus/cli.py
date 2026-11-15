"""命令行入口。

用法：

    python -m xliff_corpus extract <file.xlf> [--scope scope.json] [-o outdir]
    python -m xliff_corpus inspect <result.json>

extract 为每个输入写出 ``<名>.corpus.json``；inspect 做 read-back 核查并
打印报告。存在失败检查或文件级致命诊断时退出码为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .extractor import extract_file
from .inspect_readback import inspect_file


def _cmd_extract(args) -> int:
    result = extract_file(args.xliff, args.scope)
    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(args.xliff))[0]
    out_path = os.path.join(args.outdir, f"{base}.corpus.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2)

    s = result.stats
    print(
        f"[extract] {args.xliff} -> {out_path}\n"
        f"  status={result.file_status} segments={s['segments_total']} "
        f"trainable={s['segments_trainable']} excluded={s['segments_excluded']} "
        f"diagnostics={s['diagnostics_total']}"
    )
    for d in result.diagnostics:
        loc = d["location"]
        print(f"  - [{d['severity']}] {d['code']}: {d['message']} @ {loc['path']}")
    return 1 if result.file_status == "failed" else 0


def _cmd_inspect(args) -> int:
    report = inspect_file(args.result)
    print(
        f"[inspect] {report['source_file']} passed={report['passed']} "
        f"checks={report['total_checks']} failed={report['failed_checks']}"
    )
    for failure in report["failures"]:
        print(f"  - FAIL {failure['name']}: {failure['detail']} @ {failure['location']}")
    return 0 if report["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xliff_corpus")
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="提取 XLIFF 为语料 JSON")
    p_extract.add_argument("xliff")
    p_extract.add_argument("--scope", default=None, help="scope.json 路径")
    p_extract.add_argument("-o", "--outdir", default="out")
    p_extract.set_defaults(func=_cmd_extract)

    p_inspect = sub.add_parser("inspect", help="对提取结果做 read-back 核查")
    p_inspect.add_argument("result")
    p_inspect.set_defaults(func=_cmd_inspect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
