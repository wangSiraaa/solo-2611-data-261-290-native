# bep3asm —— 离线 BEP3 多文件分片装配与检查

纯 Python 3.11 标准库实现（内置严格 bencode 编解码，无第三方依赖、不联网、
不启动任何 BitTorrent 客户端）。读取一个 BEP3 多文件 `.torrent` 和一份
piece 索引 → 本地原件的 `pieces.json`，逐片做 **SHA1 整片校验** 后按字节
区间相交关系装配出恢复目录与 `manifest.json`。

## 目录结构

```
bep3asm/
  bencode.py    # 严格 bencode 编解码（拒绝前导零、重复键、尾随字节等）
  metadata.py   # 解析/校验 info（仅 name/piece length/pieces/files）
  piecesmap.py  # 解析 pieces.json，索引只取自 JSON，不从文件名猜测
  paths.py      # 路径组件安全规则与文件树前缀冲突检测
  assemble.py   # 装配核心与 manifest 生成
  inspect.py    # 只读检查与按元数据重组逻辑流的交叉核验
  __main__.py   # 命令行入口
tests/          # unittest，含独立参考实现与 50 个用例
run_demo.py     # 三份 fixture 清单的端到端演示
```

## 命令行用法

```bash
# 装配（输出目录必须原本不存在）
python -m bep3asm assemble fixtures/package.torrent fixtures/pieces.json -o out/full

# 只读查看 torrent 元数据
python -m bep3asm inspect-torrent fixtures/package.torrent

# 核验恢复目录（给 --torrent 时会重组逻辑流重算每片 SHA1）
python -m bep3asm inspect-package out/full --torrent fixtures/package.torrent
```

`inspect-package` 退出码：`0` 整包完整；`1` 包不完整但目录自洽；`2` 发现
不一致/篡改。装配失败（非法输入、输出目录已存在等）退出码 `2`，且不创建
任何输出目录。

`pieces.json` 支持两种形态：

```json
[{"index": 0, "path": "pieces/chunk-0.bin"}]
```

```json
{"0": "pieces/chunk-0.bin"}
```

相对路径相对于 `pieces.json` 所在目录解析。

## 核心语义保证

* **files 组成连续逻辑字节流**：按元数据顺序拼接，零长文件登记但不消耗
  字节、不改变后续偏移。piece i 覆盖 `[i*L, min((i+1)*L, total))`，尾片
  可短；`pieces` 必须是数量等于 `ceil(total/L)` 的 20 字节 SHA1 串。
* **先整片校验、后区间可信**：每个到达 piece 先核对真实长度与整片 SHA1，
  通过后其中的区间才允许写入文件。坏片（长度错或哈希错）整体拒收，不会
  因为某一段“看起来正确”而部分接收；缺片不会零填充后标成功。
* **区间相交装配**：一个 piece 可贡献多个文件，一个文件可跨多个 piece。
  非零长文件先在 `partial/` 以完整逻辑长度建立（支持的文件系统上为稀疏
  文件，空洞读出为零），完整后移入 `final/`。
* **零字节不是覆盖证据**：manifest 逐文件登记 covered/uncovered 区间、
  来源 piece、源区间与目标区间；inspect 会检查未覆盖区间里不得出现非零字节。
* **整包 complete 仅当全部文件完整**；零长文件可独立完整；缺/坏 piece 只
  影响实际与其逻辑区间相交的文件，不牵连其他文件。
* **写入前整包拒绝**：路径组件拒绝空、NUL、`.`、`..`、`/`、`\\`、盘符与
  绝对路径；重复完整路径、文件/目录前缀冲突（如 `a/b` 与 `a/b/c`）、
  `pieces.json` 同索引重复映射或越界索引全部在创建输出目录前报错。
  `info.name` 仅作目录标签，适用同一套组件规则。
* **原件只读**：装配过程只以读方式打开原件，绝不修改，也不从文件名推断索引。

## 输出布局

```
out/
  final/        # 完整文件（含零长文件），保留元数据相对目录结构
  partial/      # 不完整文件（全长稀疏文件，仅可信区间有数据）
  manifest.json # 逐 piece 状态 + 逐文件 coverage/fragments/sha1
```

## 测试与演示

```bash
python3 -m unittest discover -s tests -v   # 50 个用例
python3 run_demo.py                        # 完整/缺一片/坏一片三场景
```

测试用独立的纯函数参考实现（`tests/helpers.py: reference_assemble`，不
复用装配代码）按元数据字节区间推导期望覆盖与字节，重点覆盖：一片跨文件、
一文件跨多片、零长文件、短尾片，以及限同子集的长度/顺序/piece 长度/目录
名变化；所有合成数据均小于 1 MiB。

## 明确不实现

tracker、磁力链接、单文件模式（`info.length`）、断点续传/下载、最优 I/O。
