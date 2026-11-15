# 263 MARC书目替代文字字段的互链关系生产

输入均为人工合成。运行环境 Python 3.11；可使用 pymarc。不需要外网账号或真实业务数据。

具体语义及允许变动范围见题面与 fixtures/scope.json。fixtures 中 bad/ 或 bad-* 文件是明确的非法/不完整输入，不能和正常文件混读。候选自行实现工具；本目录不含参考答案。

## 实现

```
marctools/reader.py   记录边界切分 + 逐记录严格 UTF-8 解码
marctools/parser.py   目录/字段/子字段解析，保留绝对字节范围
marctools/models.py   Subfield / MarcField / ParsedRecord / Diagnostic
marctools/link.py     $6 解析与唯一性配对，歧义/缺链诊断
marctools/extract.py  四张基础表、配对表、汇总与 CSV/JSON 输出
marctools/inspect.py  按配对回查双方原始字段与 $6 证据
marctools/cli.py      命令行入口
tests/                fixture 验收 + 增删字段/改编号/重排/损坏记录测试
```

核心不依赖 pymarc；pymarc 只在测试中作为独立读取库核对原字段数、字段顺序与
子字段序列。

## 运行

```bash
python3.11 -m venv .venv
.venv/bin/pip install pymarc pytest
.venv/bin/python -m marctools.cli extract fixtures/catalog.mrc --out out/
.venv/bin/python -m pytest tests/ -q
```

`extract` 不带 `--out` 时只打印清单。清单分别报告**已读 / 失败 / 孤立 880** 数量，
不会用“有配对的数量”冒充记录总数。

输出表：

| 文件 | 内容 |
|---|---|
| `out/bibliographic_records.csv` | 每条记录一行：字节范围、leader、001（重复值全保留）、字段/子字段计数、配对概况 |
| `out/fields.csv` | 每个字段**出现**一行：tag、两个 indicator、原出现序号、字节范围、`$6` 解析结果、配对字段序号 |
| `out/subfields.csv` | 每个子字段**出现**一行：code、值、出现序号、字节范围；重复 code 全部保留 |
| `out/substitute_pairings.csv` | 配对/缺反向链接/歧义候选等关系，含双方字段序号与 `$6` 证据 |
| `out/diagnostics.csv` | 缺链接、多次 `$6`、重复候选、不合法语法、孤立 `00` 等 |
| `out/record_failures.csv` | 整条记录解码/结构失败的原因与字节范围 |
| `out/summary.csv`、`out/result.json` | 汇总与全量表 |

## 回查

```bash
.venv/bin/python -m marctools.cli inspect fixtures/catalog.mrc --record 0 --field 2
```

`inspect` 接受任一字段出现（普通字段或 880 都行），输出双方字段的 tag、
indicator、字段序号、字节范围、原始字段十六进制、全部子字段、`subfield6` 原始值
及字节范围，以及配对依据（同一记录 + TAG + NN，双方各自唯一）。歧义时列出全部
候选；缺反向链接时说明缺少哪一侧。

## 语义边界

- 来源由**记录边界序号**决定；`001` 只作控制号保留，重复 `001` 不合并记录。
- 控制字段 `001/005` 直接保存文本；数据字段保存 tag、两个 indicator、原出现序号。
- 子字段按出现次序保存 code 与值，重复 code（如两个 `$x`）不放进字典覆盖。
- `$6` 只解释 `TAG-NN[/suffix]`：普通字段为 `880-NN`，880 字段为 `TAG-NN`；
  `NN` 为两位十进制，`00` 仅表示孤立 880。
- 仅在同一记录内 `TAG` 匹配且 `NN` 一致、且双方各只一个候选时配对；
  编号相同但 TAG 不同不配对；另一条记录的同编号不参与；suffix 保存但不用于猜语言。
- 配对以**字段出现身份**连接，不按子字段位置逐项对齐；两边子字段数量不同仍保留整字段配对。
- 缺反向链接、多次 `$6`、重复候选、不合法语法进入诊断；歧义候选不任选一个。
- 孤立 880、未配对普通字段、不含 `$6` 的普通字段仍完整进入基础表，只缺配对关系。
- 整条记录无法 UTF-8 解码/结构不可信时该记录失败；只要后续记录边界能由 leader
  长度或 `0x1D` 独立确认，仍继续处理。不从随机损坏字节中猜字段内容。

## 测试

`tests/test_fixture.py` 覆盖现成 fixture：跨记录重复编号、双文字书名、重复主题
子字段、`00` 孤立、歧义链接，并用 pymarc 独立核对原字段数、顺序与子字段序列。
`tests/test_links.py` 覆盖限给定子集内增减字段、改编号、重排记录、缺反向链接、
同编号不同 TAG、多次 `$6`、不合法语法、suffix、无 `$6` 字段、`00` 普通字段、
inspect 回查，以及单条记录 UTF-8 失败后后续记录仍可读。
