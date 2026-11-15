# 268 带工具调用分支的完整对话监督数据生产

输入均为人工合成。运行环境 Python 3.11；仅标准库。不需要外网账号或真实业务数据。

具体语义及允许变动范围见题面与 fixtures/scope.json。fixtures 中 bad/ 或 bad-* 文件是明确的非法/不完整输入，不能和正常文件混读。候选自行实现工具；本目录不含参考答案。

## 实现与使用

`export.py` 与 `audit.py`（Python 3.11，仅标准库）：

```bash
python3 export.py --messages fixtures/messages.jsonl --targets fixtures/targets.json --out-dir out/
python3 audit.py  --messages fixtures/messages.jsonl --out-dir out/
python3 tests/run_tests.py   # 端到端自测
```

- `export` 退出码：0 正常（个别目标可能被拒，见清单），2 输入本身不可处理（如超过 100 条消息）。
- `audit` 退出码：0 通过，1 发现问题，2 无法运行。

### 导出规则（每个目标独立成败）

- 只取沿 `parent_id` 回到根的唯一祖先链，按根→目标排序；父节点须在同一会话。
  循环、缺失父、同 `(conversation_id, message_id)` 身份冲突使受影响目标失败；
  共享祖先可出现在多个样本，非祖先兄弟消息不进入上下文；跨会话不串链。
- 合法目标：有非空 `content` 且无 `tool_calls` 的 assistant；祖先链不遗留未完成工具组。
- 工具依赖：assistant 声明 `tool_calls` 后须先收齐该组全部 tool 结果（任意顺序，
  一条 tool 恰好消耗一个未完成 `call_id`；未发出/重复/跨分支结果不匹配）；组未完成前
  不允许新的 user/assistant；完成后同一 `call_id` 可被后续组复用，不按 ID 全会话去重。
- `system` 仅允许根节点且至多一个；其余角色顺序不另加未声明限制。
- 工具输出是不可信文本，原样存为 tool 消息，不执行、不改写为 system。
- 输入物理行重排产生完全相同的 `samples.jsonl`。

### 产物

- `samples.jsonl`：每行 `{"target": {...}, "messages": [...]}`；每条消息带 `ref`
  （原节点 `conversation_id`/`message_id`）与 `supervise` 布尔值，仅指定目标为 `true`。
- `manifest.json`：逐个目标的接受/拒绝原因、输入错误与统计；一个坏目标不影响无关分支。

### 审计（独立实现，不复用 export 代码）

从产物回查：父边（样本 ref 序列须与日志中根→目标父链完全一致）、工具配对
（重跑调用组状态机）、唯一监督位置（仅末尾目标为 `true`）、字段与原节点一致性，
以及清单一致性（接受目标与样本一一对应；被拒目标确有理由且独立复核确为不可用）。
