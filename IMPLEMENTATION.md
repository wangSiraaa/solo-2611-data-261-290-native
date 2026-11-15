# jsonldrel 实现说明

离线 JSON-LD 子集 → 关系事实表（nodes / facts / list_members / diagnostics），
Python 3.11，使用 PyLD 做标准展开，不访问任何远程 context。

## 架构：双路读取 + 核对

```
原文（只读）
  ├─ 解析守卫：重复键 / NaN·Infinity 拒绝
  ├─ scope.py   语法层支持范围检查（远程 context、未声明关键字 → 明确拒绝）
  ├─ walk.py    read B：独立子集解释器（自实现 context 求值与展开，全程记录 JSON Pointer）
  ├─ libread.py read A：PyLD 标准展开（离线 loader，任何远程加载企图即抛错）→ 归约为记录
  ├─ canon.py   两路记录的空白节点无关核对（1-WL 同步精化）；不一致即拒绝 CHECK_MISMATCH
  └─ tables.py  关系表落库（SQLite），Python 侧完成事实集合去重
```

- PyLD 完成标准展开，但**最终产物不直接导出库的图**：产物表由 read B 的记录落成
  （只有它携带来源指针），read A 的展开结果充当核对 oracle。
- “另一个读取过程”即 read B：独立的 context 求值（`urllib.parse.urljoin` 做 base 解析，
  不复用 PyLD 的 IRI 解析），重建规范的关系多重/集合口径后与展开结果核对。
- 空白节点/列表标签：`_:g<输入摘要8位>n<序号>` / `_:g<输入摘要8位>l<序号>`，
  同一输入摘要下稳定；重排输入得到不同标签属预期（canon 核对与标签无关）。
- canon 的 1-WL 精化采用**同步（Jacobi）更新**，结果与迭代顺序、进程哈希种子无关。

## 支持子集（严格白名单，其余一律拒绝）

- context（仅内联对象）：`@base`、`@vocab`、`@language`、词项→IRI、前缀（简写映射自动获得
  前缀能力，规则同 PyLD `_prefix`）、词项 `@type:"@id"` 强制、词项 `@language`、
  `@container:"@language"`。
- 数据：`@context`（每个嵌套对象可重设，不向兄弟泄漏）、`@id`、`@type`、普通对象、
  多值数组（无序，嵌套数组按 JSON-LD 拍平）、`@value`、`@language`、`@type`（值对象 datatype）、
  `@list`、`@reverse`。
- 明确拒绝：`@graph`/`@included`/`@nest`/`@set`/`@index`、远程或数组或 null 的 `@context`、
  `@import`/`@version`/`@propagate`/`@protected`/`@direction`、词项级 `@reverse`/`@prefix`/
  关键字别名/scoped context、非 `"@id"` 的词项 `@type`、非 `@language` 的 `@container` 及容器组合、
  列表的列表、数据中的 null、无 base 的相对 @id、未映射属性（JSON-LD 会静默丢弃的，
  这里全部显式拒绝）。

## 关系表

| 表 | 口径 |
| --- | --- |
| `nodes` | 实体（IRI/空白/列表头），`occurrences` 为源中出现次数；同 `@id` 多处描述归并为一个实体 |
| `node_sources` | 实体每次出现的 JSON Pointer 与角色（root/property/reverse/list-member） |
| `facts` | 事实集合：`(subject, predicate, object_kind, object_iri, lexical, datatype, language, native_json)` 唯一 |
| `fact_sources` | 事实来源（多重）：同一事实的每次出现各一行 |
| `lists` | 每次 `@list` 出现一个独立列表身份；空列表明确存在（`member_count=0`） |
| `list_members` | `(list_id, idx)` 主键；下标从零开始；重复成员不去重 |
| `expansions` | 键/值 → IRI 的解析记录（`via`: term/prefix/vocab/absolute/base/keyword），含生效 context 指针 |
| `diagnostics` | error（拒绝）与 note（如根位置纯 @id 引用按展开规则不产生事实） |

- `object_kind ∈ {iri, literal, list}`；`@type` 记为 `rdf:type` 谓词的显式类型边。
- `@reverse` 中的对象形成**指向**当前 subject 的边；嵌套 `@reverse` 保持嵌套（与展开树一致）。
- 数字/布尔的 datatype 由 Python 原生类型直接决定（`xsd:integer/double/boolean`），
  词法值由原生值规范导出并另存 `native_json`；不存在“先展示字符串再猜回类型”。
- 无强制 `@id` 的字符串即使长得像 URL 也只是字面量（`xsd:string`）。
- 不做 RDF 推理、实体相似合并、语言翻译。

## 用法

```bash
python -m jsonldrel import fixtures/catalog.jsonld --db out.sqlite
python -m jsonldrel inspect out.sqlite explain /parts/0/name   # 为何此 name 是该 predicate
python -m jsonldrel inspect out.sqlite entity https://data.test/items/c
python -m jsonldrel inspect out.sqlite pointers                # 源指针 → 事实/列表成员映射
```

```python
from jsonldrel import import_document, Inspector
r = import_document("fixtures/catalog.jsonld")   # ok / diagnostics / counts / conn
insp = Inspector(r.conn)
insp.explain("/parts/1/name")   # term/vocab/absolute/base 解析链 + 生效 context 指针
insp.pointer_map()              # JSON Pointer → facts/list_members/nodes/lists
```

## 测试

`tests/`：fixtures 全量核对、拒绝样例（远程 context 等）、子集语义（context 覆盖、
属性重排、列表重复与空列表、反向边与双重反向、类型强制 vs URL 形文字、同 @id 归并、
数字原生类型）、canon 单元测试、120 篇固定种子随机文档的差分核对。
运行：`.venv/bin/python -m pytest tests/ -q`（77 项）。
