#!/usr/bin/env bash
# 端到端演示：用 fixtures 生成 out/ 下的全部交付产物。
# 用法：bash scripts/demo.sh
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p out

PLAN=fixtures/plan.json
ROWS=fixtures/rows.json

# 按 fixtures/chunks.json 的四个位置切分记录（第 4 块为空块）。
python3 - <<'PY'
import json
rows = json.load(open("fixtures/rows.json", encoding="utf-8"))
chunks = json.load(open("fixtures/chunks.json", encoding="utf-8"))
for i, positions in enumerate(chunks):
    with open(f"out/records-{i}.json", "w", encoding="utf-8") as fh:
        json.dump([rows[j] for j in positions], fh, ensure_ascii=False, indent=2)
        fh.write("\n")
PY

# 1) partial：每个块一份可序列化状态（空块 chunk-3 同样有身份）。
for i in 0 1 2 3; do
  python3 -m aggpipe partial \
    --plan "$PLAN" --records "out/records-${i}.json" \
    --chunk-id "chunk-${i}" --out "out/state-${i}.json"
done

# 2) merge：计划摘要校验 + chunk_id 防重后合并。
python3 -m aggpipe merge --plan "$PLAN" \
  --states out/state-0.json out/state-1.json out/state-2.json out/state-3.json \
  --out out/merged.json

# 3) finalize：规范结果（与 docs/EXPECTED_RESULTS.md 逐项一致）。
python3 -m aggpipe finalize --plan "$PLAN" \
  --state out/merged.json --out out/result.json

# 4) 对照：一次处理（不分块）的结果。
python3 -m aggpipe run --plan "$PLAN" --records "$ROWS" \
  --chunk-id whole --out out/result-one-shot.json

# 5) 完全空的总体：零状态合并后 finalize，只有 [] 的一行零计数。
echo '[]' > out/empty-records.json
python3 -m aggpipe partial --plan "$PLAN" \
  --records out/empty-records.json --chunk-id empty-0 --out out/state-empty.json
python3 -m aggpipe merge --plan "$PLAN" \
  --states out/state-empty.json --out out/merged-empty.json
python3 -m aggpipe finalize --plan "$PLAN" \
  --state out/merged-empty.json --out out/result-empty.json

echo
echo "产物位于 out/："
ls -1 out/
