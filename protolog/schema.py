"""Descriptor loading and schema sanity checks.

``fixtures/descriptor.pb`` (a FileDescriptorSet) is the machine-readable
schema; ``fixtures/schema.proto`` is its human-readable source.  The two are
expected to describe the same content — :func:`check_schema_proto` performs a
lightweight structural cross-check (not a full .proto parse) and reports any
mismatch as a warning, with the descriptor remaining authoritative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


@dataclass
class Schema:
    """Loaded descriptor pool plus the dynamic Sample class."""

    file_descriptor: object          # FileDescriptorProto for schema.proto
    message_descriptor: object       # google.protobuf.descriptor.Descriptor
    message_class: type              # dynamic class for the target message
    message_name: str                # e.g. "demo.Sample"

    @property
    def file_name(self) -> str:
        return self.file_descriptor.name

    @property
    def package(self) -> str:
        return self.file_descriptor.package


def load_schema(descriptor_path: str, message_name: str) -> Schema:
    """Load descriptor.pb and build the dynamic message class."""
    with open(descriptor_path, "rb") as fh:
        raw = fh.read()
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(raw)
    if not fds.file:
        raise ValueError(f"{descriptor_path}: descriptor set is empty")
    pool = descriptor_pool.DescriptorPool()
    for fd in fds.file:
        pool.Add(fd)
    try:
        msg_desc = pool.FindMessageTypeByName(message_name)
    except KeyError:
        raise ValueError(
            f"{descriptor_path}: message {message_name!r} not found; "
            f"available: {[m.full_name for f in fds.file for m in f.message_type]}")
    return Schema(
        file_descriptor=next(f for f in fds.file
                             if _contains(f, message_name)),
        message_descriptor=msg_desc,
        message_class=message_factory.GetMessageClass(msg_desc),
        message_name=message_name,
    )


def _contains(file_proto, message_name: str) -> bool:
    prefix = f"{file_proto.package}." if file_proto.package else ""
    return any(f"{prefix}{m.name}" == message_name for m in file_proto.message_type)


_FIELD_RE = re.compile(
    r"(?:^|[;{}\s])(?:optional|required|repeated)\s+"
    r"(int32|sint32|string|Info)\s+(\w+)\s*=\s*(\d+)")
_ONEOF_MEMBER_RE = re.compile(r"(?:^|[;{}\s])(\w+)\s*=\s*(\d+)\s*;")


def _message_body(text: str, msg_name: str) -> str | None:
    """Extract the brace-balanced body of ``message <msg_name> { ... }``."""
    m = re.search(rf"message\s+{re.escape(msg_name)}\s*\{{", text)
    if not m:
        return None
    depth = 0
    for i in range(m.end() - 1, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[m.end():i]
    return None


def check_schema_proto(schema, schema_proto_path: str) -> list[str]:
    """Cross-check schema.proto text against the loaded descriptor.

    Returns a list of warnings; empty means the visible structure agrees.
    """
    warnings: list[str] = []
    try:
        with open(schema_proto_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return [f"cannot read {schema_proto_path}: {exc}"]

    pkg = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    if pkg and pkg.group(1) != schema.package:
        warnings.append(f"package mismatch: schema.proto says {pkg.group(1)!r}, "
                        f"descriptor says {schema.package!r}")

    # Compare field (name, number) sets per message, including oneof members.
    for msg_name, desc in _iter_messages(schema):
        body = _message_body(text, msg_name)
        if body is None:
            warnings.append(f"message {msg_name} not found in schema.proto")
            continue
        found: dict[str, int] = {}
        for m in _FIELD_RE.finditer(body):
            found[m.group(2)] = int(m.group(3))
        # oneof members are declared without a label; mask the labelled
        # fields first so their names are not re-read as oneof members
        masked = _FIELD_RE.sub(";", body)
        for oneof_block in re.findall(r"oneof\s+\w+\s*\{(.*?)\}", masked, re.S):
            for m in _ONEOF_MEMBER_RE.finditer(oneof_block):
                found[m.group(1)] = int(m.group(2))
        expected = {f.name: f.number for f in desc.fields}
        if found != expected:
            warnings.append(f"message {msg_name}: schema.proto fields {found} "
                            f"!= descriptor fields {expected}")
    return warnings


def _iter_messages(schema):
    """Yield (short_name, Descriptor) for the target and referenced messages."""
    seen = set()
    desc = schema.message_descriptor
    stack = [desc] + [f.message_type for f in desc.fields
                      if f.message_type is not None]
    while stack:
        d = stack.pop()
        if d.full_name in seen:
            continue
        seen.add(d.full_name)
        yield d.name, d
        stack.extend(d.nested_types)
        stack.extend(f.message_type for f in d.fields
                     if f.message_type is not None)
