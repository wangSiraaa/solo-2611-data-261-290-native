"""多粒度小计的可合并聚合管道：partial / merge / finalize。

仅使用 Python 3.11 标准库；聚合核心为纯 Python 字典累加，
不依赖任何数据库，也不存在 DISTINCT / GROUPING SETS 之类的捷径。
"""

from .plan import (
    AMOUNT_FIELD,
    PlanError,
    grouping_id,
    load_plan,
    plan_fingerprint,
    plan_summary,
    validate_plan,
)
from .pipeline import (
    DuplicateChunkError,
    PipelineError,
    finalize_state,
    merge_states,
    partial_records,
)

__all__ = [
    "AMOUNT_FIELD",
    "PlanError",
    "PipelineError",
    "DuplicateChunkError",
    "grouping_id",
    "load_plan",
    "plan_fingerprint",
    "plan_summary",
    "validate_plan",
    "partial_records",
    "merge_states",
    "finalize_state",
]
