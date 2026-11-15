#!/usr/bin/env python3
"""Independent scalar (element-by-element) interpreter for the query AST.

This is a second implementation used only to cross-check the bitmap index.
It walks the raw rows directly, evaluates every leaf per element and applies
three-valued logic (T/F/U) with ANY/ALL reduced per owning row.  It shares no
code with ``bitmap_quant``.

Domain tagging is explicit: leaves/composite conditions are "E" (element
domain); ANY/ALL produce "R" (row domain).  Combining different domains is an
error, exactly as in the index evaluator.
"""

from __future__ import annotations

from typing import Any

LEAF_KEYS = {"eq", "ge", "is_null"}
COMP_KEYS = {"any", "all", "and", "or", "not"}


class ScalarQueryError(ValueError):
    pass


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise ScalarQueryError(msg)


def s_and(a: str, b: str) -> str:
    if a == "F" or b == "F":
        return "F"
    if a == "T" and b == "T":
        return "T"
    return "U"


def s_or(a: str, b: str) -> str:
    if a == "T" or b == "T":
        return "T"
    if a == "F" and b == "F":
        return "F"
    return "U"


def s_not(a: str) -> str:
    return {"T": "F", "F": "T", "U": "U"}[a]


def _counts(values: list[str]) -> dict[str, int]:
    return {"T": values.count("T"), "U": values.count("U"),
            "F": values.count("F")}


class Scalar:
    """Scalar evaluator over one immutable snapshot of raw rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.row_ids = [r["row_id"] for r in rows]
        self.row_items = [r["items"] for r in rows]
        self.elements: list[dict[str, Any]] = []
        self.row_slices: list[tuple[int, int]] = []
        for items in self.row_items:
            start = len(self.elements)
            if items:                       # None and [] contribute no elements
                self.elements.extend(items)
            self.row_slices.append((start, len(self.elements)))
        self.steps: list[dict[str, Any]] = []

    # -- leaves ------------------------------------------------------------
    def leaf_values(self, node: dict[str, Any]) -> list[str]:
        if "eq" in node:
            _expect(set(node) == {"eq"}, f"malformed leaf: {node!r}")
            args = node["eq"]
            _expect(isinstance(args, list) and len(args) == 2,
                    f"eq needs [field, text]: {args!r}")
            field, text = args
            _expect(field == "kind" and isinstance(text, str),
                    f"eq only supports kind with a text value: {args!r}")
            out = []
            for e in self.elements:
                k = e["kind"]
                out.append("U" if k is None else ("T" if k == text else "F"))
            return out

        if "ge" in node:
            _expect(set(node) == {"ge"}, f"malformed leaf: {node!r}")
            args = node["ge"]
            _expect(isinstance(args, list) and len(args) == 2,
                    f"ge needs [field, int]: {args!r}")
            field, limit = args
            _expect(field == "qty" and isinstance(limit, int)
                    and not isinstance(limit, bool),
                    f"ge only supports qty with an integer: {args!r}")
            out = []
            for e in self.elements:
                q = e["qty"]
                out.append("U" if q is None
                           else ("T" if q >= limit else "F"))
            return out

        if "is_null" in node:
            _expect(set(node) == {"is_null"}, f"malformed leaf: {node!r}")
            field = node["is_null"]
            _expect(field in ("kind", "qty"),
                    f"is_null only supports kind/qty: {field!r}")
            return ["T" if e[field] is None else "F" for e in self.elements]

        raise ScalarQueryError(f"not a supported leaf: {node!r}")

    # -- full evaluation ---------------------------------------------------
    def _record(self, node: str, domain: str, values: list[str],
                **extra: Any) -> None:
        rec = {"node": node, "domain": domain, "domain_size": len(values),
               "counts": _counts(values)}
        rec.update(extra)
        self.steps.append(rec)

    def _eval(self, node: Any) -> tuple[str, list[str]]:
        if isinstance(node, dict) and set(node) & LEAF_KEYS:
            vals = self.leaf_values(node)
            self._record("leaf", "E", vals)  # overwritten by caller name below
            # Give leaves their real operator name for step comparison.
            op = next(iter(set(node) & LEAF_KEYS))
            self.steps[-1]["node"] = op
            return "E", vals

        _expect(isinstance(node, dict) and len(node) == 1
                and next(iter(node)) in COMP_KEYS,
                f"malformed query node: {node!r}")
        op, arg = next(iter(node.items()))

        if op in ("any", "all"):
            dom, inner = self._eval(arg)
            _expect(dom == "E",
                    f"{op.upper()} operand must be element-domain, got {dom}")
            values: list[str] = []
            for r, (start, end) in enumerate(self.row_slices):
                if self.row_items[r] is None:      # NULL array
                    values.append("U")
                    continue
                seg = inner[start:end]             # empty array -> []
                if op == "any":
                    values.append("T" if "T" in seg
                                  else ("U" if "U" in seg else "F"))
                else:
                    values.append("F" if "F" in seg
                                  else ("U" if "U" in seg else "T"))
            self._record(op, "R", values, from_domain="E",
                         from_size=len(inner), source_counts=_counts(inner))
            return "R", values

        if op in ("and", "or"):
            _expect(isinstance(arg, list) and len(arg) >= 1,
                    f"{op} needs a non-empty list")
            dom0, acc = self._eval(arg[0])
            operand_counts = [_counts(acc)]
            for child in arg[1:]:
                dom, vals = self._eval(child)
                _expect(dom == dom0, f"domain mismatch {dom0} vs {dom}")
                acc = [s_and(a, b) if op == "and" else s_or(a, b)
                       for a, b in zip(acc, vals)]
                operand_counts.append(_counts(vals))
            self._record(op, dom0, acc, operands=operand_counts)
            return dom0, acc

        if op == "not":
            dom, inner = self._eval(arg)
            vals = [s_not(v) for v in inner]
            self._record("not", dom, vals, input_counts=_counts(inner))
            return dom, vals

        raise ScalarQueryError(f"unsupported operator {op!r}")

    def evaluate(self, expr: Any) -> tuple[str, list[str]]:
        dom, vals = self._eval(expr)
        _expect(dom == "R",
                "top-level expression must reduce to the row domain; "
                f"got {dom}")
        return dom, vals

    def run_query(self, query: dict[str, Any]) -> dict[str, Any]:
        _expect(isinstance(query, dict), "query must be an object")
        _expect(set(query) == {"id", "expr"},
                "query must have exactly id and expr")
        _expect(isinstance(query["id"], str) and query["id"] != "",
                "query id must be a non-empty string")
        _, vals = self.evaluate(query["expr"])
        self._record("result", "R", vals)
        sets = {
            "TRUE": [self.row_ids[i] for i, v in enumerate(vals) if v == "T"],
            "FALSE": [self.row_ids[i] for i, v in enumerate(vals) if v == "F"],
            "UNKNOWN": [self.row_ids[i] for i, v in enumerate(vals) if v == "U"],
        }
        return {
            "id": query["id"],
            "result_sets": sets,
            "filtered": sets["TRUE"],
            "row_counts": _counts(vals),
            "steps": self.steps,
            "values": vals,
        }


def run_scalar(rows: list[dict[str, Any]],
               queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [q["id"] for q in queries]
    _expect(len(ids) == len(set(ids)), "duplicate query id")
    return [Scalar(rows).run_query(q) for q in queries]


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="scalar reference interpreter")
    ap.add_argument("--rows", required=True)
    ap.add_argument("--queries", required=True)
    a = ap.parse_args()
    with open(a.rows, encoding="utf-8") as fh:
        rows = json.load(fh)
    with open(a.queries, encoding="utf-8") as fh:
        queries = json.load(fh)
    print(json.dumps([{"id": r["id"], "result_sets": r["result_sets"],
                       "row_counts": r["row_counts"]}
                      for r in run_scalar(rows, queries)],
                     indent=2, ensure_ascii=False, sort_keys=True))
