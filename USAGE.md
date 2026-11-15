# protolog 使用说明

对带长度前缀的 proto2 `demo.Sample` 设备日志做入仓（ingest）与逐帧检查（inspect）。
只读输入文件；输出为 JSON 报告或终端文本。

## 命令

```bash
python3 -m protolog ingest fixtures/messages.bin              # 报告到 stdout
python3 -m protolog ingest fixtures/truncated.bin -o out/t.json
python3 -m protolog inspect fixtures/messages.bin --frame 2   # 单帧原字节+解释
python3 -m unittest discover -s tests                         # 34 个测试
```

可选参数：`--descriptor`、`--schema-proto`、`--scope`、`--message`、
`--max-payload-bytes`、`--max-frames`（默认读 fixtures/ 下对应文件与
scope.json 的 4096 / 50 上限）。

退出码：ingest 在文件完整且全部帧解码成功时为 0；存在坏 payload、截断或
帧格式错误时为 1（报告照常生成）。inspect 帧号越界为 2。

## 帧格式与处理规则

- 帧 = 4 字节大端无符号长度 + 一个 Sample payload；长度上限 4096，按文件
  先后从 0 编号。
- 完整长度帧内 payload 损坏：隔离该帧（status=bad_payload，诊断含文件
  偏移），从已知边界继续下一帧。
- 长度前缀不足 4 字节或 payload 不足声明长度：文件从该点停止
  （status=truncated），不猜下一帧起点；超长帧同理停止（framing_error）。
- `complete=false` 当且仅当存在截断/帧格式错误/超帧数限制；之前的成功行
  不代表文件完整。counts 分别统计 ok / bad_payload / truncated /
  framing_error。

## 值与 presence 语义

- 值解码用官方 protobuf 运行库：scalar 多次出现取最后值；普通 message
  字段跨 tag 合并子字段；oneof 切换清旧分支，同一 message 成员连续出现
  才合并，被替换后切回不恢复旧内容；repeated sint32 的 packed/unpacked
  按线上顺序串接。
- presence 用 HasField/WhichOneof 读回：显式零（如 `08 00`）与字段不存在
  在 presence 中不同（`["value"]` vs `[]`），values 的 JSON 不省略显式
  默认值。repeated 无 HasField，按线上是否出现（含空 packed 块 `2a 00`）
  记 presence。
- 描述符以外的合法未知字段跳过并记录字段号（不猜名字）；未知 group 内部
  的字段号无意义，只记 group 自身编号。
- 每帧输出：帧序号、长度前缀偏移、payload 字节范围、values、presence
  路径、oneof 当前成员、未知字段号、诊断；报告头部保留输入文件
  sha256/大小与 descriptor 信息。descriptor.pb 会与 schema.proto 做结构
  交叉核对（不一致记入 schema_proto_warnings，descriptor 为准）。

## 目录

- `protolog/wirescan.py` 线格式扫描器（边界校验、未知字段、inspect 走查；
  非完整解码器）
- `protolog/schema.py` descriptor.pb 加载 + schema.proto 交叉核对
- `protolog/core.py` 分帧、ingest、presence 提取、inspect
- `protolog/cli.py` 命令行
- `tests/test_ingest.py` 手工逐字节可说明的微型样例 + 官方库交叉验证
- `out/*.report.json` 三个 fixture 的产出报告
