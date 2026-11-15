#!/usr/bin/env python3
"""Cross-check the bitmap index against the scalar interpreter.

Checks:
  * fixture rows/queries: all three result sets agree with scalar evaluation;
    TRUE-only filtering matches.
  * the documented cross-element false hit (red qty=1 + blue qty=9) is
    excluded from ANY(red AND qty>=5).
  * NULL array / empty array / NULL qty semantics.
  * an input whose element count is not a multiple of 8 (tail-bit check).
  * a zero-element universe (every row NULL or empty).
  * randomized rows + randomized queries (three-set agreement).
  * persistence: the index is queried after deleting rows.json.
  * corrupt / mismatched-domain / truncated indexes raise.
  * row sets are mutually exclusive and cover every row.
  * per-step domain sizes and T/U counts are reported.

Run:  python3 verify.py
"""

from __future__ import annotations

import copy
import json
import os
import random
import sys
import tempfile

import bitmap_quant as bq
import scalar_check as sc


FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILS.append(msg)


def sample_exprs() -> list[dict]:
    """A spread of query shapes over the supported operators."""
    return [
        {"id": "any_same",
         "expr": {"any": {"and": [{"eq": ["kind", "red"]},
                                  {"ge": ["qty", 5]}]}}},
        {"id": "all_same",
         "expr": {"all": {"and": [{"eq": ["kind", "red"]},
                                  {"ge": ["qty", 5]}]}}},
        {"id": "not_any",
         "expr": {"not": {"any": {"and": [{"eq": ["kind", "red"]},
                                          {"ge": ["qty", 5]}]}}}},
        {"id": "any_null_qty", "expr": {"any": {"is_null": "qty"}}},
        {"id": "any_null_kind", "expr": {"any": {"is_null": "kind"}}},
        {"id": "any_red_or_blue",
         "expr": {"any": {"or": [{"eq": ["kind", "red"]},
                                 {"eq": ["kind", "blue"]}]}}},
        {"id": "any_red_and_not_nullqty",
         "expr": {"any": {"and": [{"eq": ["kind", "red"]},
                                  {"not": {"is_null": "qty"}}]}}},
        {"id": "all_ge0", "expr": {"all": {"ge": ["qty", 0]}}},
        {"id": "top_and",
         "expr": {"and": [{"any": {"is_null": "qty"}},
                          {"not": {"all": {"eq": ["kind", "red"]}}}]}},
        {"id": "not_all_red",
         "expr": {"not": {"all": {"eq": ["kind", "red"]}}}},
        {"id": "any_true_literal_or_null",
         "expr": {"any": {"or": [{"ge": ["qty", 0]},
                                 {"is_null": "qty"}]}}},
    ]


def assert_agrees(rows, queries, label):
    idx = bq.build_index(rows)
    # Round-trip through the persisted form to exercise validation.
    idx = json.loads(json.dumps(idx))
    bq._validate_index(idx)
    got = bq.run_queries(idx, queries)
    want = sc.run_scalar(copy.deepcopy(rows), queries)
    by_id = {w["id"]: w for w in want}
    for q in got:
        w = by_id[q["id"]]
        check(q["result_sets"] == w["result_sets"],
              f"{label}/{q['id']}: T/F/U sets agree")
        check(q["filtered"] == w["result_sets"]["TRUE"],
              f"{label}/{q['id']}: filter keeps exactly TRUE")
        check(q["row_counts"] == w["row_counts"],
              f"{label}/{q['id']}: T/U/F row counts agree")
        all_rows = {r["row_id"] for r in rows}
        union = (set(q["result_sets"]["TRUE"])
                 | set(q["result_sets"]["FALSE"])
                 | set(q["result_sets"]["UNKNOWN"]))
        check(union == all_rows,
              f"{label}/{q['id']}: three sets cover all rows")
        t = set(q["result_sets"]["TRUE"])
        f = set(q["result_sets"]["FALSE"])
        u = set(q["result_sets"]["UNKNOWN"])
        check(not (t & f) and not (t & u) and not (f & u),
              f"{label}/{q['id']}: three sets are pairwise disjoint")
    return got


def sections():
    with open("fixtures/rows.json", encoding="utf-8") as fh:
        rows = json.load(fh)
    with open("fixtures/queries.json", encoding="utf-8") as fh:
        queries = json.load(fh)

    print("\n[1] fixtures: index vs scalar")
    got = assert_agrees(rows, queries, "fixtures")
    by_id = {g["id"]: g for g in got}
    sets = {k: set(v) for k, v in by_id["any_same"]["result_sets"].items()}

    print("\n[2] cross-element false-hit must not count")
    check("r3" not in sets["TRUE"],
          "r3 (red qty=1 + blue qty=9) is not a same-element ANY hit")
    check("r4" in sets["TRUE"],
          "r4 (red qty=9) is a same-element ANY hit")

    print("\n[3] NULL / empty / NULL-qty semantics")
    check("r0" in sets["UNKNOWN"],
          "r0 NULL array -> ANY UNKNOWN")
    check("r1" in sets["FALSE"],
          "r1 empty array -> ANY FALSE")
    check("r2" in sets["UNKNOWN"],
          "r2 red with NULL qty -> element UNKNOWN -> ANY UNKNOWN")
    nullqty = set(by_id["any_null"]["result_sets"]["TRUE"])
    check(nullqty == {"r2", "r6"},
          "any(is_null(qty)) TRUE exactly for r2/r6")
    check("r0" in set(by_id["any_null"]["result_sets"]["UNKNOWN"]),
          "NULL array -> any(is_null(qty)) is UNKNOWN, not TRUE")
    check("r1" not in nullqty,
          "empty array is not a NULL-qty hit")
    nk_res = bq.run_query(bq.build_index(rows),
                          {"id": "any_null_kind",
                           "expr": {"any": {"is_null": "kind"}}})
    nullkind = set(nk_res["result_sets"]["TRUE"])
    check(nullkind == {"r4", "r6"},
          "any(is_null(kind)) TRUE exactly for r4/r6")
    check("r0" in set(nk_res["result_sets"]["UNKNOWN"]),
          "NULL array -> any(is_null(kind)) is UNKNOWN")
    allsame = by_id["all_same"]["result_sets"]
    check("r1" in set(allsame["TRUE"]),
          "empty array -> ALL TRUE")
    check("r0" in set(allsame["UNKNOWN"]),
          "NULL array -> ALL UNKNOWN")

    print("\n[4] per-step domains and T/U counts (fixtures/any_same)")
    for st in by_id["any_same"]["steps"]:
        print("   ", json.dumps(st, sort_keys=True, ensure_ascii=False))
    domains = {st["domain"] for st in by_id["any_same"]["steps"]}
    check(domains == {"E", "R"}, "steps span element and row domains")
    for st in by_id["any_same"]["steps"]:
        if st["domain"] == "E":
            check(st["domain_size"] == 7,
                  f"element-domain step {st['node']} has |E|=7")
        else:
            check(st["domain_size"] == 7,
                  f"row-domain step {st['node']} has |R|=7")

    print("\n[5] element total not a multiple of 8 (tail bits)")
    # 3 rows x 3 elements = 9 elements, plus one 2-element row -> 11 total.
    tail_rows = [
        {"row_id": "t0", "items": [{"kind": "red", "qty": 5},
                                   {"kind": "red", "qty": 1},
                                   {"kind": "blue", "qty": 9}]},
        {"row_id": "t1", "items": [
            {"kind": "blue", "qty": 0}, {"kind": "red", "qty": 7},
            {"kind": "red", "qty": 2}, {"kind": "blue", "qty": 8}]},
        {"row_id": "t2", "items": [{"kind": "red", "qty": 5},
                                   {"kind": "red", "qty": 6},
                                   {"kind": "red", "qty": 3},
                                   {"kind": "blue", "qty": 4}]},
    ]
    fifteen = [
        {"row_id": f"z{i}", "items": [
            {"kind": "red" if j % 2 else "blue", "qty": j}
            for j in range(5)]}
        for i in range(3)
    ]  # 15 elements total (mod 8 != 0)
    for label, rr in (("tail9", tail_rows), ("tail15", fifteen)):
        idx = bq.build_index(rr)
        check(idx["element_count"] % 8 != 0,
              f"{label}: element_count {idx['element_count']} % 8 != 0")
        # Every persisted bitset must have no bits past element_count.
        for s in idx["kind_values"].values():
            check(int(s, 16) >> idx["element_count"] == 0,
                  f"{label}: kind bitmap has no tail bits")
        assert_agrees(rr, sample_exprs(), label)

    print("\n[6] zero-element universe (all rows NULL/empty)")
    zero = [
        {"row_id": "e0", "items": None},
        {"row_id": "e1", "items": []},
        {"row_id": "e2", "items": None},
    ]
    idx = bq.build_index(zero)
    check(idx["element_count"] == 0, "zero-element index has |E|=0")
    zq = [
        {"id": "any", "expr": {"any": {"eq": ["kind", "red"]}}},
        {"id": "all", "expr": {"all": {"eq": ["kind", "red"]}}},
        {"id": "notany", "expr": {"not": {"any": {"is_null": "qty"}}}},
        {"id": "notall", "expr": {"not": {"all": {"eq": ["kind", "red"]}}}},
    ]
    zg = bq.run_queries(idx, zq)
    zb = {g["id"]: g["result_sets"] for g in zg}
    check(zb["any"]["TRUE"] == [] and set(zb["any"]["FALSE"]) == {"e1"}
          and set(zb["any"]["UNKNOWN"]) == {"e0", "e2"},
          "zero |E|: ANY -> F for the empty array, U for NULL arrays")
    check(set(zb["all"]["TRUE"]) == {"e1"}
          and set(zb["all"]["UNKNOWN"]) == {"e0", "e2"},
          "zero |E|: ALL -> T for the empty array, U for NULL arrays")
    check(set(zb["notany"]["TRUE"]) == {"e1"}
          and set(zb["notany"]["UNKNOWN"]) == {"e0", "e2"},
          "zero |E|: NOT(ANY) keeps U as U")
    check(set(zb["notall"]["FALSE"]) == {"e1"}
          and set(zb["notall"]["UNKNOWN"]) == {"e0", "e2"},
          "zero |E|: NOT(ALL) -> F for empty, U for NULL arrays")
    assert_agrees(zero, zq, "zero")

    # NULL array is ALL UNKNOWN even with zero elements overall.
    znull = bq.run_queries(bq.build_index([{"row_id": "n", "items": None}]),
                           [{"id": "a", "expr": {"all": {"eq": ["kind", "x"]}}}])
    check(znull[0]["result_sets"]["UNKNOWN"] == ["n"],
          "NULL array -> ALL UNKNOWN even with |E|=0")

    print("\n[7] persistence: query after deleting rows.json")
    with tempfile.TemporaryDirectory() as td:
        rows_path = os.path.join(td, "rows.json")
        idx_path = os.path.join(td, "idx.json")
        with open(rows_path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        bq.save_index(bq.build_index(json.load(open(rows_path))), idx_path)
        os.remove(rows_path)
        check(not os.path.exists(rows_path), "rows.json deleted")
        loaded = bq.load_index(idx_path)
        res = bq.run_queries(loaded, queries)
        check(res == [{**g, "steps": g["steps"]} for g in got],
              "results identical from persisted index with no raw rows")

    print("\n[8] corrupt / mismatched indexes must raise")
    idx = bq.build_index(rows)

    def expect_raise(mutate, label):
        bad = copy.deepcopy(idx)
        mutate(bad)
        try:
            bq._validate_index(bad)
        except bq.IndexFileError:
            check(True, f"{label} raises IndexFileError")
        else:
            check(False, f"{label} should raise")

    def flip_checksum(x):
        x["checksum"] = ("0" if x["checksum"][0] != "0" else "1") + x["checksum"][1:]
    expect_raise(flip_checksum, "tampered checksum")

    def change_stats(x):
        x["stats"]["elements"] = x["stats"]["elements"] + 1
    expect_raise(change_stats, "stats/domain mismatch")

    def move_owner(x):
        if x["owner"]:
            x["owner"][0] = (x["owner"][0] + 1) % x["row_count"]
            # keep checksum consistent so only the invariant can catch it
            x["checksum"] = bq._checksum({k: v for k, v in x.items()
                                          if k != "checksum"})
    expect_raise(move_owner, "owner outside its row mask")

    def grow_element_domain(x):
        x["element_count"] += 1
        x["checksum"] = bq._checksum({k: v for k, v in x.items()
                                      if k != "checksum"})
    expect_raise(grow_element_domain, "element domain does not match bitmaps")

    def drop_value_bit(x):
        for k in x["kind_values"]:
            x["kind_values"][k] = ""
            break
        x["checksum"] = bq._checksum({k: v for k, v in x.items()
                                      if k != "checksum"})
    expect_raise(drop_value_bit, "kind bitmaps no longer cover element domain")

    def foreign(x):
        x["format"] = "something-else"
    expect_raise(foreign, "foreign format")

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "bad.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write('{"format": "bitmap-quant-index"')
        try:
            bq.load_index(p)
        except bq.IndexFileError:
            check(True, "truncated JSON raises IndexFileError")
        else:
            check(False, "truncated JSON should raise")

    print("\n[9] raw-scan prohibition: evaluator never sees rows")
    ctx = bq._Ctx(json.loads(json.dumps(idx)))
    check(not hasattr(ctx, "rows") and not hasattr(ctx, "elements"),
          "_Ctx holds only persisted bitmaps + owner maps")

    print("\n[10] randomized cross-check")
    rng = random.Random(272)
    kinds = [None, "red", "blue", "Green ", "ré"]  # exact text compare
    for trial in range(60):
        n = rng.randint(0, 8)
        rr = []
        for i in range(n):
            choice = rng.random()
            if choice < 0.15:
                items = None
            elif choice < 0.25:
                items = []
            else:
                m = rng.randint(1, 6)
                items = [{"kind": rng.choice(kinds),
                          "qty": rng.choice([None, 0, 1, 4, 5, 9, -2])}
                         for _ in range(m)]
            rr.append({"row_id": f"q{i}_{i}", "items": items})
        assert_agrees_unique(rr, sample_exprs(), f"rand{trial}")

    print("\n[11] shape validation")
    bad_inputs = [
        ([{"row_id": "a", "items": [{"kind": "red", "qty": 1,
                                     "extra": 2}]}], "extra item field"),
        ([{"row_id": "a", "items": [{"kind": True, "qty": 1}]}], "bool kind"),
        ([{"row_id": "a", "items": [{"kind": "red", "qty": True}]}],
         "bool qty"),
        ([{"row_id": "a", "items": []}, {"row_id": "a", "items": []}],
         "duplicate row_id"),
        ([{"row_id": "a", "items": [{"kind": "red", "qty": 1}] * 21}],
         "too many items"),
        ([{"row_id": "a", "items": [{"kind": "red", "qty": 1.0}]}],
         "float qty"),
        ([{"row_id": "", "items": []}], "empty row_id"),
        ([{"row_id": "a", "items": [{"kind": "red"}]}], "missing qty"),
        ([{"row_id": "a", "items": [None]}], "NULL element"),
        ("not-a-list", "rows not an array"),
    ]
    for bad, label in bad_inputs:
        try:
            bq.build_index(bad)
        except bq.BuildError:
            check(True, f"build rejects {label}")
        else:
            check(False, f"build should reject {label}")

    # Query validation.
    for ast, label in [
        ({"eq": ["qty", 5]}, "eq on qty"),
        ({"ge": ["kind", 5]}, "ge on kind"),
        ({"ge": ["qty", True]}, "ge with bool"),
        ({"any": {"eq": ["kind", "red"]}, "all": {"eq": ["kind", "red"]}},
         "multi-key node"),
        ({"and": []}, "empty and"),
        ({"eq": ["kind", "red"]}, "element-domain top level"),
        ({"any": {"any": {"eq": ["kind", "red"]}}}, "row operand of ANY"),
        ({"or": [{"any": {"eq": ["kind", "red"]}},
                 {"eq": ["kind", "red"]}]}, "mixed E/R operands"),
        ({"is_null": "color"}, "unsupported is_null field"),
        ({"eq": ["kind", 3]}, "non-text eq"),
        ({"any": {}}, "empty inner object"),
    ]:
        try:
            bq.run_query(bq.build_index(rows), {"id": "x", "expr": ast})
        except bq.QueryError:
            check(True, f"query rejects {label}")
        else:
            check(False, f"query should reject {label}")

    print("\n[12] size boundaries and Unicode / exact text")
    empty_universe = bq.build_index([])
    check(empty_universe["row_count"] == 0
          and empty_universe["element_count"] == 0,
          "zero rows builds an empty index")
    check(bq.run_query(empty_universe,
                       {"id": "x", "expr": {"any": {"eq": ["kind", "a"]}}})
          ["result_sets"] == {"TRUE": [], "FALSE": [], "UNKNOWN": []},
          "zero rows: every result set empty")
    bq._validate_index(empty_universe)

    max_rows = [{"row_id": f"m{i}", "items": [
        {"kind": "red" if j % 3 else "blue", "qty": j}
        for j in range(20)]} for i in range(200)]
    idx = bq.build_index(max_rows)
    check(idx["row_count"] == 200 and idx["element_count"] == 4000,
          "200 rows x 20 elements builds")
    bq._validate_index(idx)
    max_q = [{"id": "last", "expr": {"any": {"and": [
        {"eq": ["kind", "red"]}, {"ge": ["qty", 19]}]}}}]
    assert_agrees(max_rows, max_q, "max200")
    try:
        bq.build_index(max_rows + [{"row_id": "overflow", "items": []}])
    except bq.BuildError:
        check(True, "build rejects 201 rows")
    else:
        check(False, "build should reject 201 rows")

    uni = [
        {"row_id": "u0", "items": [{"kind": "ré", "qty": 1},
                                   {"kind": "re", "qty": 2}]},
        {"row_id": "u1", "items": [{"kind": "ré", "qty": 3},
                                   {"kind": "", "qty": 4}]},
        {"row_id": "u2", "items": [{"kind": "Red", "qty": 5}]},
    ]
    uni_q = [
        {"id": "eq_re_acc", "expr": {"any": {"eq": ["kind", "ré"]}}},
        {"id": "eq_empty", "expr": {"any": {"eq": ["kind", ""]}}},
        {"id": "eq_case", "expr": {"any": {"eq": ["kind", "Red"]}}},
        {"id": "eq_red_lower", "expr": {"any": {"eq": ["kind", "red"]}}},
    ]
    ug = {g["id"]: g for g in assert_agrees(uni, uni_q, "unicode")}
    check(ug["eq_re_acc"]["result_sets"]["TRUE"] == ["u0", "u1"],
          "exact accented text matches both rows")
    check(ug["eq_empty"]["result_sets"]["TRUE"] == ["u1"],
          "empty string matches only the empty kind (NULL != '')")
    check(ug["eq_case"]["result_sets"]["TRUE"] == ["u2"],
          "text comparison is case sensitive")
    check(ug["eq_red_lower"]["result_sets"]["TRUE"] == [],
          "lowercase 'red' does not match 'Red'")


def assert_agrees_unique(rows, queries, label):
    """Like assert_agrees but only reports aggregate success per trial."""
    idx = bq.build_index(json.loads(json.dumps(rows)))
    bq._validate_index(idx)
    got = bq.run_queries(idx, queries)
    want = sc.run_scalar(copy.deepcopy(rows), queries)
    ok = all(g["result_sets"] == w["result_sets"] for g, w in zip(got, want))
    if not ok:
        for g, w in zip(got, want):
            if g["result_sets"] != w["result_sets"]:
                check(False, f"{label}/{g['id']}: {g['result_sets']} != "
                             f"{w['result_sets']}")
    check(ok, f"{label}: all three-set results agree with scalar")


def main() -> int:
    sections()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
