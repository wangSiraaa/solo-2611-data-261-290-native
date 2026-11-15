"""Frame ingestion, presence extraction and per-frame inspection.

Framing rules (see task statement):

  * frame = 4-byte big-endian unsigned payload length + one Sample payload;
  * payload length must be <= max_payload_bytes (4096);
  * a corrupt payload inside a complete-length frame is isolated: the frame is
    marked bad_payload and reading continues at the next frame boundary;
  * a truncated length prefix or truncated payload stops the file at that
    point — the next frame start is never guessed;
  * an oversized length prefix is a framing error: the file stops there too.

Value semantics come from the official protobuf runtime (last-one-wins for
scalars, merge for messages, oneof switch clears the previous branch, packed
and unpacked repeated elements concatenate in wire order).  Presence is read
back with HasField/WhichOneof so an explicit default (e.g. ``value = 0``) is
distinguishable from an absent field — JSON output never relies on omitting
default values.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from google.protobuf.message import DecodeError

from . import wirescan
from .wirescan import WireError

STATUS_OK = "ok"
STATUS_BAD_PAYLOAD = "bad_payload"
STATUS_TRUNCATED = "truncated"
STATUS_FRAMING_ERROR = "framing_error"


@dataclass
class Limits:
    max_payload_bytes: int = 4096
    max_frames: int = 50


@dataclass
class FrameRecord:
    index: int
    offset: int                       # file offset of the 4-byte length prefix
    status: str
    declared_length: Optional[int] = None
    payload_range: Optional[tuple[int, int]] = None   # [start, end) in file
    values: dict[str, Any] = field(default_factory=dict)
    presence: list[str] = field(default_factory=list)
    oneof: dict[str, Optional[str]] = field(default_factory=dict)
    unknown_fields: list[int] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    payload: bytes = b""              # raw payload bytes actually present

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "offset": self.offset,
            "status": self.status,
            "declared_length": self.declared_length,
            "payload_range": (list(self.payload_range)
                              if self.payload_range else None),
            "values": self.values,
            "presence": self.presence,
            "oneof": self.oneof,
            "unknown_fields": self.unknown_fields,
            "diagnostics": self.diagnostics,
        }


# ---------------------------------------------------------------------------
# presence / value extraction (generic over the descriptor)
# ---------------------------------------------------------------------------

def _extract_message(msg, prefix: str, values: dict, presence: list[str],
                     oneofs: dict[str, Optional[str]]) -> None:
    desc = msg.DESCRIPTOR
    for oneof in desc.oneofs:
        oneofs[oneof.name] = msg.WhichOneof(oneof.name)
    for f in desc.fields:
        path = f"{prefix}{f.name}"
        if f.is_repeated:
            container = getattr(msg, f.name)
            if len(container):
                presence.append(path)
                values[f.name] = [_scalar_or_message(v) for v in container]
            continue
        if f.containing_oneof is not None:
            if oneofs[f.containing_oneof.name] != f.name:
                continue
        elif not msg.HasField(f.name):
            continue
        presence.append(path)
        if f.message_type is not None:
            sub = getattr(msg, f.name)
            sub_values: dict[str, Any] = {}
            _extract_message(sub, f"{path}.", sub_values, presence, oneofs)
            values[f.name] = sub_values
        else:
            values[f.name] = getattr(msg, f.name)


def _scalar_or_message(v):
    """JSON-able value for one repeated element (scalars in this schema)."""
    if hasattr(v, "DESCRIPTOR"):  # repeated message field: recurse
        values: dict = {}
        _extract_message(v, "", values, [], {})
        return values
    return v


def _collect_unknown(buf: bytes, tags, desc, out: set[int]) -> None:
    """Record field numbers not declared by ``desc`` (recursively)."""
    by_number = {f.number: f for f in desc.fields}
    for tag in tags:
        f = by_number.get(tag.field_number)
        if f is None:
            # Unknown group contents belong to an unknown schema: field
            # numbers inside carry no meaning for our descriptor, so only
            # the group's own number is reported.
            out.add(tag.field_number)
            continue
        # message-typed fields always arrive length-delimited; recurse to
        # find unknown fields inside known submessages.
        if tag.wire_type == wirescan.WIRE_LEN and f.message_type is not None:
            try:
                sub_tags, _ = wirescan.scan_message(buf, tag.data_start,
                                                    tag.data_end)
            except WireError:
                continue  # boundary error already reported at top level
            _collect_unknown(buf, sub_tags, f.message_type, out)


def _decode_payload(schema, payload: bytes):
    """Decode one payload. Returns (values, presence, oneofs, unknown, err)."""
    try:
        tags, _ = wirescan.scan_message(payload)
    except WireError as exc:
        return None, None, None, None, exc

    msg = schema.message_class()
    try:
        msg.ParseFromString(payload)
    except DecodeError as exc:
        return None, None, None, None, WireError(0, f"protobuf decode: {exc}")

    values: dict[str, Any] = {}
    presence: list[str] = []
    oneofs: dict[str, Optional[str]] = {}
    _extract_message(msg, "", values, presence, oneofs)

    # repeated fields have no HasField; a packed empty block (e.g. "2a 00")
    # still proves the device sent the field, so union wire-observed paths.
    top_fields = {f.number: f for f in schema.message_descriptor.fields}
    for tag in tags:
        f = top_fields.get(tag.field_number)
        if f is not None and f.is_repeated and f.name not in values:
            presence.append(f.name)
            values[f.name] = []

    unknown: set[int] = set()
    _collect_unknown(payload, tags, schema.message_descriptor, unknown)
    return values, presence, oneofs, sorted(unknown), None


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def ingest_bytes(data: bytes, schema, limits: Limits) -> tuple[list[FrameRecord], dict]:
    """Split ``data`` into frames and decode each one."""
    frames: list[FrameRecord] = []
    counts = {STATUS_OK: 0, STATUS_BAD_PAYLOAD: 0,
              STATUS_TRUNCATED: 0, STATUS_FRAMING_ERROR: 0}
    stop_reason: Optional[str] = None
    offset = 0
    index = 0

    while offset < len(data):
        if index >= limits.max_frames:
            stop_reason = (f"frame limit {limits.max_frames} reached at "
                           f"offset {offset}; remaining {len(data) - offset} "
                           f"bytes not processed")
            counts[STATUS_FRAMING_ERROR] += 1
            break

        rec = FrameRecord(index=index, offset=offset, status=STATUS_OK)

        remaining = len(data) - offset
        if remaining < 4:
            rec.status = STATUS_TRUNCATED
            rec.diagnostics.append(
                f"truncated length prefix at offset {offset}: need 4 bytes, "
                f"only {remaining} remain; file stops here")
            frames.append(rec)
            counts[STATUS_TRUNCATED] += 1
            stop_reason = "truncated length prefix"
            break

        declared = int.from_bytes(data[offset:offset + 4], "big")
        rec.declared_length = declared

        if declared > limits.max_payload_bytes:
            rec.status = STATUS_FRAMING_ERROR
            rec.diagnostics.append(
                f"declared payload length {declared} exceeds limit "
                f"{limits.max_payload_bytes} at offset {offset}; "
                f"framing is untrustworthy, file stops here")
            frames.append(rec)
            counts[STATUS_FRAMING_ERROR] += 1
            stop_reason = "oversized frame"
            break

        payload_start = offset + 4
        payload_end = payload_start + declared
        if payload_end > len(data):
            rec.status = STATUS_TRUNCATED
            rec.payload_range = (payload_start, len(data))
            rec.payload = data[payload_start:]
            rec.diagnostics.append(
                f"truncated payload at offset {payload_start}: declared "
                f"{declared} bytes, only {len(data) - payload_start} remain; "
                f"file stops here")
            frames.append(rec)
            counts[STATUS_TRUNCATED] += 1
            stop_reason = "truncated payload"
            break

        payload = data[payload_start:payload_end]
        rec.payload_range = (payload_start, payload_end)
        rec.payload = payload

        values, presence, oneofs, unknown, err = _decode_payload(schema, payload)
        if err is not None:
            rec.status = STATUS_BAD_PAYLOAD
            abs_off = payload_start + err.offset
            rec.diagnostics.append(
                f"corrupt payload in frame {index} (file offset {abs_off}): "
                f"{err.message}; frame isolated, continuing at offset "
                f"{payload_end}")
            counts[STATUS_BAD_PAYLOAD] += 1
        else:
            rec.values = values
            rec.presence = presence
            rec.oneof = oneofs
            rec.unknown_fields = unknown
            if unknown:
                nums = ", ".join(f"#{n}" for n in unknown)
                rec.diagnostics.append(
                    f"unknown field(s) {nums} skipped (legal wire format, "
                    f"not in descriptor)")
            counts[STATUS_OK] += 1

        frames.append(rec)
        offset = payload_end
        index += 1

    complete = stop_reason is None
    summary = {
        "complete": complete,
        "stop_reason": stop_reason,
        "counts": counts,
        "frames_total": len(frames),
    }
    return frames, summary


def ingest_file(path: str, schema, limits: Limits,
                descriptor_path: Optional[str] = None,
                schema_warnings: Optional[list[str]] = None) -> dict:
    """Read ``path`` (never written to) and build the full report dict."""
    with open(path, "rb") as fh:
        data = fh.read()
    frames, summary = ingest_bytes(data, schema, limits)
    report = {
        "tool": "protolog-ingest",
        "input": {
            "path": path,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "descriptor": {
            "message": schema.message_name,
            "file": schema.file_name,
            "package": schema.package,
            **({"path": descriptor_path,
                "sha256": _file_sha256(descriptor_path)}
               if descriptor_path else {}),
            "schema_proto_warnings": schema_warnings or [],
        },
        "limits": {
            "max_payload_bytes": limits.max_payload_bytes,
            "max_frames": limits.max_frames,
        },
        **summary,
        "frames": [f.to_dict() for f in frames],
    }
    return report


def _file_sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

def inspect_frame(data: bytes, schema, limits: Limits,
                  frame_index: int) -> dict:
    """Re-run framing up to ``frame_index`` and explain that frame."""
    frames, summary = ingest_bytes(data, schema, limits)
    if frame_index < 0 or frame_index >= len(frames):
        raise IndexError(
            f"frame {frame_index} not present: file yields "
            f"{len(frames)} frame(s) (complete={summary['complete']})")
    rec = frames[frame_index]
    walk = _walk_payload(rec.payload, schema) if rec.payload else []
    return {
        "frame": rec.to_dict(),
        "hex": _hexdump(rec.payload, base=rec.payload_range[0]
                        if rec.payload_range else rec.offset),
        "wire_walk": walk,
        "file": {"complete": summary["complete"],
                 "stop_reason": summary["stop_reason"],
                 "counts": summary["counts"]},
    }


def _walk_payload(payload: bytes, schema) -> list[dict]:
    """Per-tag interpretation of a payload; tolerant of corrupt tails."""
    try:
        tags, _ = wirescan.scan_message(payload)
        error = None
    except WireError as exc:
        # walk what we can up to the error
        tags, error = _partial_tags(payload, exc), exc
    out: list[dict] = []
    _walk_tags(payload, tags, schema.message_descriptor, "", out)
    if error is not None:
        out.append({"error": f"offset {error.offset}: {error.message}"})
    return out


def _partial_tags(payload: bytes, exc: WireError) -> list:
    """Best-effort tags before a wire error (for diagnostics display)."""
    tags = []
    pos = 0
    try:
        while pos < len(payload):
            key, npos = wirescan.read_varint(payload, pos)
            field_number, wire_type = key >> 3, key & 7
            if field_number == 0 or wire_type in (4, 6, 7):
                break
            if wire_type == wirescan.WIRE_VARINT:
                value, npos = wirescan.read_varint(payload, npos)
                tags.append(wirescan.Tag(pos, field_number, wire_type, pos,
                                         pos, npos, varint_value=value))
            elif wire_type == wirescan.WIRE_LEN:
                length, hpos = wirescan.read_varint(payload, npos)
                if hpos + length > len(payload):
                    break
                tags.append(wirescan.Tag(pos, field_number, wire_type, hpos,
                                         hpos, hpos + length, length=length))
                npos = hpos + length
            elif wire_type == wirescan.WIRE_FIXED32:
                if npos + 4 > len(payload):
                    break
                tags.append(wirescan.Tag(pos, field_number, wire_type, pos,
                                         npos, npos + 4))
                npos += 4
            elif wire_type == wirescan.WIRE_FIXED64:
                if npos + 8 > len(payload):
                    break
                tags.append(wirescan.Tag(pos, field_number, wire_type, pos,
                                         npos, npos + 8))
                npos += 8
            else:
                break
            pos = npos
    except WireError:
        pass
    return tags


def _walk_tags(buf, tags, desc, prefix, out) -> None:
    by_number = {f.number: f for f in desc.fields}
    for tag in tags:
        f = by_number.get(tag.field_number)
        name = f.name if f is not None else None
        entry = {
            "offset": tag.offset,
            "field_number": tag.field_number,
            "field_name": name,          # None => unknown, name not guessed
            "wire_type": wirescan.WIRE_NAMES.get(tag.wire_type,
                                                 str(tag.wire_type)),
            "known": f is not None,
        }
        path = f"{prefix}{name}" if name else f"{prefix}#{tag.field_number}"
        entry["path"] = path
        if tag.wire_type == wirescan.WIRE_VARINT:
            entry["varint"] = tag.varint_value
            if f is not None and f.type == f.TYPE_SINT32:
                entry["value"] = wirescan.zigzag32(tag.varint_value)
            elif f is not None and f.type in (f.TYPE_INT32, f.TYPE_INT64,
                                              f.TYPE_UINT32, f.TYPE_UINT64,
                                              f.TYPE_BOOL, f.TYPE_ENUM):
                entry["value"] = tag.varint_value
        elif tag.wire_type == wirescan.WIRE_LEN:
            entry["length"] = tag.length
            if f is not None and f.message_type is not None:
                entry["message_type"] = f.message_type.full_name
                try:
                    sub, _ = wirescan.scan_message(buf, tag.data_start,
                                                   tag.data_end)
                except WireError as exc:
                    entry["error"] = f"offset {exc.offset}: {exc.message}"
                else:
                    children: list[dict] = []
                    _walk_tags(buf, sub, f.message_type, f"{path}.", children)
                    entry["children"] = children
            elif f is not None and f.type == f.TYPE_STRING:
                raw = buf[tag.data_start:tag.data_end]
                try:
                    entry["value"] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    entry["value"] = None
                    entry["error"] = "invalid UTF-8 in string field"
            elif f is not None and f.is_repeated and f.is_packed:
                vals = []
                pos = tag.data_start
                try:
                    while pos < tag.data_end:
                        v, pos = wirescan.read_varint(buf, pos)
                        if f.type == f.TYPE_SINT32:
                            v = wirescan.zigzag32(v)
                        vals.append(v)
                    entry["packed_values"] = vals
                except WireError as exc:
                    entry["error"] = f"offset {exc.offset}: {exc.message}"
            else:
                entry["note"] = "length-delimited data"
        elif tag.wire_type == wirescan.WIRE_SGROUP:
            children = []
            _walk_tags(buf, tag.children, desc if f is None else
                       (f.message_type or desc), f"{path}.", children)
            entry["children"] = children
        out.append(entry)


def _hexdump(data: bytes, base: int = 0, width: int = 16) -> list[str]:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + i:08x}  {hexs:<{width * 3}}  {text}")
    return lines


def report_to_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"
