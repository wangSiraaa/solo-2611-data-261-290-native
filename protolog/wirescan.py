"""Minimal protobuf wire-format scanner.

This is *not* a full decoder: value decoding/presence is delegated to the
official protobuf runtime.  The scanner exists to

  * validate wire boundaries and report precise offsets for corrupt payloads,
  * detect unknown fields (field numbers absent from the descriptor),
  * drive the human-readable per-tag walk used by ``inspect``.

Only the generic wire grammar (varint / fixed32 / fixed64 / length-delimited /
groups) is implemented; nothing here guesses field names or semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_SGROUP = 3
WIRE_EGROUP = 4
WIRE_FIXED32 = 5

WIRE_NAMES = {
    WIRE_VARINT: "varint",
    WIRE_FIXED64: "fixed64",
    WIRE_LEN: "length-delimited",
    WIRE_SGROUP: "start-group",
    WIRE_EGROUP: "end-group",
    WIRE_FIXED32: "fixed32",
}


class WireError(Exception):
    """Illegal wire boundary; ``offset`` is relative to the scanned buffer."""

    def __init__(self, offset: int, message: str):
        super().__init__(f"offset {offset}: {message}")
        self.offset = offset
        self.message = message


@dataclass
class Tag:
    """One scanned tag within a message buffer."""

    offset: int            # offset of the tag byte, relative to the buffer
    field_number: int
    wire_type: int
    header_end: int        # offset just past tag (and length prefix for LEN)
    data_start: int
    data_end: int          # for groups: offset just past the matching end-group
    varint_value: Optional[int] = None   # decoded value for WIRE_VARINT
    length: Optional[int] = None         # declared length for WIRE_LEN
    children: list["Tag"] = field(default_factory=list)  # nested group tags

    @property
    def end(self) -> int:
        return self.data_end


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Return (value, next_pos). Raises WireError on truncation/overlong."""
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(buf):
            raise WireError(start, "truncated varint (runs past end of buffer)")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 70:
            raise WireError(start, "varint longer than 10 bytes")


def scan_message(buf: bytes, start: int = 0, end: Optional[int] = None,
                 group_field: Optional[int] = None) -> tuple[list[Tag], int]:
    """Scan tags in buf[start:end].

    Returns (tags, stop_offset).  When ``group_field`` is given, scanning stops
    at the matching end-group tag (which is consumed and validated).
    Raises WireError on any illegal boundary.
    """
    if end is None:
        end = len(buf)
    tags: list[Tag] = []
    pos = start
    while pos < end:
        tag_offset = pos
        key, pos = read_varint(buf, pos)
        if key == 0:
            raise WireError(tag_offset, "tag byte 0 is not a valid field key")
        field_number = key >> 3
        wire_type = key & 0x7
        if field_number == 0:
            raise WireError(tag_offset, "field number 0 is reserved")
        if wire_type in (6, 7):
            raise WireError(tag_offset, f"wire type {wire_type} is not valid")
        if wire_type == WIRE_EGROUP:
            if group_field is None:
                raise WireError(tag_offset, "end-group without matching start-group")
            if field_number != group_field:
                raise WireError(
                    tag_offset,
                    f"end-group field {field_number} does not match "
                    f"start-group field {group_field}")
            return tags, pos

        if wire_type == WIRE_VARINT:
            value, pos = read_varint(buf, pos)
            tags.append(Tag(tag_offset, field_number, wire_type, tag_offset,
                            tag_offset, pos, varint_value=value))
        elif wire_type == WIRE_FIXED64:
            if pos + 8 > end:
                raise WireError(tag_offset,
                                f"fixed64 field {field_number} needs 8 bytes, "
                                f"only {end - pos} remain")
            tags.append(Tag(tag_offset, field_number, wire_type, tag_offset,
                            pos, pos + 8))
            pos += 8
        elif wire_type == WIRE_FIXED32:
            if pos + 4 > end:
                raise WireError(tag_offset,
                                f"fixed32 field {field_number} needs 4 bytes, "
                                f"only {end - pos} remain")
            tags.append(Tag(tag_offset, field_number, wire_type, tag_offset,
                            pos, pos + 4))
            pos += 4
        elif wire_type == WIRE_LEN:
            length, pos = read_varint(buf, pos)
            if pos + length > end:
                raise WireError(
                    tag_offset,
                    f"length-delimited field {field_number} declares "
                    f"{length} bytes, only {end - pos} remain")
            tags.append(Tag(tag_offset, field_number, wire_type, pos,
                            pos, pos + length, length=length))
            pos += length
        elif wire_type == WIRE_SGROUP:
            children, pos = scan_message(buf, pos, end, group_field=field_number)
            tags.append(Tag(tag_offset, field_number, wire_type, tag_offset,
                            tag_offset, pos, children=children))
        else:  # pragma: no cover - guarded above
            raise WireError(tag_offset, f"unhandled wire type {wire_type}")

    if group_field is not None:
        raise WireError(end, f"group field {group_field} never closed")
    return tags, pos


def zigzag32(value: int) -> int:
    """Decode a sint32 zigzag varint."""
    return (value >> 1) ^ -(value & 1)
