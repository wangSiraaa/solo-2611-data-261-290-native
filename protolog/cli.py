"""Command line interface: ``ingest`` and ``inspect`` subcommands."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import core, schema as schema_mod

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.normpath(os.path.join(_HERE, os.pardir, "fixtures"))


def _add_schema_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--descriptor", default=os.path.join(_FIXTURES, "descriptor.pb"),
                   help="FileDescriptorSet path (default: fixtures/descriptor.pb)")
    p.add_argument("--schema-proto", default=os.path.join(_FIXTURES, "schema.proto"),
                   help="schema.proto source for cross-check "
                        "(default: fixtures/schema.proto)")
    p.add_argument("--scope", default=os.path.join(_FIXTURES, "scope.json"),
                   help="scope.json with limits and message name "
                        "(default: fixtures/scope.json)")
    p.add_argument("--message", default=None,
                   help="fully qualified message name (default: from scope.json)")
    p.add_argument("--max-payload-bytes", type=int, default=None,
                   help="override max payload length (default: from scope.json)")
    p.add_argument("--max-frames", type=int, default=None,
                   help="override frame limit (default: from scope.json)")


def _load(args):
    scope = {}
    if args.scope and os.path.exists(args.scope):
        with open(args.scope, "r", encoding="utf-8") as fh:
            scope = json.load(fh)
    message = args.message or scope.get("message", "demo.Sample")
    limits = core.Limits(
        max_payload_bytes=(args.max_payload_bytes
                           or scope.get("max_payload_bytes", 4096)),
        max_frames=args.max_frames or scope.get("max_frames", 50),
    )
    schema = schema_mod.load_schema(args.descriptor, message)
    warnings = []
    if args.schema_proto and os.path.exists(args.schema_proto):
        warnings = schema_mod.check_schema_proto(schema, args.schema_proto)
    return schema, limits, warnings


def cmd_ingest(args) -> int:
    schema, limits, warnings = _load(args)
    report = core.ingest_file(args.input, schema, limits,
                              descriptor_path=args.descriptor,
                              schema_warnings=warnings)
    text = core.report_to_json(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    c = report["counts"]
    print(f"frames={report['frames_total']} ok={c['ok']} "
          f"bad_payload={c['bad_payload']} truncated={c['truncated']} "
          f"framing_error={c['framing_error']} complete={report['complete']}",
          file=sys.stderr)
    return 0 if report["complete"] and c["bad_payload"] == 0 else 1


def cmd_inspect(args) -> int:
    schema, limits, warnings = _load(args)
    with open(args.input, "rb") as fh:
        data = fh.read()
    try:
        result = core.inspect_frame(data, schema, limits, args.frame)
    except IndexError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rec = result["frame"]
    rng = rec["payload_range"]
    print(f"frame {rec['index']}  status={rec['status']}  "
          f"length-prefix offset={rec['offset']}  "
          f"declared_length={rec['declared_length']}  "
          f"payload_range={rng}")
    for diag in rec["diagnostics"]:
        print(f"  diagnostic: {diag}")
    if result["hex"]:
        print("payload bytes:")
        for line in result["hex"]:
            print(f"  {line}")
    else:
        print("payload bytes: <none>")
    if result["wire_walk"]:
        print("wire walk (offsets relative to payload start):")
        _print_walk(result["wire_walk"], indent=1)
    print("decoded:")
    print(f"  values   = {json.dumps(rec['values'], ensure_ascii=False)}")
    print(f"  presence = {json.dumps(rec['presence'], ensure_ascii=False)}")
    print(f"  oneof    = {json.dumps(rec['oneof'], ensure_ascii=False)}")
    print(f"  unknown_fields = {rec['unknown_fields']}")
    f = result["file"]
    print(f"file: complete={f['complete']} stop_reason={f['stop_reason']} "
          f"counts={f['counts']}")
    return 0


def _print_walk(entries, indent=0):
    pad = "  " * indent
    for e in entries:
        if "error" in e and "offset" not in e:
            print(f"{pad}!! {e['error']}")
            continue
        name = e["field_name"] if e["field_name"] else "<unknown>"
        known = "" if e["known"] else "  [unknown field, skipped]"
        head = (f"{pad}+{e['offset']:<3} field {e['field_number']} "
                f"({name}) {e['wire_type']}{known}")
        detail = []
        if "value" in e:
            detail.append(f"value={e['value']!r}")
        elif "varint" in e:
            detail.append(f"varint={e['varint']}")
        if "length" in e:
            detail.append(f"len={e['length']}")
        if "message_type" in e:
            detail.append(f"type={e['message_type']}")
        if "packed_values" in e:
            detail.append(f"packed={e['packed_values']}")
        print(head + ("  " + " ".join(detail) if detail else ""))
        if "error" in e:
            print(f"{pad}    !! {e['error']}")
        if e.get("children"):
            _print_walk(e["children"], indent + 1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="protolog",
        description="ingest and inspect length-prefixed proto2 Sample logs")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="decode a whole log file")
    p_ingest.add_argument("input", help="binary log file (read-only)")
    p_ingest.add_argument("-o", "--output", help="write JSON report here")
    _add_schema_args(p_ingest)
    p_ingest.set_defaults(func=cmd_ingest)

    p_inspect = sub.add_parser("inspect", help="show one frame's bytes and meaning")
    p_inspect.add_argument("input", help="binary log file (read-only)")
    p_inspect.add_argument("--frame", type=int, required=True,
                           help="frame index (0-based, file order)")
    _add_schema_args(p_inspect)
    p_inspect.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
