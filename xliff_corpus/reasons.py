"""排除原因与诊断代码常量。

训练表只接受满足全部硬性条件的 segment；其余 segment 一律保留在清单
（manifest）里，并记录一个或多个结构化原因。
"""

# ---- segment 未入训练表的原因（reasons 列表取值） ----
REASON_UNIT_TRANSLATE_NO = "unit_translate_no"          # unit translate != yes
REASON_STATE_NOT_ACCEPTED = "state_not_accepted"        # state 不是 translated/final
REASON_TARGET_MISSING = "target_missing"                # 根本没有 <target> 节点
REASON_TARGET_EMPTY = "target_empty"                    # 有 <target> 但无任何内容
REASON_CODE_MISMATCH = "code_mismatch"                  # 两侧代码关系不一致（含各类代码诊断）
REASON_SOURCE_MISSING = "source_missing"                # segment 没有 source 节点

# ---- 诊断代码（diagnostics[].code） ----
# XML / 文件级（file_fatal，整个文件失败）
DIAG_XML_PARSE_ERROR = "xml_parse_error"
DIAG_ROOT_NOT_XLIFF = "root_not_xliff"
DIAG_SCOPE_NAMESPACE_MISMATCH = "scope_namespace_mismatch"
DIAG_FILE_COUNT = "file_count_not_one"
DIAG_TOO_MANY_UNITS = "too_many_units"
DIAG_DUPLICATE_UNIT_ID = "duplicate_unit_id"
DIAG_MISSING_UNIT_ID = "missing_unit_id"
DIAG_DUPLICATE_DATA_ID = "duplicate_data_id"

# unit 级（非致命警告）
DIAG_UNSUPPORTED_UNIT_CHILD = "unsupported_unit_child"

# segment 级（按片段隔离，仅令该片段不合格）
DIAG_MISSING_SEGMENT_ID = "missing_segment_id"
DIAG_DUPLICATE_SEGMENT_ID = "duplicate_segment_id"
DIAG_SOURCE_MISSING = "source_missing"
DIAG_UNSUPPORTED_INLINE_TAG = "unsupported_inline_tag"
DIAG_DUPLICATE_CODE_ID = "duplicate_code_id"
DIAG_UNBALANCED_PC = "unbalanced_pc"
DIAG_MISSING_REF_ATTR = "missing_ref_attribute"
DIAG_MISSING_DATA_REFERENCE = "missing_data_reference"
DIAG_TARGET_CODE_MISSING = "target_code_missing"
DIAG_TARGET_CODE_EXTRA = "target_code_extra"
DIAG_CODE_KIND_MISMATCH = "code_kind_mismatch"
DIAG_CODE_REF_MISMATCH = "code_ref_mismatch"
DIAG_PC_CONTAINMENT_MISMATCH = "pc_containment_mismatch"

# 任何片段级代码问题都归入训练排除原因 REASON_CODE_MISMATCH
SEGMENT_CODE_DIAGS = frozenset(
    {
        DIAG_UNSUPPORTED_INLINE_TAG,
        DIAG_DUPLICATE_CODE_ID,
        DIAG_UNBALANCED_PC,
        DIAG_MISSING_REF_ATTR,
        DIAG_MISSING_DATA_REFERENCE,
        DIAG_DUPLICATE_DATA_ID,
        DIAG_TARGET_CODE_MISSING,
        DIAG_TARGET_CODE_EXTRA,
        DIAG_CODE_KIND_MISMATCH,
        DIAG_CODE_REF_MISMATCH,
        DIAG_PC_CONTAINMENT_MISMATCH,
    }
)

# 文件级致命诊断：出现即 file_status = failed
FILE_FATAL_DIAGS = frozenset(
    {
        DIAG_XML_PARSE_ERROR,
        DIAG_ROOT_NOT_XLIFF,
        DIAG_SCOPE_NAMESPACE_MISMATCH,
        DIAG_FILE_COUNT,
        DIAG_TOO_MANY_UNITS,
        DIAG_DUPLICATE_UNIT_ID,
        DIAG_MISSING_UNIT_ID,
    }
)


REASON_TEXT = {
    REASON_UNIT_TRANSLATE_NO: "unit translate 不是 yes",
    REASON_STATE_NOT_ACCEPTED: "segment state 不在 translated/final 之内",
    REASON_TARGET_MISSING: "缺少 target 节点（与空 target 区分）",
    REASON_TARGET_EMPTY: "target 存在但为空",
    REASON_CODE_MISMATCH: "源/目标代码 id、类型、引用或 pc 包含关系不一致",
    REASON_SOURCE_MISSING: "segment 缺少 source 节点",
}
