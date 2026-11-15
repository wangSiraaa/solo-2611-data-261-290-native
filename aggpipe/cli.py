"""命令行入口：python -m aggpipe {partial,merge,finalize,run} ...

状态与结果全部使用 UTF-8 JSON 文件落盘，便于人工检查与跨进程传递；
状态本身是纯数据，同样可以原样写入 SQLite，本实现不依赖数据库。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .pipeline import (
    DuplicateChunkError,
    PipelineError,
    finalize_state,
    merge_states,
    partial_records,
)
from .plan import PlanError, load_plan


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _dump_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")


def _cmd_partial(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    records = _load_json(args.records)
    if not isinstance(records, list):
        raise PipelineError("records 文件必须是 JSON 数组")
    state = partial_records(plan, args.chunk_id, records)
    _dump_json(args.out, state)
    print(f"partial: chunk_id={args.chunk_id!r} 记录 {len(records)} 条 -> {args.out}")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    states = [_load_json(path) for path in args.states]
    merged = merge_states(plan, states)
    _dump_json(args.out, merged)
    print(
        f"merge: {merged['chunk_count']} 个块 "
        f"({', '.join(merged['chunk_ids'])}) 记录 {merged['record_count']} 条 "
        f"-> {args.out}"
    )
    return 0


def _cmd_finalize(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    merged = _load_json(args.state)
    rows = finalize_state(plan, merged)
    _dump_json(args.out, rows)
    print(f"finalize: {len(rows)} 行报表 -> {args.out}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """便捷命令：一次读完全部记录，走 partial -> merge -> finalize。"""
    plan = load_plan(args.plan)
    records = _load_json(args.records)
    if not isinstance(records, list):
        raise PipelineError("records 文件必须是 JSON 数组")
    state = partial_records(plan, args.chunk_id, records)
    merged = merge_states(plan, [state])
    rows = finalize_state(plan, merged)
    _dump_json(args.out, rows)
    print(f"run: 一次处理 {len(records)} 条记录，{len(rows)} 行报表 -> {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aggpipe",
        description="多粒度小计的可合并聚合（partial/merge/finalize）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("partial", help="分块聚合，输出可序列化状态")
    p.add_argument("--plan", required=True)
    p.add_argument("--records", required=True, help="本块记录的 JSON 数组文件")
    p.add_argument("--chunk-id", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=_cmd_partial)

    m = sub.add_parser("merge", help="校验并合并多个 partial 状态")
    m.add_argument("--plan", required=True)
    m.add_argument("--states", nargs="+", required=True)
    m.add_argument("--out", required=True)
    m.set_defaults(func=_cmd_merge)

    f = sub.add_parser("finalize", help="合并状态物化为最终报表")
    f.add_argument("--plan", required=True)
    f.add_argument("--state", required=True, help="merge 产出的状态文件")
    f.add_argument("--out", required=True)
    f.set_defaults(func=_cmd_finalize)

    r = sub.add_parser("run", help="便捷命令：单块一次跑完整管道")
    r.add_argument("--plan", required=True)
    r.add_argument("--records", required=True)
    r.add_argument("--chunk-id", default="one-shot")
    r.add_argument("--out", required=True)
    r.set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (PlanError, PipelineError, DuplicateChunkError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"错误: 文件不存在: {exc.filename}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
