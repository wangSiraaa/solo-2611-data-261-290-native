# 261 XLIFF分段与内联代码保真的双语语料生产

输入均为人工合成。运行环境 Python 3.11；仅标准库。不需要外网账号或真实业务数据。

具体语义及允许变动范围见题面与 fixtures/scope.json。fixtures 中 bad/ 或 bad-* 文件是明确的非法/不完整输入，不能和正常文件混读。候选自行实现工具；本目录不含参考答案。

## 实现：xliff_corpus（仅标准库，不做翻译/训练）

- `xliff_corpus/tokenizer.py` —— source/target 混合 XML → `text`/`placeholder`/`open`/`close`
  四类 token，按解析后字符原样保存（不 trim、不补空格），递归顺序保留节点 text/tail 先后；
  另含规范序列化与 pc 后代代码集合的独立重建。
- `xliff_corpus/extractor.py` —— `extract_file(xliff_path, scope_path)`：
  - 清单 `manifest` 覆盖每个 segment（unit+segment 组合为身份），含两侧 token、state、
    `has_target`/`target_empty`（缺 target 与空 target 区分）、排除原因；
  - 训练配对表 `pairs` 只收 `translate=yes`、state ∈ {translated, final}、
    存在非空 target 且代码关系完全合格的片段，pair 自带载荷回查表；
  - `code_table` 按 unit 局部保存 originalData/data 载荷（实体解码、不执行 HTML，
    跨 unit 同 id 不共享）；
  - 片段级错误按片段隔离并定位到 XML 节点路径；XML 整体损坏或文件级致命问题令
    `file.status=failed`。
- `xliff_corpus/inspect_readback.py` —— `inspect_document(doc)`：只读提取结果，
  独立重建两个方向的 token 串、pc 开闭配对与后代代码集合、载荷回查与训练表一致性。

命令行：

```
python -m xliff_corpus extract fixtures/catalog.xlf --scope fixtures/scope.json -o out
python -m xliff_corpus inspect out/catalog.corpus.json
```

测试（32 个，覆盖换序、嵌套 pc、尾部空白、未翻译、禁译、坏引用、坏代码关系及
read-back 损坏检测）：

```
python -m unittest test.test_xliff_corpus -v
```

