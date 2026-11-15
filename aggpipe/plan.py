"""计划（plan.json）的加载、预检与摘要。

计划摘要随每个 partial 状态一起持久化，merge 时逐字段比对，
保证被合并的状态确实来自同一份聚合计划。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# 金额字段名与两个文本维度由题目固定，不支持任意表达式。
AMOUNT_FIELD = "amount"
ALLOWED_AGGREGATES = ("count_star", "count_amount", "sum_amount", "avg_amount")


class PlanError(ValueError):
    """计划结构不合法时抛出。"""


def validate_plan(plan: Any) -> dict:
    """预检计划；合法则返回规范化（深拷贝）后的计划，否则抛出 PlanError。

    规则：
    - dimensions 为 1..N 个互不重复的字符串；本题固定为 region/channel。
    - grouping_sets 是有序多重列表，每个集合内维度互不重复且必须已知；
      集合位置即 set_id，重复集合不去重。
    - aggregates 只能取自 ALLOWED_AGGREGATES（avg 由最终 sum/count 生成）。
    """
    if not isinstance(plan, dict):
        raise PlanError("plan 必须是 JSON 对象")

    dimensions = plan.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise PlanError("dimensions 必须是非空列表")
    if not all(isinstance(d, str) for d in dimensions):
        raise PlanError("dimensions 只能包含字符串维度名")
    if len(set(dimensions)) != len(dimensions):
        raise PlanError("dimensions 不允许重复维度")

    grouping_sets = plan.get("grouping_sets")
    if not isinstance(grouping_sets, list) or not grouping_sets:
        raise PlanError("grouping_sets 必须是非空列表")
    for set_id, gs in enumerate(grouping_sets):
        if not isinstance(gs, list):
            raise PlanError(f"grouping_sets[{set_id}] 必须是列表")
        if not all(isinstance(d, str) for d in gs):
            raise PlanError(f"grouping_sets[{set_id}] 只能包含字符串维度名")
        unknown = [d for d in gs if d not in dimensions]
        if unknown:
            raise PlanError(
                f"grouping_sets[{set_id}] 含未知维度: {unknown!r}"
            )
        if len(set(gs)) != len(gs):
            raise PlanError(f"grouping_sets[{set_id}] 内部维度重复")

    aggregates = plan.get("aggregates")
    if not isinstance(aggregates, list) or not aggregates:
        raise PlanError("aggregates 必须是非空列表")
    bad = [a for a in aggregates if a not in ALLOWED_AGGREGATES]
    if bad:
        raise PlanError(f"不支持的聚合: {bad!r}，仅允许 {ALLOWED_AGGREGATES}")

    return {
        "dimensions": list(dimensions),
        "grouping_sets": [list(gs) for gs in grouping_sets],
        "aggregates": list(aggregates),
    }


def load_plan(path: str) -> dict:
    """从 JSON 文件加载并预检计划。"""
    with open(path, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    return validate_plan(plan)


def grouping_id(plan: dict, set_id: int) -> int:
    """计算某分组集合位置的 grouping_id 位向量。

    最高位对应 dimensions 的第一个维度；该维未参与分组记 1。
    例如 dimensions=[region, channel]：
      [region, channel] -> 0b00 = 0；[region] -> 0b01 = 1；
      []                -> 0b11 = 3。
    """
    dimensions = plan["dimensions"]
    grouped = set(plan["grouping_sets"][set_id])
    bits = 0
    for idx, dim in enumerate(dimensions):
        if dim not in grouped:
            # 第一维占据最高位，依次向低位排列。
            bits |= 1 << (len(dimensions) - 1 - idx)
    return bits


def plan_summary(plan: dict) -> dict:
    """可序列化的计划摘要：随状态持久化，merge/finalize 时做一致性校验。"""
    return {
        "dimensions": list(plan["dimensions"]),
        "grouping_sets": [list(gs) for gs in plan["grouping_sets"]],
        "aggregates": list(plan["aggregates"]),
        "grouping_ids": [
            grouping_id(plan, i) for i in range(len(plan["grouping_sets"]))
        ],
    }


def plan_fingerprint(plan: dict) -> str:
    """计划摘要的规范 JSON SHA-256 指纹（便于快速比对与人工核对）。"""
    canonical = json.dumps(
        plan_summary(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
