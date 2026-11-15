"""protolog — length-prefixed proto2 Sample log ingester.

Reads files of the shape

    [4-byte big-endian payload length][one demo.Sample payload] ...

and produces per-frame records that keep protobuf presence semantics
(explicit default vs. absent), oneof merge/switch behaviour, mixed
packed/unpacked repeated fields and unknown-field reporting.
"""

__all__ = ["schema", "wirescan", "core"]
