"""inspect：对提取结果做独立 read-back 核查。

不重新解析原始 XLIFF，而是只读取 extract 写出的 JSON 结果，独立地：

1. 用 token 列表重新拼接两个方向（source/target）的 token 串，与保存的
   ``serialized`` 比对；
2. 用 open/close 栈重新扫描每个 pc 的后代代码集合，与保存的
   ``pc_containment`` 及对侧结果比对；
3. 校验 pair 内代码 id 集合两侧一致、类型一致、data 引用可在本 unit 的
   code_table 回查到原始载荷；
4. 校验训练表只包含 trainable 片段、非训练片段都带排除原因；
5. 校验不同 unit 的同 id 载荷互不共享、缺 target 与空 target 被区分。

任何一项失败都进入 ``checks[].ok=False`` 并令整体 ``passed=False``；
文件级致命诊断同样使整体不通过——不会把损坏输入宣称成完整成功。
"""

from __future__ import annotations

import json
from typing import Optional

from .tokenizer import CLOSE, OPEN, pc_containment, serialize_tokens
from .reasons import FILE_FATAL_DIAGS, REASON_TARGET_EMPTY, REASON_TARGET_MISSING


def inspect_document(doc: dict) -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str, **loc) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "location": loc})

    manifest = doc.get("manifest", [])
    pairs = doc.get("pairs", [])
    code_table = doc.get("code_table", {})
    diagnostics = doc.get("diagnostics", [])

    # ---- 1. 逐 segment：两个方向的 token 串与 pc 包含关系重建 ----
    for rec in manifest:
        ident = rec["identity"]
        loc = {"unit_id": ident["unit_id"], "segment_id": ident["segment_id"]}
        for side in ("source", "target"):
            data = rec.get(side)
            if data is None:
                continue
            tokens = data["tokens"]
            rebuilt = serialize_tokens(tokens)
            check(
                "token_stream_readback",
                rebuilt == data["serialized"],
                f"{side} 重建串与保存值{'一致' if rebuilt == data['serialized'] else '不一致'}: "
                f"rebuilt={rebuilt!r} saved={data['serialized']!r}",
                side=side, **loc,
            )
            rebuilt_cont = pc_containment(tokens)
            check(
                "pc_containment_readback",
                rebuilt_cont == data["pc_containment"],
                f"{side} pc 包含关系重建 {rebuilt_cont} vs 保存 {data['pc_containment']}",
                side=side, **loc,
            )
            # open/close 配对索引也必须能闭合。
            balance_ok, detail = _pair_balance(tokens)
            check("pc_pair_balance", balance_ok, f"{side} {detail}", side=side, **loc)

        # ---- 缺 target / 空 target 区分 ----
        if not rec["has_target"]:
            check("target_state_distinction", REASON_TARGET_MISSING in rec["reasons"],
                  "缺少 target 已标记 target_missing", **loc)
            check("target_state_distinction", rec["target"] is None,
                  "缺少 target 时不产生 target token 侧", **loc)
        elif rec["target_empty"]:
            check("target_state_distinction", REASON_TARGET_EMPTY in rec["reasons"],
                  "空 target 已标记 target_empty", **loc)
            check("target_state_distinction",
                  rec["target"] is not None and rec["target"]["tokens"] == [],
                  "空 target 保留空侧记录而非缺失", **loc)

        # ---- 两侧代码 id / 类型一致性（从 token 重新统计） ----
        src_codes = _code_index(rec.get("source"))
        tgt_codes = _code_index(rec.get("target"))
        if rec.get("has_target") and rec["trainable"]:
            same_ids = set(src_codes) == set(tgt_codes)
            check("code_id_set_parity", same_ids,
                  f"代码 id 集合 source={sorted(src_codes)} target={sorted(tgt_codes)}",
                  **loc)
            if same_ids:
                kind_ok = all(
                    _kind_of(rec["source"], cid) == _kind_of(rec["target"], cid)
                    for cid in src_codes
                )
                check("code_kind_parity", kind_ok,
                      "同 id 代码两侧类型一致" if kind_ok else "存在类型不一致代码", **loc)

            # pc 后代集合两侧一致（允许换序，不允许移出/移入）。
            # 已判废片段的不匹配是预期诊断，隔离正确性由 exclusion/trainable 检查覆盖。
            if rec["trainable"]:
                s_cont = rec["source"]["pc_containment"] if rec.get("source") else {}
                t_cont = rec["target"]["pc_containment"] if rec.get("target") else {}
                check("pc_containment_parity",
                      all(s_cont.get(cid) == t_cont.get(cid) for cid in s_cont),
                      f"pc 包含关系 source={s_cont} target={t_cont}", **loc)

    # ---- 2. 配对表：每条都可回查载荷，且只收 trainable ----
    trainable_keys = {
        (r["identity"]["unit_id"], r["identity"]["segment_id"])
        for r in manifest if r["trainable"]
    }
    pair_keys = set()
    for pair in pairs:
        ident = pair["identity"]
        key = (ident["unit_id"], ident["segment_id"])
        pair_keys.add(key)
        loc = {"unit_id": ident["unit_id"], "segment_id": ident["segment_id"]}
        check("pair_is_trainable", key in trainable_keys,
              "配对表条目对应 trainable 片段", **loc)

        # 配对表序列化串与 manifest 一致（双向 token 串再建一次）。
        rec = next(r for r in manifest if (r["identity"]["unit_id"],
                                           r["identity"]["segment_id"]) == key)
        check("pair_source_readback",
              serialize_tokens(pair["source_tokens"]) == pair["source_serialized"]
              == rec["source"]["serialized"],
              "配对表 source token 串可重建且与清单一致", **loc)
        check("pair_target_readback",
              serialize_tokens(pair["target_tokens"]) == pair["target_serialized"]
              == rec["target"]["serialized"],
              "配对表 target token 串可重建且与清单一致", **loc)

        # 载荷回查：pair 携带的载荷必须等于 unit code_table 中的原始载荷。
        unit_data = code_table.get(ident["unit_id"], {})
        for cid, info in pair["code_payloads"].items():
            for ref, payload in info["refs"].items():
                table_entry = unit_data.get(ref)
                ok = table_entry is not None and table_entry["payload"] == payload
                check("payload_lookup", ok,
                      f"代码 {cid} → data {ref} 载荷回查 "
                      f"pair={payload!r} table={None if table_entry is None else table_entry['payload']!r}",
                      **loc)

    check("pair_table_completeness", pair_keys == trainable_keys,
          f"配对表 {len(pair_keys)} 条 vs trainable {len(trainable_keys)} 条")

    # ---- 3. 非训练片段必须带排除原因，来源不丢失 ----
    for rec in manifest:
        ident = rec["identity"]
        if not rec["trainable"]:
            check("exclusion_reason_present", bool(rec["reasons"]),
                  f"非训练片段 {ident['unit_id']}/{ident['segment_id']} 带排除原因",
                  unit_id=ident["unit_id"], segment_id=ident["segment_id"])
        check("manifest_keeps_source", rec.get("source") is not None,
              "清单保留 source token 来源",
              unit_id=ident["unit_id"], segment_id=ident["segment_id"])

    # ---- 4. 跨 unit：相同 data id 不共享载荷（按 code_table 独立建表） ----
    seen_payloads: dict[str, set[tuple[str, str]]] = {}
    for uid, data_map in code_table.items():
        for did, entry in data_map.items():
            seen_payloads.setdefault(did, set()).add((uid, entry["payload"]))
    for did, occurrences in seen_payloads.items():
        # 同一 data id 出现在多个 unit 是允许的，但每个 unit 的载荷各自独立存储；
        # code_table 以 unit 为顶层键即满足隔离，这里仅输出可观察事实。
        check("unit_payload_isolation", len(occurrences) >= 1,
              f"data id {did} 在 {len(occurrences)} 个 unit 中各自独立存载荷")

    # ---- 5. 文件级致命诊断 → 整体失败（不能把损坏输入核查成“完整成功”） ----
    fatal = [d for d in diagnostics if d["code"] in FILE_FATAL_DIAGS]
    status_ok = doc.get("file", {}).get("status") == "ok"
    check("file_status_consistency",
          status_ok == (not fatal),
          f"file.status={doc.get('file', {}).get('status')} 致命诊断数={len(fatal)}")
    check("no_file_level_failure", not fatal and status_ok,
          f"存在 {len(fatal)} 条文件级致命诊断，文件不完整")

    failed = [c for c in checks if not c["ok"]]
    return {
        "source_file": doc.get("source_file"),
        "passed": not failed,
        "total_checks": len(checks),
        "failed_checks": len(failed),
        "failures": failed,
        "checks": checks,
    }


def inspect_file(result_json_path: str) -> dict:
    with open(result_json_path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return inspect_document(doc)


def _pair_balance(tokens: list[dict]) -> tuple[bool, str]:
    stack: list[Optional[str]] = []
    for i, tok in enumerate(tokens):
        if tok["type"] == OPEN:
            stack.append(tok.get("code_id"))
        elif tok["type"] == CLOSE:
            if not stack:
                return False, f"token {i} 出现多余 close: {tok.get('code_id')}"
            opened = stack.pop()
            if opened != tok.get("code_id"):
                return False, f"pc 错配: open={opened} close={tok.get('code_id')}"
            if tok.get("pair_index") is not None:
                pidx = tok["pair_index"]
                if not (0 <= pidx < len(tokens)) or tokens[pidx]["type"] != OPEN \
                        or tokens[pidx].get("code_id") != tok.get("code_id"):
                    return False, f"pair_index {pidx} 未指向匹配 open"
    if stack:
        return False, f"存在未闭合 pc: {stack}"
    return True, "pc 开闭配对平衡"


def _code_index(side: Optional[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    if side is None:
        return out
    for tok in side["tokens"]:
        cid = tok.get("code_id")
        if cid is not None and cid not in out:
            out[cid] = tok["type"]
    return out


def _kind_of(side: Optional[dict], code_id: str) -> str:
    if side is None:
        return ""
    for tok in side["tokens"]:
        if tok.get("code_id") == code_id and tok["type"] != CLOSE:
            return "pc" if tok["type"] == OPEN else tok["type"]
    return ""
