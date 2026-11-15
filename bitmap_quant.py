#!/usr/bin/env python3
"""Persistent bitmap index for repeated-structure item arrays.

Input rows look like:

    [{"row_id": "r0", "items": null | [{"kind": text|null, "qty": int|null}, ...]}]

The index assigns a contiguous row domain R and a contiguous element domain E.
Every element has exactly one owner row (persisted as ``owner``).  Leaf
predicates are evaluated only on E; ANY/ALL reduce element truth to R.

Truth values are pairs of disjoint bitsets ``(t, u)`` over a named domain;
``f`` is the complement inside the domain mask.  Every composite operation
checks the domain tag, so element bitsets and row bitsets never mix.  All
bitsets mask off tail bits beyond their domain.

Python 3.11 standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

FORMAT = "bitmap-quant-index"
VERSION = 1
MAX_ROWS = 200
MAX_ITEMS_PER_ROW = 20

_HEX_RE = re.compile(r"[0-9a-f]+\Z")
_QTY_KEY_RE = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")

_TOP_KEYS = {
    "format", "version", "row_count", "element_count", "row_ids", "owner",
    "row_element_masks", "null_array_rows", "empty_array_rows",
    "kind_values", "qty_values", "null_kind", "null_qty", "stats", "checksum",
}
_STAT_KEYS = {
    "rows", "elements", "elements_per_row", "kind_distinct", "qty_distinct",
    "null_kind_elements", "null_qty_elements", "null_array_rows",
    "empty_array_rows", "rows_with_elements",
}
_LEAF_KEYS = {"eq", "ge", "is_null"}
_COMP_KEYS = {"any", "all", "and", "or", "not"}


class BuildError(ValueError):
    """Input rows violate the documented shape or limits."""


class QueryError(ValueError):
    """The query AST is malformed or not allowed."""


class IndexFileError(ValueError):
    """On-disk index is corrupt, inconsistent, or belongs to another schema."""


# --------------------------------------------------------------------------
# Domain mask helpers (pure Python ints; tail bits always masked)
# --------------------------------------------------------------------------

def _mask(n: int) -> int:
    return (1 << n) - 1 if n > 0 else 0


def _popcount(x: int) -> int:
    return int(x).bit_count()


def _bits(x: int, n: int) -> list[int]:
    """Set bit indices of x restricted to [0, n)."""
    x &= _mask(n)
    out = []
    while x:
        low = x & -x
        out.append(low.bit_length() - 1)
        x ^= low
    return out


def _hex(x: int, n: int) -> str:
    x &= _mask(n)
    width = (n + 7) // 8 * 2
    return "" if width == 0 else format(x, "0%dx" % width)


def _unhex(s: Any, n: int, what: str) -> int:
    if n == 0:
        _expect(s == "", f"{what}: expected empty hex for empty domain")
        return 0
    _expect(isinstance(s, str) and _HEX_RE.fullmatch(s) is not None,
            f"{what}: invalid hex bitset")
    width = (n + 7) // 8 * 2
    _expect(len(s) == width, f"{what}: hex width {len(s)} != expected {width}")
    v = int(s, 16)
    _expect(v & ~_mask(n) == 0, f"{what}: bits outside domain of size {n}")
    return v


def _expect(cond: bool, msg: str, exc: type[Exception] = IndexFileError) -> None:
    if not cond:
        raise exc(msg)


# --------------------------------------------------------------------------
# Domain-tagged truth values
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Truth:
    domain: str   # "E" (elements) or "R" (rows)
    size: int
    t: int        # TRUE bits
    u: int        # UNKNOWN bits; t & u == 0

    def __post_init__(self) -> None:
        m = _mask(self.size)
        _expect(self.domain in ("E", "R"), f"bad truth domain {self.domain!r}")
        _expect(self.t & ~m == 0 and self.u & ~m == 0,
                "truth bits outside domain")
        _expect(self.t & self.u == 0, "TRUE and UNKNOWN bits overlap")

    @property
    def f(self) -> int:
        return _mask(self.size) & ~(self.t | self.u)

    def counts(self) -> dict[str, int]:
        return {"T": _popcount(self.t), "U": _popcount(self.u),
                "F": _popcount(self.f)}


def _same_domain(a: Truth, b: Truth) -> None:
    _expect(a.domain == b.domain,
            f"domain mismatch: {a.domain} vs {b.domain}", QueryError)
    _expect(a.size == b.size,
            f"domain size mismatch: {a.size} vs {b.size}", QueryError)


def t_and(a: Truth, b: Truth) -> Truth:
    _same_domain(a, b)
    m = _mask(a.size)
    t = a.t & b.t
    f = a.f | b.f
    return Truth(a.domain, a.size, t, m & ~t & ~f)


def t_or(a: Truth, b: Truth) -> Truth:
    _same_domain(a, b)
    m = _mask(a.size)
    t = a.t | b.t
    f = a.f & b.f
    return Truth(a.domain, a.size, t, m & ~t & ~f)


def t_not(a: Truth) -> Truth:
    return Truth(a.domain, a.size, a.f, a.u)


def _combine(terms: list[Truth], op: str) -> Truth:
    _expect(bool(terms), f"{op}: empty composite", QueryError)
    acc = terms[0]
    for x in terms[1:]:
        acc = t_and(acc, x) if op == "and" else t_or(acc, x)
    return acc


# --------------------------------------------------------------------------
# Index construction
# --------------------------------------------------------------------------

def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _normalize_rows(rows: Any) -> list[tuple[str, Any, list[dict[str, Any]]]]:
    _expect(isinstance(rows, list), "rows.json must contain a JSON array",
            BuildError)
    _expect(len(rows) <= MAX_ROWS, f"too many rows: {len(rows)} > {MAX_ROWS}",
            BuildError)
    out = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        _expect(isinstance(row, dict), f"row #{i} is not an object", BuildError)
        _expect(set(row) == {"row_id", "items"},
                f"row #{i} must have exactly row_id and items", BuildError)
        rid = row["row_id"]
        _expect(isinstance(rid, str) and rid != "", f"row #{i}: bad row_id",
                BuildError)
        _expect(rid not in seen, f"duplicate row_id {rid!r}", BuildError)
        seen.add(rid)
        items = row["items"]
        if items is None:
            out.append((rid, None, []))
            continue
        _expect(isinstance(items, list), f"row {rid}: items is not an array",
                BuildError)
        _expect(len(items) <= MAX_ITEMS_PER_ROW,
                f"row {rid}: {len(items)} items > {MAX_ITEMS_PER_ROW}",
                BuildError)
        elems = []
        for j, item in enumerate(items):
            _expect(isinstance(item, dict),
                    f"row {rid}[{j}]: item is not an object", BuildError)
            _expect(set(item) == {"kind", "qty"},
                    f"row {rid}[{j}]: must have exactly kind and qty",
                    BuildError)
            k, q = item["kind"], item["qty"]
            _expect(k is None or isinstance(k, str),
                    f"row {rid}[{j}]: kind must be text or NULL", BuildError)
            _expect(q is None or _is_int(q),
                    f"row {rid}[{j}]: qty must be integer or NULL", BuildError)
            elems.append({"kind": k, "qty": q})
        out.append((rid, items, elems))
    return out


def _checksum(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_index(rows: Any) -> dict[str, Any]:
    norm = _normalize_rows(rows)
    row_count = len(norm)
    element_count = sum(len(e) for _, _, e in norm)

    row_ids = [rid for rid, _, _ in norm]
    owner: list[int] = []
    null_array_rows = 0
    empty_array_rows = 0
    kind_values: dict[str, int] = {}
    qty_values: dict[str, int] = {}
    null_kind = 0
    null_qty = 0
    row_masks: list[str] = []
    per_row: list[int] = []

    for r, (rid, items, elems) in enumerate(norm):
        per_row.append(len(elems))
        if items is None:
            null_array_rows |= 1 << r
            row_masks.append(_hex(0, element_count))
            continue
        if not elems:
            empty_array_rows |= 1 << r
            row_masks.append(_hex(0, element_count))
            continue

        base = len(owner)
        owner.extend([r] * len(elems))
        row_masks.append(_hex(_mask(len(elems)) << base, element_count))
        for k, e in enumerate(elems):
            bit = 1 << (base + k)
            kk, qq = e["kind"], e["qty"]
            if kk is None:
                null_kind |= bit
            else:
                kind_values[kk] = kind_values.get(kk, 0) | bit
            if qq is None:
                null_qty |= bit
            else:
                key = str(qq)
                qty_values[key] = qty_values.get(key, 0) | bit

    payload = {
        "format": FORMAT,
        "version": VERSION,
        "row_count": row_count,
        "element_count": element_count,
        "row_ids": row_ids,
        "owner": owner,
        "row_element_masks": row_masks,
        "null_array_rows": _hex(null_array_rows, row_count),
        "empty_array_rows": _hex(empty_array_rows, row_count),
        "kind_values": {k: _hex(v, element_count)
                        for k, v in sorted(kind_values.items())},
        "qty_values": {k: _hex(v, element_count)
                       for k, v in sorted(qty_values.items(),
                                          key=lambda kv: int(kv[0]))},
        "null_kind": _hex(null_kind, element_count),
        "null_qty": _hex(null_qty, element_count),
    }
    payload["stats"] = {
        "rows": row_count,
        "elements": element_count,
        "elements_per_row": per_row,
        "kind_distinct": len(kind_values),
        "qty_distinct": len(qty_values),
        "null_kind_elements": _popcount(null_kind),
        "null_qty_elements": _popcount(null_qty),
        "null_array_rows": _popcount(null_array_rows),
        "empty_array_rows": _popcount(empty_array_rows),
        "rows_with_elements": sum(1 for n in per_row if n > 0),
    }
    payload["checksum"] = _checksum(payload)
    return payload


def save_index(index: dict[str, Any], path: str) -> None:
    blob = json.dumps(index, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Index loading and validation
# --------------------------------------------------------------------------

def load_index(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            idx = json.load(fh)
        except json.JSONDecodeError as exc:
            raise IndexFileError(f"index is not valid JSON: {exc}") from exc
    _validate_index(idx)
    return idx


def _validate_index(idx: Any) -> None:
    _expect(isinstance(idx, dict), "index root must be an object")
    _expect(set(idx) == _TOP_KEYS,
            f"index keys mismatch; missing={sorted(_TOP_KEYS - set(idx))} "
            f"extra={sorted(set(idx) - _TOP_KEYS)}")
    _expect(idx["format"] == FORMAT, "foreign index format")
    _expect(idx["version"] == VERSION, "foreign index version")
    _expect(_checksum({k: v for k, v in idx.items() if k != "checksum"})
            == idx["checksum"], "index checksum mismatch (corrupt index)")

    row_count, element_count = idx["row_count"], idx["element_count"]
    _expect(_is_int(row_count) and 0 <= row_count <= MAX_ROWS,
            "invalid row_count")
    _expect(_is_int(element_count)
            and 0 <= element_count <= MAX_ROWS * MAX_ITEMS_PER_ROW,
            "invalid element_count")

    row_ids = idx["row_ids"]
    _expect(isinstance(row_ids, list) and len(row_ids) == row_count,
            "row_ids length does not match row_count")
    _expect(all(isinstance(x, str) and x != "" for x in row_ids),
            "invalid row_id in index")
    _expect(len(set(row_ids)) == row_count, "duplicate row_id in index")

    owner = idx["owner"]
    _expect(isinstance(owner, list) and len(owner) == element_count,
            "owner length does not match element_count")
    owner_counts = [0] * row_count
    for i, r in enumerate(owner):
        _expect(_is_int(r) and 0 <= r < row_count,
                f"owner[{i}] outside row domain")
        owner_counts[r] += 1

    masks = idx["row_element_masks"]
    _expect(isinstance(masks, list) and len(masks) == row_count,
            "row_element_masks length does not match row_count")
    seen = 0
    per_row = [0] * row_count
    for r, s in enumerate(masks):
        v = _unhex(s, element_count, f"row_element_masks[{r}]")
        _expect(v & seen == 0, f"row_element_masks[{r}] overlaps another row")
        seen |= v
        per_row[r] = _popcount(v)
        for b in _bits(v, element_count):
            _expect(owner[b] == r,
                    f"owner[{b}]={owner[b]} but row mask {r} claims it")
    _expect(seen == _mask(element_count),
            "row_element_masks do not cover the element domain")
    _expect(per_row == owner_counts,
            "owner counts disagree with row masks")

    stats = idx["stats"]
    _expect(isinstance(stats, dict) and set(stats) == _STAT_KEYS,
            "stats keys mismatch")
    _expect(stats["rows"] == row_count and stats["elements"] == element_count,
            "stats row/element counts disagree")
    _expect(stats["elements_per_row"] == per_row,
            "stats elements_per_row disagrees")
    _expect(stats["rows_with_elements"] == sum(1 for n in per_row if n > 0),
            "stats rows_with_elements disagrees")

    null_rows = _unhex(idx["null_array_rows"], row_count, "null_array_rows")
    empty_rows = _unhex(idx["empty_array_rows"], row_count, "empty_array_rows")
    _expect(null_rows & empty_rows == 0,
            "NULL-array and empty-array row domains overlap")
    for r in range(row_count):
        bit = 1 << r
        if null_rows & bit:
            _expect(per_row[r] == 0,
                    f"row {r} is NULL array but has elements")
        elif empty_rows & bit:
            _expect(per_row[r] == 0,
                    f"row {r} is empty array but has elements")
        else:
            _expect(per_row[r] > 0,
                    f"row {r} is neither NULL/empty nor has elements")

    kv = idx["kind_values"]
    _expect(isinstance(kv, dict), "kind_values must be an object")
    kind_union = 0
    for k, s in kv.items():
        v = _unhex(s, element_count, f"kind_values[{k!r}]")
        _expect(v & kind_union == 0,
                f"kind_values[{k!r}] overlaps another key")
        kind_union |= v
    qv = idx["qty_values"]
    _expect(isinstance(qv, dict), "qty_values must be an object")
    qty_union = 0
    for k, s in qv.items():
        _expect(_QTY_KEY_RE.fullmatch(k) is not None,
                f"qty_values key {k!r} is not an integer literal")
        v = _unhex(s, element_count, f"qty_values[{k}]")
        _expect(v & qty_union == 0,
                f"qty_values[{k}] overlaps another value")
        qty_union |= v
    nk = _unhex(idx["null_kind"], element_count, "null_kind")
    nq = _unhex(idx["null_qty"], element_count, "null_qty")
    _expect(nk & kind_union == 0, "NULL kind domain overlaps kind values")
    _expect(nq & qty_union == 0, "NULL qty domain overlaps qty values")
    _expect((nk | kind_union) == _mask(element_count),
            "kind domains do not cover the element domain")
    _expect((nq | qty_union) == _mask(element_count),
            "qty domains do not cover the element domain")

    # Stats must be a pure function of the bitmaps they summarise.
    _expect(stats["kind_distinct"] == len(kv),
            "stats kind_distinct disagrees")
    _expect(stats["qty_distinct"] == len(qv),
            "stats qty_distinct disagrees")
    _expect(stats["null_kind_elements"] == _popcount(nk),
            "stats null_kind_elements disagrees")
    _expect(stats["null_qty_elements"] == _popcount(nq),
            "stats null_qty_elements disagrees")
    _expect(stats["null_array_rows"] == _popcount(null_rows),
            "stats null_array_rows disagrees")
    _expect(stats["empty_array_rows"] == _popcount(empty_rows),
            "stats empty_array_rows disagrees")


# --------------------------------------------------------------------------
# Query evaluation (index-only: no access to the raw arrays)
# --------------------------------------------------------------------------

class _Ctx:
    def __init__(self, idx: dict[str, Any]) -> None:
        self.row_size = idx["row_count"]
        self.element_size = idx["element_count"]
        self.row_ids = idx["row_ids"]
        self.kind_values = {k: int(v, 16) if v else 0
                            for k, v in idx["kind_values"].items()}
        self.qty_values = {int(k): int(v, 16) if v else 0
                           for k, v in idx["qty_values"].items()}
        self.null_kind = int(idx["null_kind"], 16) if idx["null_kind"] else 0
        self.null_qty = int(idx["null_qty"], 16) if idx["null_qty"] else 0
        self.null_rows = (int(idx["null_array_rows"], 16)
                          if idx["null_array_rows"] else 0)
        self.empty_rows = (int(idx["empty_array_rows"], 16)
                           if idx["empty_array_rows"] else 0)
        self.masks = [int(s, 16) if s else 0 for s in idx["row_element_masks"]]
        self.steps: list[dict[str, Any]] = []

    def record(self, node: str, tr: Truth, **extra: Any) -> None:
        rec = {"node": node, "domain": tr.domain, "domain_size": tr.size,
               "counts": tr.counts()}
        rec.update(extra)
        self.steps.append(rec)


def _is_leaf(node: Any) -> bool:
    return isinstance(node, dict) and bool(set(node) & _LEAF_KEYS)


def _is_comp(node: Any) -> bool:
    return (isinstance(node, dict) and len(node) == 1
            and next(iter(node)) in _COMP_KEYS)


def _eval_leaf(ctx: _Ctx, node: dict[str, Any]) -> Truth:
    if "eq" in node:
        _expect(set(node) == {"eq"}, f"malformed leaf: {node!r}", QueryError)
        args = node["eq"]
        _expect(isinstance(args, list) and len(args) == 2,
                f"eq needs [field, text]: {args!r}", QueryError)
        field, text = args
        _expect(field == "kind" and isinstance(text, str),
                f"eq only supports kind with a text value: {args!r}",
                QueryError)
        t = ctx.kind_values.get(text, 0)
        # A NULL kind cannot satisfy eq(kind, text): it is UNKNOWN, not FALSE.
        tr = Truth("E", ctx.element_size, t, ctx.null_kind)
        ctx.record("eq", tr, field=field, value=text, implementation="bitmap")
        return tr

    if "ge" in node:
        _expect(set(node) == {"ge"}, f"malformed leaf: {node!r}", QueryError)
        args = node["ge"]
        _expect(isinstance(args, list) and len(args) == 2,
                f"ge needs [field, int]: {args!r}", QueryError)
        field, limit = args
        _expect(field == "qty" and _is_int(limit),
                f"ge only supports qty with an integer: {args!r}", QueryError)
        t = 0
        merged = []
        for val in sorted(ctx.qty_values):
            if val >= limit:
                t |= ctx.qty_values[val]
                merged.append(val)
        tr = Truth("E", ctx.element_size, t, ctx.null_qty)
        ctx.record("ge", tr, field=field, value=limit,
                   merged_values=merged, implementation="value-bitmaps")
        return tr

    if "is_null" in node:
        _expect(set(node) == {"is_null"}, f"malformed leaf: {node!r}",
                QueryError)
        field = node["is_null"]
        _expect(field in ("kind", "qty"),
                f"is_null only supports kind/qty: {field!r}", QueryError)
        t = ctx.null_kind if field == "kind" else ctx.null_qty
        tr = Truth("E", ctx.element_size, t, 0)
        ctx.record("is_null", tr, field=field, implementation="null-bitmap")
        return tr

    raise QueryError(f"not a supported leaf: {node!r}")


def _eval(ctx: _Ctx, ast: Any) -> Truth:
    if _is_leaf(ast):
        return _eval_leaf(ctx, ast)
    if not _is_comp(ast):
        raise QueryError(f"malformed query node: {ast!r}")

    op, arg = next(iter(ast.items()))

    if op in ("any", "all"):
        inner = _eval(ctx, arg)
        _expect(inner.domain == "E",
                f"{op.upper()} operand must be element-domain, got {inner.domain}",
                QueryError)
        t_rows = u_rows = 0
        if op == "any":
            # T if some element T; else U if some element U (or the array
            # itself is NULL); else F.  Empty arrays stay F.
            for r, elem_mask in enumerate(ctx.masks):
                if inner.t & elem_mask:
                    t_rows |= 1 << r
                elif (inner.u & elem_mask) or (ctx.null_rows >> r) & 1:
                    u_rows |= 1 << r
        else:
            # F if some element F; else U if some element U (or the array is
            # NULL); else T.  Empty arrays stay T.
            for r, elem_mask in enumerate(ctx.masks):
                if inner.f & elem_mask:
                    pass
                elif (inner.u & elem_mask) or (ctx.null_rows >> r) & 1:
                    u_rows |= 1 << r
                else:
                    t_rows |= 1 << r
        tr = Truth("R", ctx.row_size, t_rows, u_rows)
        ctx.record(op, tr, from_domain="E", from_size=ctx.element_size,
                   source_counts=inner.counts())
        return tr

    if op in ("and", "or"):
        _expect(isinstance(arg, list) and len(arg) >= 1,
                f"{op} needs a non-empty list", QueryError)
        terms = [_eval(ctx, x) for x in arg]
        tr = _combine(terms, op)
        ctx.record(op, tr, operands=[x.counts() for x in terms])
        return tr

    if op == "not":
        inner = _eval(ctx, arg)
        tr = t_not(inner)
        ctx.record("not", tr, input_counts=inner.counts())
        return tr

    raise QueryError(f"unsupported operator {op!r}")


def evaluate(ctx: _Ctx, expr: Any) -> Truth:
    tr = _eval(ctx, expr)
    _expect(tr.domain == "R",
            "top-level expression must reduce to the row domain "
            f"(wrap element conditions in ANY/ALL); got {tr.domain}",
            QueryError)
    return tr


def run_query(idx: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    _expect(isinstance(query, dict), "query must be an object", QueryError)
    _expect(set(query) == {"id", "expr"},
            "query must have exactly id and expr", QueryError)
    _expect(isinstance(query["id"], str) and query["id"] != "",
            "query id must be a non-empty string", QueryError)
    ctx = _Ctx(idx)
    tr = evaluate(ctx, query["expr"])
    sets = {
        "TRUE": [idx["row_ids"][b] for b in _bits(tr.t, ctx.row_size)],
        "FALSE": [idx["row_ids"][b] for b in _bits(tr.f, ctx.row_size)],
        "UNKNOWN": [idx["row_ids"][b] for b in _bits(tr.u, ctx.row_size)],
    }
    ctx.record("result", tr)
    return {
        "id": query["id"],
        "result_sets": sets,
        "filtered": sets["TRUE"],
        "row_counts": tr.counts(),
        "steps": ctx.steps,
    }


def run_queries(idx: dict[str, Any], queries: Any) -> list[dict[str, Any]]:
    _expect(isinstance(queries, list), "queries.json must be a JSON array",
            QueryError)
    for i, q in enumerate(queries):
        _expect(isinstance(q, dict) and set(q) == {"id", "expr"},
                f"query #{i} must have exactly id and expr", QueryError)
        _expect(isinstance(q["id"], str) and q["id"] != "",
                f"query #{i} has a bad id", QueryError)
    ids = [q["id"] for q in queries]
    _expect(len(set(ids)) == len(ids), "duplicate query id", QueryError)
    return [run_query(idx, q) for q in queries]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _load_json(path: str, what: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise BuildError(f"{what}: invalid JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bitmap_quant",
        description="build/query a persistent repeated-structure bitmap index")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build an index from rows.json")
    b.add_argument("--rows", required=True)
    b.add_argument("--index", required=True)
    q = sub.add_parser("query", help="query a persisted index")
    q.add_argument("--index", required=True)
    q.add_argument("--queries", required=True)
    q.add_argument("--out", default=None)
    q.add_argument("--show-steps", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            idx = build_index(_load_json(a.rows, "rows"))
            save_index(idx, a.index)
            print(json.dumps(idx["stats"], sort_keys=True, ensure_ascii=False))
            return 0
        idx = load_index(a.index)
        results = run_queries(idx, _load_json(a.queries, "queries"))
        if not a.show_steps:
            for r in results:
                r.pop("steps", None)
        payload = {"format": FORMAT, "index_stats": idx["stats"],
                   "results": results}
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        else:
            print(text)
        return 0
    except (BuildError, QueryError, IndexFileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
