# 重复结构列的位图量词索引

Python 3.11，仅标准库。文件：

| 文件 | 作用 |
| --- | --- |
| `bitmap_quant.py` | 索引 build / 持久化 / load 校验 / query |
| `scalar_check.py` | 独立逐元素标量解释器（仅用于交叉核对） |
| `verify.py` | 自动核对：fixtures、边界、随机对拍、损坏索引 |

## 命令

```bash
# 构建（输出 stats，写入索引）
python3 bitmap_quant.py build --rows fixtures/rows.json --index demo/index.json

# 查询（索引已持久化；原始 rows.json 可以删除）
python3 bitmap_quant.py query --index demo/index.json --queries fixtures/queries.json
python3 bitmap_quant.py query --index demo/index.json --queries fixtures/queries.json \
        --out demo/results.json --show-steps   # 附带每步域大小与 T/U/F 计数

# 全量核对（262 项检查）
python3 verify.py
```

退出码：`0` 成功；`2` 输入不合法、查询非法或索引损坏/不匹配。

## 域与编号

- **行域 R**：`row_id` 按输入顺序编号 `0..row_count-1`（`row_ids` 持久化）。
- **元素域 E**：按行顺序、行内顺序连续编号 `0..element_count-1`。
- **owner**：长度 `|E|` 的数组，`owner[e] = r`，与 `row_element_masks[r]` 互为校验。
- 每个位图都是 Python 整数，域外尾位一律被 `mask(|domain|)` 屏蔽。

## 索引格式（JSON，版本 1）

```jsonc
{
  "format": "bitmap-quant-index",
  "version": 1,
  "row_count": 7,
  "element_count": 7,
  "row_ids": ["r0", ...],                 // 行域编号 -> row_id
  "owner": [2, 3, 3, 4, 4, 5, 6],         // 元素域编号 -> 行域编号
  "row_element_masks": ["00","00","01","06","18","20","40"],
  "null_array_rows": "01",                // 行域：items == null
  "empty_array_rows": "02",               // 行域：items == []
  "kind_values": {"blue": "24", "red": "0b"},   // 元素域：精确全文
  "qty_values": {"0": "20", "1": "02", "2": "10", "9": "0c"},
  "null_kind": "50",                      // 元素域：kind is NULL
  "null_qty": "41",                       // 元素域：qty is NULL
  "stats": { ... },
  "checksum": "<sha256 of all other keys>"
}
```

位图以定宽小写十六进制保存：`width = ceil(size/8)*2` 个字符，最高尾位为 0。
`size == 0` 时为空串。域大小不同则 hex 宽度不同，读取时逐位校验无域外 1。

### 格式不变量（load 时全部检查，违反即报错）

- 顶层键集合、`format`、`version` 精确匹配；`checksum` 覆盖除自身外全部字段。
- `len(row_ids) == row_count`、`len(owner) == element_count`、owner 取值在 R 内。
- `row_element_masks`：每行一个位图，彼此不交、并集 == `mask(|E|)`。
- 每个 `row_element_masks[r]` 里的位 `e` 必须 `owner[e] == r`，且各行的位数 == `owner` 中该行的计数 == `stats.elements_per_row[r]`。
- NULL 数组行与空数组行不交，且其行位图为 0；其余非空行必须至少有一个元素。
- `kind_values` 各值位图两两不交，与 `null_kind` 不交，并集 == `mask(|E|)`；`qty_values` 同理。
- 每个 `qty_values` 的键必须是规范整数字面量（无 `+`、无前导零、`-0` 不合法）。

任何一条不满足（包括只是 `stats` 与位图不一致、或元素域与位图宽度不一致）都会抛 `IndexFileError`，CLI 返回 2。

## 三值逻辑与域检查

`Truth(domain, size, t, u)`：`t`/`u` 为不交位集，`f = mask(size) & ~(t|u)`。

- `AND`：`t = a.t & b.t`，`f = a.f | b.f`，其余 U。
- `OR`：`t = a.t | b.t`，`f = a.f & b.f`，其余 U。
- `NOT`：交换 `t` 与 `f`，U 不变。
- 复合运算先校验 `domain` 与 `size` 相同，**E 位图与 R 位图不可能被混算**。

叶谓词只在 E 求值：

- `eq(kind, text)`：`T` = 该精确文本的位图；`U` = `null_kind`；其余 `F`。
- `ge(qty, n)`：合并所有已索引且值 `>= n` 的 `qty_values` 位图得 `T`；`U` = `null_qty`；其余 `F`（不建范围树）。
- `is_null(field)`：对应 NULL 位图；无 U。

`ANY` / `ALL` 按 `owner` 映射归约到 R（只遍历索引中的行元素掩码，不读原始数组）：

- `ANY`：行内有 T 则 T；否则行内有 U 或该行是 NULL 数组则 U；否则 F（空数组 F）。
- `ALL`：行内有 F 则 F；否则行内有 U 或该行是 NULL 数组则 U；否则 T（空数组 T）。

顶层按同一三值逻辑组合 ANY/ALL 结果；`NOT` 不会把 U 翻成 T。
顶层表达式必须归约到 R，否则报 `QueryError`。

## 输出

```json
{"id": "any_same",
 "result_sets": {"TRUE": ["r4"], "FALSE": ["r1","r3","r5"], "UNKNOWN": ["r0","r2","r6"]},
 "filtered": ["r4"],
 "row_counts": {"T": 1, "U": 3, "F": 3},
 "steps": [ ... ]}
```

`TRUE`/`FALSE`/`UNKNOWN` 三集合互斥且并集为全部行；`filtered` 只含 TRUE。
`steps` 每项带 `domain`、`domain_size`、`counts`（T/U/F），可核对元素域与行域从未混用。

## fixtures 预期

`|E| = 7`（r2..r6 各 1/2/2/1/1 个元素），`|R| = 7`。

| query | TRUE | FALSE | UNKNOWN |
| --- | --- | --- | --- |
| `any_same` | r4 | r1 r3 r5 | r0 r2 r6 |
| `all_same` | r1 | r3 r4 r5 | r0 r2 r6 |
| `not_any` | r1 r3 r5 | r4 | r0 r2 r6 |
| `any_null` (`is_null(qty)`) | r2 r6 | r1 r3 r4 r5 | r0 |

关键点：r3 的 red(qty=1) 与 blue(qty=9) 是不同元素，`ANY(red AND qty>=5)` 不命中 r3；
r0 是 NULL 数组、r1 是空数组、r2 有 NULL 数量，三者分别得到 U / F / U。
