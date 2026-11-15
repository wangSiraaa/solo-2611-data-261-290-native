#!/usr/bin/env python3
"""Export trainable dialogue samples from a branching conversation log.

messages.jsonl carries one node per physical line:

    {"conversation_id": "c1", "message_id": "m2", "parent_id": "m1",
     "role": "assistant", "content": null,
     "tool_calls": [{"id": "call_1", "name": "search", "arguments": "..."}]}

targets.json explicitly lists the assistant messages to train on:

    [{"conversation_id": "c1", "message_id": "m5"}, ...]

    (also accepts {"targets": [...]} and bare "message_id" strings when the
     id is unique across conversations)

For each listed target this tool emits ONE sample whose context is the
unique ancestor chain obtained by walking parent_id inside a single
conversation, ordered root -> target.  Sibling branches never enter a
sample; shared ancestors may appear in several samples.  Messages are never
merged or time-sorted across a whole conversation.

Outputs (inside --out-dir):
    samples.jsonl   one sample per line:
                    {"target": {"conversation_id": ..., "message_id": ...},
                     "messages": [{"role": ..., "content": ..., ...,
                                   "ref": {"conversation_id": ..., "message_id": ...},
                                   "supervise": true|false}, ...]}
                    supervise is true on the target only.
    manifest.json   per-target accept/reject reasons.

Exit status: 0 = export ran (individual targets may still be rejected, see
the manifest), 2 = the input itself could not be processed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROLES = ("system", "user", "assistant", "tool")
MAX_MESSAGES = 100


class Reject(Exception):
    """One target cannot become a trainable sample; str(self) is the reason."""


# ------------------------------------------------------------------ input --

def _is_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def load_nodes(path):
    """Index well-formed nodes by (conversation_id, message_id).

    Returns (nodes, conflicts, errors):
      nodes     {(cid, mid): node}  first definition wins
      conflicts {(cid, mid)}        ids redefined with a different payload
      errors    [str]               unparseable / unkeyable physical lines
    """
    nodes, conflicts, errors = {}, set(), []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: invalid JSON ({exc.msg})")
                continue
            if not isinstance(obj, dict):
                errors.append(f"line {lineno}: not a JSON object")
                continue
            cid, mid = obj.get("conversation_id"), obj.get("message_id")
            if not _is_id(cid) or not _is_id(mid):
                errors.append(
                    f"line {lineno}: missing or unsupported conversation_id/message_id"
                )
                continue
            key = (cid, mid)
            if key in conflicts:
                continue
            if key in nodes:
                if nodes[key] == obj:
                    continue  # identical re-statement carries no new identity
                conflicts.add(key)  # same id, different payload: identity conflict
            else:
                nodes[key] = obj
    return nodes, conflicts, errors


def load_targets(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("targets")
    if not isinstance(data, list):
        raise ValueError("targets.json must be a JSON list or an object with a 'targets' list")
    return [{"message_id": e} if isinstance(e, str) else e for e in data]


# ------------------------------------------------------------------ chain --

def resolve_chain(nodes, conflicts, cid, mid):
    """Unique ancestor chain of (cid, mid), ordered root -> target."""
    chain, seen, key = [], set(), (cid, mid)
    while True:
        if key in seen:
            raise Reject(f"cycle detected at message_id {key[1]!r}")
        seen.add(key)
        if key in conflicts:
            raise Reject(f"identity conflict: message_id {key[1]!r} has multiple definitions")
        node = nodes.get(key)
        if node is None:
            if not chain:
                raise Reject("target message not found")
            raise Reject(f"missing parent message_id {key[1]!r} in conversation {cid!r}")
        chain.append(node)
        parent_id = node.get("parent_id")
        if parent_id is None:
            break
        if not _is_id(parent_id):
            raise Reject(f"message {node.get('message_id')!r}: unsupported parent_id {parent_id!r}")
        key = (cid, parent_id)  # parents are resolved inside the same conversation only
    chain.reverse()
    return chain


def validate_target(node):
    if node.get("role") != "assistant":
        raise Reject(f"target role is {node.get('role')!r}, expected 'assistant'")
    if node.get("tool_calls"):
        raise Reject("target declares tool_calls; only a plain assistant answer is trainable")
    content = node.get("content")
    if not isinstance(content, str) or not content:
        raise Reject("target assistant message has empty content")


def _valid_call_id(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and value != ""


def _get_call_id(node):
    """call_id links a tool result to its call; tolerate the tool_call_id alias."""
    if "call_id" in node:
        return node["call_id"]
    return node.get("tool_call_id")


def _open_group(node):
    """Start a tool-call group; return its pending call_ids (or None)."""
    calls = node.get("tool_calls")
    if not calls:
        return None
    mid = node.get("message_id")
    if not isinstance(calls, list):
        raise Reject(f"message {mid!r}: tool_calls is not an array")
    ids = []
    for call in calls:
        if (
            not isinstance(call, dict)
            or not _valid_call_id(call.get("id"))
            or not isinstance(call.get("name"), str)
            or not call.get("name")
        ):
            raise Reject(f"message {mid!r}: every tool call needs a non-empty id and name")
        ids.append(call["id"])
    if len(set(ids)) != len(ids):
        raise Reject(f"message {mid!r}: duplicate call id inside one tool-call group")
    return set(ids)


def validate_chain(chain):
    """Dependency rules along one root -> target chain."""
    pending = None  # call_ids of the currently open tool-call group
    for index, node in enumerate(chain):
        mid, role = node.get("message_id"), node.get("role")
        if role not in ROLES:
            raise Reject(f"message {mid!r}: unsupported role {role!r}")
        if role == "system":
            if index != 0:
                raise Reject(f"message {mid!r}: system is only allowed at the root")
        elif role in ("user", "assistant"):
            if pending:
                raise Reject(
                    f"message {mid!r}: {role} arrived before tool-call group "
                    f"{sorted(map(str, pending))} completed"
                )
            if role == "assistant":
                pending = _open_group(node)
        else:  # tool
            call_id = _get_call_id(node)
            if pending is None:
                raise Reject(f"message {mid!r}: tool result without a preceding tool_calls group")
            if call_id not in pending:
                raise Reject(
                    f"message {mid!r}: tool result for unissued or already "
                    f"consumed call_id {call_id!r}"
                )
            pending.discard(call_id)  # one tool message consumes exactly one call_id
            if not pending:
                pending = None
    if pending:
        raise Reject(f"tool-call group {sorted(map(str, pending))} left uncompleted")


# ----------------------------------------------------------------- render --

def render(node, supervise):
    """Sample message: logged payload + original-node ref + supervise flag.

    Tool output is untrusted text: stored verbatim as a tool message, never
    executed and never rewritten as system.
    """
    msg = {"role": node["role"], "content": node.get("content")}
    if node.get("tool_calls"):
        msg["tool_calls"] = node["tool_calls"]
    for key in ("call_id", "tool_call_id", "name"):
        if node.get(key) is not None:
            msg[key] = node[key]
    msg["ref"] = {"conversation_id": node["conversation_id"], "message_id": node["message_id"]}
    msg["supervise"] = supervise
    return msg


def _resolve_target(nodes, entry):
    if not isinstance(entry, dict):
        raise Reject(f"malformed target entry: {entry!r}")
    mid = entry.get("message_id")
    if mid is None:
        raise Reject("target entry lacks message_id")
    cid = entry.get("conversation_id")
    if cid is None:
        matches = {c for (c, m) in nodes if m == mid}
        if not matches:
            raise Reject(f"target message_id {mid!r} not found in any conversation")
        if len(matches) > 1:
            raise Reject(
                f"message_id {mid!r} exists in {len(matches)} conversations; "
                f"conversation_id is required"
            )
        cid = next(iter(matches))
    return cid, mid


# ------------------------------------------------------------------- main --

def main(argv=None):
    ap = argparse.ArgumentParser(description="Export trainable samples from a branching dialogue log.")
    ap.add_argument("--messages", required=True, help="messages.jsonl")
    ap.add_argument("--targets", required=True, help="targets.json")
    ap.add_argument("--out-dir", required=True, help="directory for samples.jsonl and manifest.json")
    args = ap.parse_args(argv)

    try:
        nodes, conflicts, input_errors = load_nodes(args.messages)
        targets = load_targets(args.targets)
    except (OSError, ValueError) as exc:
        print(f"export: cannot read input: {exc}", file=sys.stderr)
        return 2
    if len(nodes) > MAX_MESSAGES:
        print(f"export: {len(nodes)} messages exceeds the {MAX_MESSAGES}-message limit", file=sys.stderr)
        return 2

    samples, results = [], []
    for entry in targets:
        cid = entry.get("conversation_id") if isinstance(entry, dict) else None
        mid = entry.get("message_id") if isinstance(entry, dict) else None
        try:
            cid, mid = _resolve_target(nodes, entry)
            chain = resolve_chain(nodes, conflicts, cid, mid)
            validate_target(chain[-1])
            validate_chain(chain)
        except Reject as exc:
            results.append({
                "target": {"conversation_id": cid, "message_id": mid},
                "status": "rejected",
                "reason": str(exc),
            })
            print(f"rejected {cid}/{mid}: {exc}")
            continue
        sample = {
            "target": {"conversation_id": cid, "message_id": mid},
            "messages": [render(n, supervise=i == len(chain) - 1) for i, n in enumerate(chain)],
        }
        results.append({
            "target": sample["target"],
            "status": "accepted",
            "reason": "ok",
            "sample_index": len(samples),
        })
        print(f"accepted {cid}/{mid} -> sample {len(samples)} ({len(chain)} messages)")
        samples.append(sample)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    samples_path = out_dir / "samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, ensure_ascii=False) + "\n")
    manifest = {
        "messages_file": str(args.messages),
        "targets_file": str(args.targets),
        "samples_file": str(samples_path),
        "input_errors": input_errors,
        "total_targets": len(results),
        "accepted": sum(1 for r in results if r["status"] == "accepted"),
        "rejected": sum(1 for r in results if r["status"] == "rejected"),
        "results": results,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"exported {manifest['accepted']} sample(s), rejected {manifest['rejected']} target(s) -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
