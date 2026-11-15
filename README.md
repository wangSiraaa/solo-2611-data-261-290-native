# 273 多粒度小计的可合并聚合状态

按 `fixtures/plan.json` 对 `region`、`channel` 两个文本维度和可空整数
`amount` 实现多粒度聚合的三阶段管道：**partial / merge / finalize**。

- Python 3.11，仅标准库（`json` / `argparse` / `fractions` / `hashlib` …）。
- 聚合核心是纯 Python 字典上的三项可结合累加，**不使用任何数据库**
  （状态是纯 JSON，可原样存入 SQLite TEXT 列，但不调用 GROUPING SETS）。
- 无 DISTINCT、无任意表达式；金额只做整数 sum/count，avg 最后由
  最终 `sum/count` 约分生成，绝不平均各块平均数。

## 目录结构

```
aggpipe/
  plan.py        计划加载/预检、grouping_id、计划摘要与指纹
  pipeline.py    partial_records / merge_states / finalize_state（聚合核心）
  cli.py         命令行入口
  __main__.py    支持 python -m aggpipe
tests/
  verify.py      独立 oracle + 全部核验场景（期望结果不复用管道代码）
scripts/
  demo.sh        端到端演示，产出 out/ 下全部文件
docs/
  STATE_FORMAT.md      状态格式规范（partial/merge/finalize）
  EXPECTED_RESULTS.md  fixtures 的规范结果（11 行，oracle 生成）
out/                     demo 产物（状态、合并态、报表、空总体结果）
```

## 语义要点（对应 fixtures/semantics.md）

- `grouping_sets` 是**有序多重列表**，位置即 `set_id`；重复的 `[region]`
  （set_id 1 与 3）不去重，各输出一份。
- `grouping_id` 是位向量：最高位对应第一维 `region`，某维未分组记 1。
  本计划四个集合依次为 `0, 1, 3, 1`。真实 NULL 维值与小计省略维在输出中
  都是 `null`，但靠 `grouping_id` 区分身份。
- 每组维护三个量：`row_count`（全部行）、`nonnull_count`（amount 非空）、
  `sum_int`（非空整数合计）。`nonnull_count==0` 即“没有非空金额”，
  此时 `sum/avg` 输出 NULL；它与“有非空金额但正负相抵合计为零”
  （`sum_amount=0, avg=0/1`）是两种状态，不能用一个零总和混淆。
- 空输入：非空分组集合不产生组；`[]` 恒产生一行
  `count=0, sum=NULL, avg=NULL`。空块也有 chunk_id 身份，其 `[]` 空状态
  在合并时只是加法零元。
- 排序：先 `set_id`，组内各维 NULL 最前、文本按 Unicode 码点序。
- merge 先校验 `state_format` 与计划摘要/指纹一致，再检查 `chunk_id`
  无重复（同 ID 同内容、同 ID 不同内容一律拒绝）；防重只针对 chunk_id。

## 命令

```bash
# 单块 partial（空块同样适用）
python3 -m aggpipe partial --plan fixtures/plan.json \
    --records out/records-0.json --chunk-id chunk-0 --out out/state-0.json

# 合并多个 partial 状态（计划摘要校验 + chunk_id 防重）
python3 -m aggpipe merge --plan fixtures/plan.json \
    --states out/state-0.json out/state-1.json out/state-2.json out/state-3.json \
    --out out/merged.json

# 物化最终报表
python3 -m aggpipe finalize --plan fixtures/plan.json \
    --state out/merged.json --out out/result.json

# 便捷命令：一次读完全部记录走完整管道
python3 -m aggpipe run --plan fixtures/plan.json \
    --records fixtures/rows.json --chunk-id whole --out out/result-one-shot.json

# 一键演示（含完全空的总体）
bash scripts/demo.sh

# 全量核验（独立 oracle、一次/四块/洗牌、空总体、重复 ID、零和区分、CLI）
python3 tests/verify.py
```

## 核验覆盖（tests/verify.py）

期望值由文件内独立 oracle 直接扫描原始记录逐集合聚合得到
（手工 gcd 约分、独立排序，不 import 管道聚合代码），再与管道输出按
`set_id / grouping_id / region / channel / 四个聚合值` 逐项比较：

1. fixtures：一次处理、四块处理（含空块）、固定置换与随机洗牌合并顺序；
   重复分组集合两份输出；真实 NULL 与小计 NULL 的 grouping_id 身份。
2. 完全空的总体：零状态合并与多个空块合并，都只剩 `[]` 的一行零计数。
3. 重复 chunk_id：同 ID 同内容、同 ID 不同内容、夹带重复均被拒绝
   （库 API 抛 `DuplicateChunkError`，CLI 退出码 2）。
4. 计划摘要/指纹不一致拒绝合并。
5. “合计为零”（sum=0,count=2,avg=0/1）与“全 NULL 金额”
   （sum=NULL,count=0,avg=NULL）区分。
6. CLI 端到端子进程跑通四块流水线并验证重复 ID 拒绝。
7. 另含 30 组随机数据（随机值、随机 1..9 分块、随机块顺序、含空总体）
   泛化比对，全部与 oracle 一致。
