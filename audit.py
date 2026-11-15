#!/usr/bin/env python3
"""Independent audit of exported dialogue samples.

Reads the original messages.jsonl plus the exported artifacts
(--out-dir/samples.jsonl and --out-dir/manifest.json) and re-checks, from
the artifacts:

  * parent edges  - each sample's refs are exactly the log's root -> target
                    parent chain (no siblings, no gaps, same conversation)
  * tool pairing  - every tool result consumes exactly one pending call_id;
                    a group completes before the next user/assistant
  * supervision   - exactly one supervise=true, on the final (target) message
  * fidelity      - exported fields match the logged nodes
  * manifest      - accepted targets map 1:1 to samples; rejected targets
                    carry a reason and are independently confirmed invalid

Exit status: 0 = clean, 1 = audit failures found, 2 = audit could not run.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROLES = ("system", "user", "assistant", "tool")


def _is_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _valid_call_id(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and value != ""


def _get_call_id(node):
    if isinstance(node, dict) and "call_id" in node:
        return node["call_id"]
    return node.get("tool_call_id") if isinstance(node, dict) else None


def load_nodes(path):
    nodes, conflicts = {}, set()
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            cid, mid = obj.get("conversation_id"), obj.get("message_id")
            if not _is_id(cid) or not _is_id(mid):
                continue
            key = (cid, mid)
            if key in nodes:
                if nodes[key] != obj:
                    conflicts.add(key)
            else:
                nodes[key] = obj
    return nodes, conflicts


def expected_refs(nodes, conflicts, cid, mid):
    """Re-walk parent_id from the target; None if no clean chain exists."""
    refs, seen, key = [], set(), (cid, mid)
    while True:
        if key in seen or key in conflicts or key not in nodes:
            return None
        seen.add(key)
        refs.append(key)
        parent = nodes[key].get("parent_id")
        if parent is None:
            break
        if not _is_id(parent):
            return None
        key = (cid, parent)
    refs.reverse()
    return refs


def check_structure(items):
    """Role/system/tool-pairing rules over one message sequence."""
    problems = []
    pending = None
    for i, m in enumerate(items):
        if not isinstance(m, dict):
            problems.append(f"message[{i}]: not a JSON object")
            continue
        role = m.get("role")
        if role not in ROLES:
            problems.append(f"message[{i}]: unsupported role {role!r}")
            continue
        if role == "system":
            if i != 0:
                problems.append(f"message[{i}]: system is only allowed at the root")
        elif role in ("user", "assistant"):
            if pending:
                problems.append(
                    f"message[{i}]: {role} arrived before tool-call group "
                    f"{sorted(map(str, pending))} completed"
                )
            if role == "assistant":
                calls = m.get("tool_calls") or []
                ids, bad = [], False
                if not isinstance(calls, list):
                    problems.append(f"message[{i}]: tool_calls is not an array")
                    bad = True
                else:
                    for c in calls:
                        if (
                            not isinstance(c, dict)
                            or not _valid_call_id(c.get("id"))
                            or not isinstance(c.get("name"), str)
                            or not c.get("name")
                        ):
                            problems.append(f"message[{i}]: tool call needs a non-empty id and name")
                            bad = True
                        else:
                            ids.append(c["id"])
                if not bad and ids:
                    if len(set(ids)) != len(ids):
                        problems.append(f"message[{i}]: duplicate call id inside one tool-call group")
                    pending = set(ids)
        else:  # tool
            call_id = _get_call_id(m)
            if pending is None:
                problems.append(f"message[{i}]: tool result without a preceding tool_calls group")
            elif call_id not in pending:
                problems.append(
                    f"message[{i}]: tool result for unissued or already consumed call_id {call_id!r}"
                )
            else:
                pending.discard(call_id)
                if not pending:
                    pending = None
    if pending:
        problems.append(f"tool-call group {sorted(map(str, pending))} left uncompleted")
    return problems


def chain_problems(nodes, conflicts, cid, mid):
    """Independently decide whether (cid, mid) is unusable as a target."""
    key = (cid, mid)
    node = nodes.get(key)
    if key in conflicts:
        return ["identity conflict"]
    if node is None:
        return ["target not found"]
    problems = []
    if (
        node.get("role") != "assistant"
        or node.get("tool_calls")
        or not isinstance(node.get("content"), str)
        or not node.get("content")
    ):
        problems.append("not a plain assistant message with non-empty content")
    refs = expected_refs(nodes, conflicts, cid, mid)
    if refs is None:
        problems.append("unresolvable parent chain (cycle/conflict/missing parent)")
        return problems
    problems += check_structure([nodes[r] for r in refs])
    return problems


def audit_sample(sample, nodes, conflicts):
    problems = []
    if not isinstance(sample, dict):
        return ["sample is not a JSON object"]
    target, messages = sample.get("target"), sample.get("messages")
    if not isinstance(target, dict):
        return ["sample lacks a target identity"]
    cid, mid = target.get("conversation_id"), target.get("message_id")
    key = (cid, mid)
    if not isinstance(messages, list) or not messages:
        return ["sample has no messages"]

    tnode = nodes.get(key)
    if key in conflicts:
        problems.append("target has an identity conflict in the log")
    if tnode is None:
        problems.append("target not found in the log")
    elif (
        tnode.get("role") != "assistant"
        or tnode.get("tool_calls")
        or not isinstance(tnode.get("content"), str)
        or not tnode.get("content")
    ):
        problems.append("target is not a plain assistant message with non-empty content")

    refs = []
    for i, m in enumerate(messages):
        ref = m.get("ref") if isinstance(m, dict) else None
        if not isinstance(ref, dict) or "conversation_id" not in ref or "message_id" not in ref:
            problems.append(f"message[{i}]: missing original-node ref")
            refs.append(None)
        else:
            refs.append((ref["conversation_id"], ref["message_id"]))

    # parent edges: refs must be exactly the log's root -> target chain
    if tnode is not None and key not in conflicts and all(refs):
        want = expected_refs(nodes, conflicts, cid, mid)
        if want is None:
            problems.append("no clean root->target chain exists in the log")
        elif refs != want:
            problems.append("message refs are not exactly the root->target parent chain")

    # fidelity: exported fields must match the logged nodes
    for i, (m, r) in enumerate(zip(messages, refs)):
        if not isinstance(m, dict) or r is None or r in conflicts or r not in nodes:
            continue
        node = nodes[r]
        for field in ("role", "content", "call_id", "tool_call_id", "name"):
            if m.get(field) != node.get(field):
                problems.append(f"message[{i}]: {field} differs from logged node {r[1]!r}")
        if (m.get("tool_calls") or None) != (node.get("tool_calls") or None):
            problems.append(f"message[{i}]: tool_calls differ from logged node {r[1]!r}")

    # tool pairing / conversation structure
    problems += check_structure(messages)

    # unique supervision position
    flags = []
    for i, m in enumerate(messages):
        flag = m.get("supervise") if isinstance(m, dict) else None
        if not isinstance(flag, bool):
            problems.append(f"message[{i}]: supervise is missing or not a boolean")
        flags.append(flag)
    if [i for i, f in enumerate(flags) if f is True] != [len(messages) - 1]:
        problems.append("supervise=true must appear exactly once, on the final (target) message")
    if refs and refs[-1] is not None and refs[-1] != key:
        problems.append("final message ref does not match the declared target")
    return problems


def audit_manifest(manifest, samples, nodes, conflicts):
    problems = []
    if not isinstance(manifest, dict):
        return ["manifest is not a JSON object"]
    results = manifest.get("results")
    if not isinstance(results, list):
        return ["manifest lacks a results list"]
    accepted, rejected = [], []
    for r in results:
        if not isinstance(r, dict):
            problems.append("manifest result entry is not an object")
            continue
        t = r.get("target") or {}
        key = (t.get("conversation_id"), t.get("message_id"))
        if r.get("status") == "accepted":
            accepted.append(key)
        elif r.get("status") == "rejected":
            rejected.append((key, r))
        else:
            problems.append(f"manifest target {key}: unknown status {r.get('status')!r}")
    sample_keys = [
        (s.get("target", {}).get("conversation_id"), s.get("target", {}).get("message_id"))
        for s in samples
        if isinstance(s, dict) and isinstance(s.get("target"), dict)
    ]
    if Counter(accepted) != Counter(sample_keys):
        problems.append("manifest accepted targets do not match the exported samples one-to-one")
    sample_key_set = set(sample_keys)
    for key, r in rejected:
        if key in sample_key_set:
            problems.append(f"rejected target {key} unexpectedly has an exported sample")
        if not r.get("reason") or r.get("reason") == "ok":
            problems.append(f"rejected target {key} lacks a reason")
        cid, _ = key
        if cid is None:
            continue  # unresolvable/ambiguous entries are legitimately rejected
        if not chain_problems(nodes, conflicts, *key):
            problems.append(f"rejected target {key} looks valid; the rejection is unjustified")
    return problems


def _label(sample):
    t = sample.get("target") if isinstance(sample, dict) else None
    if isinstance(t, dict):
        return f"{t.get('conversation_id')}/{t.get('message_id')}"
    return "<no target>"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Independently audit exported dialogue samples.")
    ap.add_argument("--messages", required=True, help="original messages.jsonl")
    ap.add_argument("--out-dir", required=True, help="directory with samples.jsonl and manifest.json")
    args = ap.parse_args(argv)
    out_dir = Path(args.out_dir)

    try:
        nodes, conflicts = load_nodes(args.messages)
        samples = []
        with (out_dir / "samples.jsonl").open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                if not raw.strip():
                    continue
                try:
                    samples.append(json.loads(raw))
                except json.JSONDecodeError as exc:
                    print(f"samples.jsonl line {lineno}: invalid JSON ({exc.msg})")
                    samples.append(None)
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"audit: cannot read input: {exc}", file=sys.stderr)
        return 2

    failures = 0
    valid_samples = []
    for i, sample in enumerate(samples):
        if sample is None:
            failures += 1
            continue
        valid_samples.append(sample)
        problems = audit_sample(sample, nodes, conflicts)
        if problems:
            failures += 1
            print(f"sample {i} ({_label(sample)}): FAIL")
            for p in problems:
                print(f"  - {p}")
        else:
            print(f"sample {i} ({_label(sample)}): OK")

    manifest_problems = audit_manifest(manifest, valid_samples, nodes, conflicts)
    if manifest_problems:
        failures += 1
        print("manifest: FAIL")
        for p in manifest_problems:
            print(f"  - {p}")
    else:
        print("manifest: OK")

    if failures:
        print(f"AUDIT FAIL ({failures} failing area(s) across {len(samples)} sample(s) + manifest)")
        return 1
    print(f"AUDIT PASS ({len(samples)} sample(s), manifest consistent)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
