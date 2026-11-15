# 聚合状态格式（aggpipe-state/v1）

partial / merge 的中间状态都是纯 JSON 对象（UTF-8），可直接 `json.load/dump`，
也可原样存入 SQLite 的 TEXT 列；本实现默认落 JSON 文件，不使用任何数据库
聚合函数，更不使用 GROUPING SETS。

## 1. partial 状态（每个 chunk 一份）

```json
{
  "state_format": "aggpipe-state/v1",
  "plan": {
    "dimensions": ["region", "channel"],
    "grouping_sets": [["region", "channel"], ["region"], [], ["region"]],
    "aggregates": ["count_star", "count_amount", "sum_amount", "avg_amount"],
    "grouping_ids": [0, 1, 3, 1]
  },
  "plan_fingerprint": "<plan 摘要规范 JSON 的 sha256 十六进制>",
  "chunk_id": "chunk-0",
  "record_count": 2,
  "groups_by_set": [
    {
      "set_id": 0,
      "groups": [
        {
          "key": ["east", "web"],
          "row_count": 1,
          "nonnull_count": 1,
          "sum_int": 10
        }
      ]
    }
  ]
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `state_format` | 格式标识，固定 `aggpipe-state/v1`，merge/finalize 先校验 |
| `plan` | 计划摘要（维度、有序分组集合多重列表、聚合、各 set_id 的 grouping_id） |
| `plan_fingerprint` | 计划摘要的 SHA-256，merge 时与首块逐字段比对，不一致即拒绝 |
| `chunk_id` | 块身份；非空字符串。空块也有身份，同样产出合法状态 |
| `record_count` | 本块原始记录数（空块为 0） |
| `groups_by_set` | 按 set_id 排列的组表，顺序与计划的 grouping_sets 位置一一对应 |
| `groups[].key` | 组键，仅含该集合参与分组的维度值；真实 NULL 序列化为 `null` |
| `groups[].row_count` | 落入该组的全部记录数（`count_star` 的部分态） |
| `groups[].nonnull_count` | amount 非 NULL 的记录数（`count_amount` 的部分态） |
| `groups[].sum_int` | 非 NULL amount 的整数合计；无是非空金额时为 0 |

### 关键不变量

- 三项计数/求和同时保存，**绝不只存平均值**：avg 在 finalize 阶段才由
  最终的 `sum_int / nonnull_count` 约分生成，因此不会出现“平均各块平均数”。
- `nonnull_count == 0` 且 `sum_int == 0` 表示“没有非空金额”，
  与 `nonnull_count > 0` 但正负相抵（合计为零）是两种不同状态；
  单靠零总和无法区分二者，所以 `nonnull_count` 必须独立维护。
- 空分组集合 `[]` 在 partial 阶段就预置一个 `key=[]` 的零计数累加器；
  非空分组集合在空块中没有任何组。

## 2. merge 状态（全部 chunk 合并后一份）

```json
{
  "state_format": "aggpipe-state/v1",
  "plan": { "...": "与 partial 中相同的计划摘要" },
  "plan_fingerprint": "<同上>",
  "chunk_ids": ["chunk-0", "chunk-1", "chunk-2", "chunk-3"],
  "chunk_count": 4,
  "record_count": 5,
  "groups_by_set": [ { "set_id": 0, "groups": ["...同结构，值为各块之和..."] } ]
}
```

合并规则：

1. 每个待合并状态先过 `state_format` 与计划摘要/指纹校验，不一致拒绝；
2. `chunk_id` 重复即拒绝（同 ID 同内容、同 ID 不同内容都拒绝）；
3. 同 set_id、同组键的三个字段分别做整数加法——空块与 `[]` 的预置
   零计数组都是加法零元，不增加任何计数；
4. 空状态列表（完全空的总体）合法，产出的 merge 状态里只有 `[]` 的
   一个零计数组，其余 set_id 组表为空。

## 3. finalize 报表行

merge 状态物化为行列表，每行：

```json
{
  "set_id": 1,
  "grouping_id": 1,
  "region": "east",
  "channel": null,
  "count_star": 2,
  "count_amount": 1,
  "sum_amount": 10,
  "avg_amount": "10/1"
}
```

- `grouping_id`：位向量，最高位对应 `dimensions` 第一个维度，未分组记 1。
  真实 NULL 维值与小计省略维都输出 `null`，但前者 `grouping_id` 对应位为 0，
  后者为 1，身份可区分。
- `sum_amount`：`nonnull_count == 0` 时为 `null`（全 NULL 组），否则为整数。
- `avg_amount`：`Fraction(sum_int, nonnull_count)` 约分后的 `"分子/分母"`，
  分母恒为正；分母为零（无是非空金额）时为 `null`。无浮点运算。
- 排序：先按 `set_id`；组内各维 NULL 最前，其后文本按 Unicode 码点序。
- 重复的分组集合位置不去重，每个 set_id 各自产出一组行。
