"""聚合核心：partial（分块）、merge（合并）、finalize（出报表）。

状态是纯 JSON 可序列化对象，也可直接写入 SQLite（本题默认落 JSON 文件）。
聚合只做三项可交换、可结合的计数/求和，avg 永远在 finalize 阶段
由最终的 sum/count 生成，绝不平均各块的平均值。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Iterable

from .plan import (
    AMOUNT_FIELD,
    PlanError,
    grouping_id,
    plan_fingerprint,
    plan_summary,
)

STATE_FORMAT = "aggpipe-state/v1"

# 单条记录的合法金额范围（含端点）。
MIN_AMOUNT = -1_000_000
MAX_AMOUNT = 1_000_000


class PipelineError(ValueError):
    """管道输入不合法（记录/状态结构问题）。"""


class DuplicateChunkError(PipelineError):
    """merge 时发现重复 chunk_id（含同 ID 不同内容）。"""


# --------------------------------------------------------------------------- #
# 内部累加器
# --------------------------------------------------------------------------- #

def _empty_acc() -> dict:
    """一个组的零元状态。

    三项必须同时保留：
      row_count       全部记录数（含 amount 为 NULL 的行）；
      nonnull_count   amount 非 NULL 的记录数；
      sum_int         非 NULL amount 的整数合计（无是非空金额时为 0）。
    nonnull_count==0 且 sum_int==0 表示“没有非空金额”，
    与 nonnull_count>0 但正负相抵合计为零是两种不同状态，
    不能用一个零总和代表二者。
    """
    return {"row_count": 0, "nonnull_count": 0, "sum_int": 0}


def _seed_states(plan: dict) -> list[dict[Any, dict]]:
    """为每个 set_id 建一张组表；空分组集合 [] 预置全局唯一组。

    空输入对非空分组集合不产生任何组；对 [] 必须产生一行
    count=0、sum/avg=NULL，因此 [] 的空累加器从 partial 阶段就存在，
    合并时它只是零元，不会增加计数。
    """
    tables: list[dict[Any, dict]] = []
    for set_id in range(len(plan["grouping_sets"])):
        table: dict[Any, dict] = {}
        if not plan["grouping_sets"][set_id]:
            table[()] = _empty_acc()
        tables.append(table)
    return tables


def _validate_record(record: Any, plan: dict, index: int) -> None:
    if not isinstance(record, dict):
        raise PipelineError(f"第 {index} 条记录不是 JSON 对象")
    for dim in plan["dimensions"]:
        value = record.get(dim)
        if value is not None and not isinstance(value, str):
            raise PipelineError(
                f"第 {index} 条记录维度 {dim!r} 必须是字符串或 null"
            )
    amount = record.get(AMOUNT_FIELD)
    if amount is not None:
        # bool 是 int 的子类，显式拒绝，避免 True 被当成 1 累加。
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise PipelineError(
                f"第 {index} 条记录 amount 必须是可空整数（null/int）"
            )
        if not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
            raise PipelineError(
                f"第 {index} 条记录 amount={amount} 超出 "
                f"[{MIN_AMOUNT}, {MAX_AMOUNT}]"
            )


def _dim_sort_key(key: tuple) -> list:
    """组键排序：NULL 最前，其余文本按 Unicode 码点序。"""
    return [(0, "") if value is None else (1, value) for value in key]


def _serialize_groups(table: dict[Any, dict]) -> list[dict]:
    """组表转成确定性顺序的可序列化列表。"""
    groups = []
    for key in sorted(table.keys(), key=_dim_sort_key):
        acc = table[key]
        groups.append(
            {
                "key": list(key),
                "row_count": acc["row_count"],
                "nonnull_count": acc["nonnull_count"],
                "sum_int": acc["sum_int"],
            }
        )
    return groups


# --------------------------------------------------------------------------- #
# partial
# --------------------------------------------------------------------------- #

def partial_records(
    plan: dict, chunk_id: str, records: Iterable[dict]
) -> dict:
    """读取本 chunk 所属的全部记录一次，产出可序列化的聚合状态。

    空块同样产出带 chunk_id 的合法状态（空块也有身份），
    其中非空分组集合没有任何组，[] 保留一个零计数预置组。
    """
    if not isinstance(chunk_id, str) or not chunk_id:
        raise PipelineError("chunk_id 必须是非空字符串")
    records = list(records)
    tables = _seed_states(plan)

    for index, record in enumerate(records):
        _validate_record(record, plan, index)
        amount = record.get(AMOUNT_FIELD)
        for set_id, gs in enumerate(plan["grouping_sets"]):
            key = tuple(record[dim] for dim in gs)
            acc = tables[set_id].setdefault(key, _empty_acc())
            acc["row_count"] += 1
            if amount is not None:
                acc["nonnull_count"] += 1
                acc["sum_int"] += amount

    return {
        "state_format": STATE_FORMAT,
        "plan": plan_summary(plan),
        "plan_fingerprint": plan_fingerprint(plan),
        "chunk_id": chunk_id,
        "record_count": len(records),
        "groups_by_set": [
            {"set_id": set_id, "groups": _serialize_groups(table)}
            for set_id, table in enumerate(tables)
        ],
    }


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #

def _check_plan_compatible(state: dict, expected_summary: dict,
                           expected_fp: str) -> None:
    if state.get("state_format") != STATE_FORMAT:
        raise PipelineError(
            f"状态格式不兼容: {state.get('state_format')!r}，期望 {STATE_FORMAT}"
        )
    if state.get("plan_fingerprint") != expected_fp or state.get("plan") != expected_summary:
        raise PipelineError(
            f"chunk {state.get('chunk_id')!r} 的计划摘要与首块不一致，拒绝合并"
        )


def merge_states(plan: dict, states: Iterable[dict]) -> dict:
    """验证计划一致、chunk_id 无重复后，合并全部 partial 状态。

    空状态列表（完全空的总体）也合法：返回只含 [] 零计数预置组的
    合并状态；[] 的空状态在合并中只是零元，不增加任何计数。
    """
    states = list(states)
    expected_summary = plan_summary(plan)
    expected_fp = plan_fingerprint(plan)
    n_sets = len(plan["grouping_sets"])

    seen_chunks: set[str] = set()
    merged = _seed_states(plan)

    for state in states:
        if not isinstance(state, dict):
            raise PipelineError("待合并状态必须是对象")
        _check_plan_compatible(state, expected_summary, expected_fp)

        chunk_id = state.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            raise PipelineError("状态缺少合法 chunk_id")
        # 防重只针对 chunk_id；同 ID 不同内容同样在这里被拒绝。
        if chunk_id in seen_chunks:
            raise DuplicateChunkError(f"重复的 chunk_id: {chunk_id!r}")
        seen_chunks.add(chunk_id)

        groups_by_set = state.get("groups_by_set")
        if (not isinstance(groups_by_set, list)
                or len(groups_by_set) != n_sets):
            raise PipelineError(
                f"chunk {chunk_id!r} 的分组集合数量与计划不一致"
            )

        for entry in groups_by_set:
            if (not isinstance(entry, dict)
                    or entry.get("set_id") not in range(n_sets)
                    or not isinstance(entry.get("groups"), list)):
                raise PipelineError(
                    f"chunk {chunk_id!r} 状态结构损坏"
                )
            set_id = entry["set_id"]
            for group in entry["groups"]:
                key = tuple(group["key"])
                acc = merged[set_id].setdefault(key, _empty_acc())
                acc["row_count"] += group["row_count"]
                acc["nonnull_count"] += group["nonnull_count"]
                acc["sum_int"] += group["sum_int"]

    return {
        "state_format": STATE_FORMAT,
        "plan": expected_summary,
        "plan_fingerprint": expected_fp,
        "chunk_ids": sorted(seen_chunks),
        "chunk_count": len(seen_chunks),
        "record_count": sum(int(s.get("record_count", 0)) for s in states),
        "groups_by_set": [
            {"set_id": set_id, "groups": _serialize_groups(table)}
            for set_id, table in enumerate(merged)
        ],
    }


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #

def _avg_fraction(sum_int: int, nonnull_count: int) -> str | None:
    """avg 输出 sum/count 的最简分数字符串，分母为零（无是非空金额）为 NULL。

    分母统一取正数；整数也写成分数形式（如 10 -> "10/1"），
    保证该列类型稳定、可逐项精确比较，不引入浮点误差。
    """
    if nonnull_count == 0:
        return None
    frac = Fraction(sum_int, nonnull_count)
    return f"{frac.numerator}/{frac.denominator}"


def finalize_state(plan: dict, merged: dict) -> list[dict]:
    """把合并状态物化为最终报表行。

    每行携带 set_id 与 grouping_id：真实 NULL 维值保留 null，
    因小计被省略的维度也输出 null，但二者身份由 grouping_id 区分。
    输出先按 set_id，组内按各维 NULL 最前、文本码点序排序。
    两个相同的 grouping_sets 位置各自产出一组输出，不去重。
    """
    expected_summary = plan_summary(plan)
    expected_fp = plan_fingerprint(plan)
    _check_plan_compatible(merged, expected_summary, expected_fp)

    n_sets = len(plan["grouping_sets"])
    groups_by_set = merged.get("groups_by_set")
    if (not isinstance(groups_by_set, list)
            or len(groups_by_set) != n_sets):
        raise PipelineError("合并状态的分组集合数量与计划不一致")

    rows: list[dict] = []
    for set_id, gs in enumerate(plan["grouping_sets"]):
        gid = grouping_id(plan, set_id)
        entry = groups_by_set[set_id]
        if entry.get("set_id") != set_id or not isinstance(
            entry.get("groups"), list
        ):
            raise PipelineError(f"set_id={set_id} 的合并状态结构损坏")

        ordered = sorted(
            entry["groups"],
            key=lambda g: _dim_sort_key(tuple(g["key"])),
        )
        for group in ordered:
            key = list(group["key"])
            if len(key) != len(gs):
                raise PipelineError(
                    f"set_id={set_id} 组键宽度与分组集合不符"
                )
            # 参与分组的维度取真实值（可能为真实 NULL）；
            # 未参与分组的维度按语义补 NULL，身份由 grouping_id 标明。
            key_iter = iter(key)
            dim_values = {
                dim: (next(key_iter) if dim in gs else None)
                for dim in plan["dimensions"]
            }
            nonnull = group["nonnull_count"]
            total = group["sum_int"]
            rows.append(
                {
                    "set_id": set_id,
                    "grouping_id": gid,
                    **dim_values,
                    "count_star": group["row_count"],
                    "count_amount": nonnull,
                    # 全 NULL（无是非空金额）时 sum 为 NULL，而非 0。
                    "sum_amount": total if nonnull > 0 else None,
                    "avg_amount": _avg_fraction(total, nonnull),
                }
            )
    return rows
