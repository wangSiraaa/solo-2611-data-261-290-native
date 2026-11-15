#!/usr/bin/env python3
"""End-to-end checks for export.py and audit.py.

Covers the acceptance matrix: sibling branches, out-of-order tool results,
call_id reuse by a later group, missing result, wrong result, cyclic target,
identity conflict, mid-chain system, cross-conversation id reuse, identical
duplicates, and physical line reordering of the input.
"""
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "export.py"
AUDIT = ROOT / "audit.py"


def build_messages():
    msgs = []

    def m(cid, mid, pid, role, **kw):
        node = {"conversation_id": cid, "message_id": mid, "parent_id": pid, "role": role}
        node.update(kw)
        msgs.append(node)

    # c1: main branch with out-of-order tool results, a sibling branch, call-id reuse
    m("c1", "m0", None, "system", content="You are helpful.")
    m("c1", "m1", "m0", "user", content="Weather in Paris and NYC?")
    m("c1", "m2", "m1", "assistant", content=None, tool_calls=[
        {"id": "call_a", "name": "get_weather", "arguments": "Paris"},
        {"id": "call_b", "name": "get_weather", "arguments": "NYC"},
    ])
    m("c1", "m3", "m2", "tool", call_id="call_b", content="NYC: sunny")   # out of order
    m("c1", "m4", "m3", "tool", call_id="call_a", content="Paris: rainy")
    m("c1", "m5", "m4", "assistant", content="Paris rainy, NYC sunny.")
    m("c1", "m6", "m1", "assistant", content="I cannot check the weather.")    # sibling of m2
    m("c1", "m7", "m5", "user", content="And tomorrow in Paris?")
    m("c1", "m8", "m7", "assistant", content=None, tool_calls=[
        {"id": "call_a", "name": "get_weather", "arguments": "Paris tomorrow"},  # reused call id
    ])
    m("c1", "m9", "m8", "tool", call_id="call_a", content="Paris tomorrow: windy")
    m("c1", "m10", "m9", "assistant", content="Windy tomorrow in Paris.")
    m("c1", "m11", "m5", "user", content="Thanks!")
    m("c1", "m12", "m11", "assistant", content="You are welcome!")
    m("c1", "m13", "m12", "assistant", content="")                             # empty-content target
    # c2: same message ids may live in another conversation; no cross-chaining
    m("c2", "m1", None, "user", content="Hi")
    m("c2", "a1", "m1", "assistant", content="Hello!")
    m("c2", "orphan1", "m5", "assistant", content="parent exists only in c1")
    # c3: bad chains
    m("c3", "b1", None, "user", content="Q")
    m("c3", "b2", "b1", "assistant", content=None, tool_calls=[
        {"id": "k1", "name": "f"}, {"id": "k2", "name": "g"}])
    m("c3", "b3", "b2", "tool", call_id="k1", content="partial")          # k2 never answered
    m("c3", "b4", "b3", "assistant", content="premature answer")
    m("c3", "w1", None, "user", content="Q")
    m("c3", "w2", "w1", "assistant", content=None, tool_calls=[{"id": "k9", "name": "f"}])
    m("c3", "w3", "w2", "tool", call_id="nope", content="wrong result")   # unissued id
    m("c3", "w4", "w3", "assistant", content="answer")
    m("c3", "y1", "y2", "user", content="cycle")
    m("c3", "y2", "y1", "user", content="cycle")
    m("c3", "y3", "y1", "assistant", content="after cycle")
    m("c3", "d1", None, "user", content="first definition")
    m("c3", "d2", "d1", "assistant", content="conflicted ancestor")
    m("c3", "s1", None, "user", content="Q")
    m("c3", "s2", "s1", "system", content="mid-chain system")
    m("c3", "s3", "s2", "assistant", content="after system")
    return msgs


TARGETS = [
    ({"conversation_id": "c1", "message_id": "m5"}, "accepted"),
    ({"conversation_id": "c1", "message_id": "m10"}, "accepted"),   # reused call id
    ({"conversation_id": "c1", "message_id": "m12"}, "accepted"),   # shared ancestors
    ({"conversation_id": "c2", "message_id": "a1"}, "accepted"),
    ({"message_id": "a1"}, "accepted"),                             # unique bare id
    ({"conversation_id": "c2", "message_id": "orphan1"}, "rejected"),  # missing parent
    ({"conversation_id": "c1", "message_id": "m2"}, "rejected"),    # has tool_calls
    ({"conversation_id": "c1", "message_id": "m1"}, "rejected"),    # not assistant
    ({"conversation_id": "c1", "message_id": "m13"}, "rejected"),   # empty content
    ({"conversation_id": "c1", "message_id": "nope"}, "rejected"),  # not found
    ({"message_id": "m1"}, "rejected"),                             # ambiguous bare id
    ({"conversation_id": "c3", "message_id": "b4"}, "rejected"),    # uncompleted group
    ({"conversation_id": "c3", "message_id": "w4"}, "rejected"),    # unmatched result
    ({"conversation_id": "c3", "message_id": "y3"}, "rejected"),    # cycle
    ({"conversation_id": "c3", "message_id": "d2"}, "rejected"),    # identity conflict
    ({"conversation_id": "c3", "message_id": "s3"}, "rejected"),    # system mid-chain
]


def run(cmd, check=True):
    r = subprocess.run([sys.executable, *map(str, cmd)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"{cmd} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


def main():
    tmp = Path(tempfile.mkdtemp(prefix="dialog_export_"))
    try:
        msgs = build_messages()
        lines = [json.dumps(m, ensure_ascii=False) for m in msgs]
        lines.append(json.dumps(msgs[0], ensure_ascii=False))  # identical duplicate: harmless
        d1 = next(m for m in msgs if m["message_id"] == "d1")
        lines.append(json.dumps(dict(d1, content="second, conflicting definition")))
        lines.append("{ this is not json")
        messages = tmp / "messages.jsonl"
        messages.write_text("\n".join(lines) + "\n", encoding="utf-8")
        targets = tmp / "targets.json"
        targets.write_text(json.dumps([t for t, _ in TARGETS]), encoding="utf-8")
        out = tmp / "out"
        run([EXPORT, "--messages", messages, "--targets", targets, "--out-dir", out])

        # ---- manifest: per-target accept/reject ------------------------------
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        got = [r["status"] for r in manifest["results"]]
        want = [s for _, s in TARGETS]
        assert got == want, f"status mismatch: {got} != {want}"
        assert all(r.get("reason") for r in manifest["results"]), "every result needs a reason"
        assert manifest["accepted"] == 5 and manifest["rejected"] == 11
        assert manifest["input_errors"], "broken line should be reported"

        # ---- samples ----------------------------------------------------------
        samples = [json.loads(l) for l in (out / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(samples) == 5

        s0 = samples[0]
        assert s0["target"] == {"conversation_id": "c1", "message_id": "m5"}
        refs = [m["ref"]["message_id"] for m in s0["messages"]]
        assert refs == ["m0", "m1", "m2", "m3", "m4", "m5"], refs
        assert "m6" not in refs, "sibling branch leaked into the sample"
        assert [m["supervise"] for m in s0["messages"]] == [False] * 5 + [True]
        assert s0["messages"][2]["tool_calls"][1]["id"] == "call_b"
        assert s0["messages"][3]["call_id"] == "call_b"   # out-of-order result kept
        assert s0["messages"][3]["content"] == "NYC: sunny"    # tool text stored verbatim

        refs1 = [m["ref"]["message_id"] for m in samples[1]["messages"]]
        assert refs1 == ["m0", "m1", "m2", "m3", "m4", "m5", "m7", "m8", "m9", "m10"], refs1
        assert samples[1]["messages"][7]["tool_calls"][0]["id"] == "call_a"  # id reused by new group
        assert samples[1]["messages"][8]["call_id"] == "call_a"

        for s in samples[1:3]:  # shared ancestors may appear in several samples
            r = [m["ref"]["message_id"] for m in s["messages"]]
            assert r[:6] == ["m0", "m1", "m2", "m3", "m4", "m5"]
        assert [m["ref"]["message_id"] for m in samples[3]["messages"]] == ["m1", "a1"]  # c2 chain
        assert samples[4]["target"] == {"conversation_id": "c2", "message_id": "a1"}     # bare id resolved

        # ---- physical line reordering yields identical samples ----------------
        baseline = (out / "samples.jsonl").read_bytes()
        rng = random.Random(42)
        for trial in range(5):
            shuffled = lines[:]
            rng.shuffle(shuffled)
            p = tmp / f"messages_shuf{trial}.jsonl"
            p.write_text("\n".join(shuffled) + "\n", encoding="utf-8")
            o = tmp / f"out_shuf{trial}"
            run([EXPORT, "--messages", p, "--targets", targets, "--out-dir", o])
            assert (o / "samples.jsonl").read_bytes() == baseline, f"shuffle {trial} changed samples"

        # ---- audit: clean export passes ---------------------------------------
        r = run([AUDIT, "--messages", messages, "--out-dir", out], check=False)
        assert r.returncode == 0, f"audit should pass:\n{r.stdout}\n{r.stderr}"

        # ---- audit: tampered artifacts fail -----------------------------------
        def tamper(note, fn):
            o = tmp / f"out_{note}"
            shutil.copytree(out, o)
            ss = [json.loads(l) for l in (o / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
            fn(ss)
            with (o / "samples.jsonl").open("w", encoding="utf-8") as fh:
                for s in ss:
                    fh.write(json.dumps(s, ensure_ascii=False) + "\n")
            r = run([AUDIT, "--messages", messages, "--out-dir", o], check=False)
            assert r.returncode == 1, f"audit must fail after tampering: {note}\n{r.stdout}"

        def swap(ss):
            ms = ss[0]["messages"]
            ms[3], ms[4] = ms[4], ms[3]

        tamper("swapped", swap)
        tamper("extra_supervise", lambda ss: ss[0]["messages"][0].update(supervise=True))
        tamper("no_supervise", lambda ss: ss[0]["messages"][-1].update(supervise=False))
        tamper("dropped", lambda ss: ss[0]["messages"].pop(3))
        tamper("wrong_call", lambda ss: ss[0]["messages"][3].update(call_id="call_a"))

        # ---- input contract: more than 100 messages is refused ----------------
        many = tmp / "many.jsonl"
        rows = [
            {"conversation_id": "big", "message_id": f"n{i}",
             "parent_id": f"n{i-1}" if i else None,
             "role": "user" if i % 2 == 0 else "assistant", "content": "x"}
            for i in range(101)
        ]
        many.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        r = run([EXPORT, "--messages", many, "--targets", targets, "--out-dir", tmp / "out_big"], check=False)
        assert r.returncode == 2, "over-limit input must be refused"

        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
