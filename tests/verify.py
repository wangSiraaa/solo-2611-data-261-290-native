"""独立核验脚本。

期望值由本文件内从零实现的 oracle 产生：直接对原始记录逐分组集合做
字典累加，不 import aggpipe 的任何聚合代码（仅共享 JSON 计划文件）。
随后分别用一次处理、四块处理、打乱合并顺序跑管道，按
set_id / grouping_id / 维值 / 四个聚合值逐项比对。

运行：python tests/verify.py
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aggpipe import (  # noqa: E402
    DuplicateChunkError,
    PipelineError,
    finalize_state,
    merge_states,
    partial_records,
)
from aggpipe.plan import load_plan  # noqa: E402

PLAN_PATH = ROOT / "fixtures" / "plan.json"
ROWS_PATH = ROOT / "fixtures" / "rows.json"
CHUNKS_PATH = ROOT / "fixtures" / "chunks.json"

COMPARE_FIELDS = [
    "set_id",
    "grouping_id",
    "region",
    "channel",
    "count_star",
    "count_amount",
    "sum_amount",
    "avg_amount",
]


# --------------------------------------------------------------------------- #
# 独立 oracle：与 aggpipe 完全分开的一份实现
# --------------------------------------------------------------------------- #

def _oracle_grouping_id(dims: list[str], gs: list[str]) -> int:
    gset = set(gs)
    gid = 0
    for i, name in enumerate(dims):
        if name not in gset:  # 未分组维度记 1，第一维在最高位
            gid |= 1 << (len(dims) - 1 - i)
    return gid


def _oracle_avg(total: int, nonnull: int):
    if nonnull == 0:
        return None
    g = math.gcd(abs(total), nonnull)  # 手工约分，不使用管道里的 Fraction
    num, den = total // g, nonnull // g
    if den < 0:
        num, den = -num, -den
    return f"{num}/{den}"


def oracle(plan: dict, records: list[dict]) -> list[dict]:
    """逐分组集合直接扫描原始记录，独立生成期望报表。"""
    dims = plan["dimensions"]
    rows: list[dict] = []
    for set_id, gs in enumerate(plan["grouping_sets"]):
        gid = _oracle_grouping_id(dims, gs)
        # key -> [row_count, nonnull_count, sum_int]
        groups: dict[tuple, list[int]] = {}
        for rec in records:
            key = tuple(rec[d] for d in gs)
            acc = groups.setdefault(key, [0, 0, 0])
            acc[0] += 1
            amount = rec.get("amount")
            if amount is not None:
                acc[1] += 1
                acc[2] += amount
        if not gs:  # 空总体也要有一行零计数的总计
            groups.setdefault((), [0, 0, 0])

        def sort_key(key: tuple):
            return [(0, "") if v is None else (1, v) for v in key]

        for key in sorted(groups, key=sort_key):
            row_count, nonnull, total = groups[key]
            row = {"set_id": set_id, "grouping_id": gid}
            it = iter(key)
            for dim in dims:
                row[dim] = next(it) if dim in gs else None
            row["count_star"] = row_count
            row["count_amount"] = nonnull
            row["sum_amount"] = total if nonnull > 0 else None
            row["avg_amount"] = _oracle_avg(total, nonnull)
            rows.append(row)
    return rows


def compare_rows(actual: list[dict], expected: list[dict], label: str) -> None:
    assert isinstance(actual, list) and isinstance(expected, list)
    if len(actual) != len(expected):
        raise AssertionError(
            f"[{label}] 行数不同: 实际 {len(actual)} vs 期望 {len(expected)}"
        )
    for idx, (got, want) in enumerate(zip(actual, expected)):
        for field in COMPARE_FIELDS:
            if got.get(field) != want.get(field):
                raise AssertionError(
                    f"[{label}] 第 {idx} 行字段 {field!r} 不一致: "
                    f"实际 {got.get(field)!r} vs 期望 {want.get(field)!r}\n"
                    f"实际行={got}\n期望行={want}"
                )
    print(f"  ✓ {label}: {len(expected)} 行逐项一致")


def pipeline_run(plan: dict, records: list[dict], chunk_plan: list[list[int]],
                 order: list[int] | None = None) -> list[dict]:
    """按给定分块与合并顺序跑 partial -> merge -> finalize。"""
    order = list(range(len(chunk_plan))) if order is None else list(order)
    states = []
    for idx in order:
        chunk_records = [records[j] for j in chunk_plan[idx]]
        states.append(partial_records(plan, f"chunk-{idx}", chunk_records))
    merged = merge_states(plan, states)
    return finalize_state(plan, merged)


# --------------------------------------------------------------------------- #
# 各核验场景
# --------------------------------------------------------------------------- #

def test_fixture_plan(plan: dict, records: list[dict],
                      chunk_plan: list[list[int]]) -> None:
    print("场景 1：fixtures 计划（NULL 维/全空金额组/重复分组集合/空块）")
    expected = oracle(plan, records)

    # 1a. 一次处理（单块 partial -> merge -> finalize）
    one_shot = finalize_state(
        plan, merge_states(plan, [partial_records(plan, "whole", records)])
    )
    compare_rows(one_shot, expected, "一次处理")

    # 1b. 四块处理（chunks.json 含一个空块）
    four_chunk = pipeline_run(plan, records, chunk_plan)
    compare_rows(four_chunk, expected, "四块处理（含空块）")

    # 1c. 打乱合并顺序：固定逆序/置换 + 固定种子随机洗牌
    compare_rows(
        pipeline_run(plan, records, chunk_plan, order=[3, 0, 2, 1]),
        expected,
        "打乱顺序 [3,0,2,1]",
    )
    rng = __import__("random").Random(20261115)
    shuffled = list(range(len(chunk_plan)))
    rng.shuffle(shuffled)
    compare_rows(
        pipeline_run(plan, records, chunk_plan, order=shuffled),
        expected,
        f"随机洗牌 {shuffled}",
    )

    # 1d. 重复分组集合：set_id 1 与 3 规格相同，必须各出一份
    set1 = [r for r in expected if r["set_id"] == 1]
    set3 = [r for r in expected if r["set_id"] == 3]
    assert set1 and set3 and set1 == [
        {**r, "set_id": 1} for r in set3
    ], "重复分组集合未输出两份独立结果"
    print("  ✓ 重复分组集合 [region] 保留 set_id=1 与 set_id=3 两份输出")

    # 1e. 真实 NULL 与小计省略 NULL 由 grouping_id 区分
    gid_rows = {(r["set_id"], r["grouping_id"]) for r in expected}
    assert (0, 0) in gid_rows and (1, 1) in gid_rows and (2, 3) in gid_rows
    real_null = next(
        r for r in expected
        if r["set_id"] == 0 and r["region"] is None and r["channel"] == "web"
    )
    subtotal = next(r for r in expected if r["set_id"] == 1)
    assert real_null["grouping_id"] == 0 and subtotal["grouping_id"] == 1
    assert subtotal["channel"] is None  # 小计省略，输出 NULL 但身份不同
    print("  ✓ 真实 NULL 维值（gid=0）与小计省略维（gid=1/3）身份可区分")


def test_empty_population(plan: dict) -> None:
    print("场景 2：完全空的总体")
    expected = oracle(plan, [])
    # 只有 [] 分组集合产出一行零计数
    assert len(expected) == 1 and expected[0]["set_id"] == 2
    assert expected[0]["grouping_id"] == 3
    assert expected[0]["count_star"] == 0
    assert expected[0]["count_amount"] == 0
    assert expected[0]["sum_amount"] is None
    assert expected[0]["avg_amount"] is None

    # 2a. 零个状态合并（merge 空列表）
    rows = finalize_state(plan, merge_states(plan, []))
    compare_rows(rows, expected, "零状态合并")

    # 2b. 单个空块、多个空块合并（空块有身份，[] 空状态只是零元）
    states = [partial_records(plan, f"empty-{i}", []) for i in range(3)]
    merged = merge_states(plan, states)
    assert merged["chunk_count"] == 3
    rows = finalize_state(plan, merged)
    compare_rows(rows, expected, "三个空块合并")
    print("  ✓ 空总体仅 [] 产出 count=0、sum/avg=NULL 的一行")


def test_duplicate_chunk_rejected(plan: dict, records: list[dict]) -> None:
    print("场景 3：重复 chunk_id 必须拒绝")
    s1 = partial_records(plan, "dup", records[:2])
    s2 = partial_records(plan, "dup", records[2:])  # 同 ID 不同内容
    s3 = partial_records(plan, "other", records[2:])
    s4 = partial_records(plan, "dup", records[:2])  # 同 ID 同内容

    for label, states in (
        ("同 ID 不同内容", [s1, s2]),
        ("同 ID 同内容", [s1, s4]),
        ("三块中夹带重复", [s1, s3, s4]),
    ):
        try:
            merge_states(plan, states)
        except DuplicateChunkError as exc:
            print(f"  ✓ {label} 被拒绝: {exc}")
        else:
            raise AssertionError(f"{label} 未被拒绝")


def test_plan_mismatch(plan: dict, records: list[dict]) -> None:
    print("场景 4：计划摘要不一致拒绝合并")
    good = partial_records(plan, "ok", records)
    tampered = partial_records(plan, "bad", records)
    tampered["plan"]["aggregates"] = ["count_star"]
    tampered["plan_fingerprint"] = "x" * 64
    try:
        merge_states(plan, [good, tampered])
    except PipelineError as exc:
        print(f"  ✓ 计划不一致被拒绝: {exc}")
    else:
        raise AssertionError("计划不一致的状态未被拒绝")


def test_zero_sum_vs_all_null(plan: dict) -> None:
    print("场景 5：无是非空金额 vs 非空合计为零")
    records = [
        {"id": "z1", "region": "east", "channel": "web", "amount": 5},
        {"id": "z2", "region": "east", "channel": "web", "amount": -5},
        {"id": "n1", "region": "east", "channel": "store", "amount": None},
        {"id": "n2", "region": "east", "channel": "store", "amount": None},
    ]
    expected = oracle(plan, records)
    actual = finalize_state(
        plan, merge_states(plan, [partial_records(plan, "z", records)])
    )
    compare_rows(actual, expected, "零和/全空混合数据")

    web = next(r for r in actual if r["set_id"] == 0 and r["channel"] == "web")
    store = next(r for r in actual if r["set_id"] == 0 and r["channel"] == "store")
    assert web["count_amount"] == 2 and web["sum_amount"] == 0
    assert web["avg_amount"] == "0/1"
    assert store["count_amount"] == 0 and store["sum_amount"] is None
    assert store["avg_amount"] is None and store["count_star"] == 2
    print("  ✓ 合计为零(sum=0,count=2,avg=0/1) 与全 NULL(sum=NULL,count=0) 可区分")


def test_cli_end_to_end(plan: dict, records: list[dict],
                        chunk_plan: list[list[int]]) -> None:
    print("场景 6：CLI 端到端（python -m aggpipe）")
    expected = oracle(plan, records)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        state_files = []
        for idx, positions in enumerate(chunk_plan):
            chunk_file = tmp / f"records-{idx}.json"
            state_file = tmp / f"state-{idx}.json"
            chunk_file.write_text(
                json.dumps([records[j] for j in positions], ensure_ascii=False),
                encoding="utf-8",
            )
            subprocess.run(
                [sys.executable, "-m", "aggpipe", "partial",
                 "--plan", str(PLAN_PATH), "--records", str(chunk_file),
                 "--chunk-id", f"chunk-{idx}", "--out", str(state_file)],
                check=True, env=env, cwd=ROOT,
            )
            state_files.append(state_file)

        merged_file = tmp / "merged.json"
        subprocess.run(
            [sys.executable, "-m", "aggpipe", "merge",
             "--plan", str(PLAN_PATH),
             "--states", *map(str, state_files),
             "--out", str(merged_file)],
            check=True, env=env, cwd=ROOT,
        )
        result_file = tmp / "result.json"
        subprocess.run(
            [sys.executable, "-m", "aggpipe", "finalize",
             "--plan", str(PLAN_PATH), "--state", str(merged_file),
             "--out", str(result_file)],
            check=True, env=env, cwd=ROOT,
        )
        compare_rows(json.loads(result_file.read_text(encoding="utf-8")),
                     expected, "CLI 四块流水线")

        # 重复 chunk_id：CLI 必须以非零码退出
        dup = subprocess.run(
            [sys.executable, "-m", "aggpipe", "merge",
             "--plan", str(PLAN_PATH),
             "--states", str(state_files[0]), str(state_files[0]),
             "--out", str(tmp / "dup.json")],
            env=env, cwd=ROOT, capture_output=True, text=True,
        )
        assert dup.returncode == 2, f"重复 ID 未被 CLI 拒绝: {dup.stdout}"
        assert "重复的 chunk_id" in dup.stderr
        print(f"  ✓ CLI 拒绝重复 chunk_id（退出码 2）: {dup.stderr.strip()}")


def write_expected_doc(plan: dict, records: list[dict]) -> None:
    """把 oracle 对 fixtures 的规范结果渲染成交付文档。"""
    expected = oracle(plan, records)
    header = (
        "| set_id | grouping_id | region | channel | count_star | "
        "count_amount | sum_amount | avg_amount |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
    )
    lines = [header]
    for r in expected:
        def cell(v):
            return "NULL" if v is None else str(v)
        lines.append(
            f"| {r['set_id']} | {r['grouping_id']} | {cell(r['region'])} | "
            f"{cell(r['channel'])} | {r['count_star']} | {r['count_amount']} | "
            f"{cell(r['sum_amount'])} | {cell(r['avg_amount'])} |\n"
        )
    doc = ROOT / "docs" / "EXPECTED_RESULTS.md"
    doc.write_text(
        "# 规范结果（fixtures/plan.json + fixtures/rows.json）\n\n"
        "期望值由 tests/verify.py 中独立 oracle 直接扫描原始记录生成；\n"
        "NULL 表示 JSON null；avg_amount 为最简分数 `分子/分母`，"
        "分母为零（无是非空金额）时为 NULL。\n\n" + "".join(lines) +
        "\n共 " + str(len(expected)) + " 行；一次处理、四块处理、"
        "打乱合并顺序的管道输出必须与本表逐项一致。\n",
        encoding="utf-8",
    )
    print(f"  ✓ 写出 {doc.relative_to(ROOT)}（{len(expected)} 行）")


def main() -> int:
    plan = load_plan(str(PLAN_PATH))
    records = json.loads(ROWS_PATH.read_text(encoding="utf-8"))
    chunk_plan = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
    # 分块位置必须互不重叠且覆盖全部记录
    flat = [j for chunk in chunk_plan for j in chunk]
    assert sorted(flat) == list(range(len(records))), "chunks.json 分块定义有误"
    assert any(len(chunk) == 0 for chunk in chunk_plan), "夹具缺少空块"

    test_fixture_plan(plan, records, chunk_plan)
    test_empty_population(plan)
    test_duplicate_chunk_rejected(plan, records)
    test_plan_mismatch(plan, records)
    test_zero_sum_vs_all_null(plan)
    test_cli_end_to_end(plan, records, chunk_plan)
    write_expected_doc(plan, records)

    print("\n全部核验通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
