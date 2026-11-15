"""Tests for protolog.

Every payload below is a hand-written byte string that can be explained
tag by tag; the official protobuf runtime is used as an independent
cross-check (encode + decode), never as the thing under test.

Wire-format reminders used in the comments:
  key = (field_number << 3) | wire_type, encoded as varint
  wire types: 0=varint 1=fixed64 2=length-delimited 3/4=group 5=fixed32
  sint32 values are zigzag-encoded: 0->0, -1->1, 1->2, -2->3, 2->4 ...
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from protolog import core, schema as schema_mod          # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")
DESCRIPTOR = os.path.join(FIXTURES, "descriptor.pb")
SCHEMA_PROTO = os.path.join(FIXTURES, "schema.proto")

SCHEMA = schema_mod.load_schema(DESCRIPTOR, "demo.Sample")
LIMITS = core.Limits(max_payload_bytes=4096, max_frames=50)
Sample = SCHEMA.message_class


def frame(payload: bytes) -> bytes:
    """Prefix one payload with its 4-byte big-endian length."""
    return len(payload).to_bytes(4, "big") + payload


def run(blob: bytes):
    return core.ingest_bytes(blob, SCHEMA, LIMITS)


def official(payload: bytes):
    """Decode with the official runtime only (no scanner involvement)."""
    msg = Sample()
    msg.ParseFromString(payload)
    return msg


class MicroSamples(unittest.TestCase):
    """Hand-explainable payloads, one semantic each."""

    def test_empty_message_has_no_presence(self):
        # payload "" : nothing on the wire -> nothing present
        frames, summary = run(frame(b""))
        self.assertTrue(summary["complete"])
        rec = frames[0]
        self.assertEqual(rec.status, "ok")
        self.assertEqual(rec.values, {})
        self.assertEqual(rec.presence, [])
        self.assertEqual(rec.oneof, {"choice": None})

    def test_explicit_zero_differs_from_absent(self):
        # 08 00 : field 1 (value), varint 0 -> value present AND == 0
        frames, _ = run(frame(bytes([0x08, 0x00])))
        rec = frames[0]
        self.assertEqual(rec.values, {"value": 0})
        self.assertEqual(rec.presence, ["value"])
        # same *value* as the empty message, different *presence*
        empty, _ = run(frame(b""))
        self.assertEqual(empty[0].values.get("value", 0), rec.values["value"])
        self.assertNotIn("value", empty[0].presence)

    def test_scalar_last_one_wins(self):
        # 08 01 08 02 : value written 1 then 2 -> last wins
        frames, _ = run(frame(bytes([0x08, 0x01, 0x08, 0x02])))
        self.assertEqual(frames[0].values, {"value": 2})
        self.assertEqual(frames[0].presence, ["value"])

    def test_info_merged_across_tags(self):
        # 12 02 08 07            info = Info{code: 7}
        # 12 08 12 06 e5 90 88   info = Info{note: "合并"}  (second tag merges)
        #          e5 b9 b6
        payload = bytes([0x12, 0x02, 0x08, 0x07,
                         0x12, 0x08, 0x12, 0x06,
                         0xE5, 0x90, 0x88, 0xE5, 0xB9, 0xB6])
        frames, _ = run(frame(payload))
        rec = frames[0]
        self.assertEqual(rec.values, {"info": {"code": 7, "note": "合并"}})
        self.assertEqual(rec.presence, ["info", "info.code", "info.note"])

    def test_oneof_three_switches(self):
        # 22 02 08 09          details = Info{code: 9}
        # 1a 05 72 65 73 65 74 text = "reset"      (details cleared)
        # 22 05 12 03 6e 65 77 details = Info{note: "new"} (fresh, code:9 lost)
        payload = bytes([0x22, 0x02, 0x08, 0x09,
                         0x1A, 0x05]) + b"reset" + bytes(
                         [0x22, 0x05, 0x12, 0x03]) + b"new"
        frames, _ = run(frame(payload))
        rec = frames[0]
        self.assertEqual(rec.oneof, {"choice": "details"})
        self.assertEqual(rec.values, {"details": {"note": "new"}})
        self.assertNotIn("details.code", rec.presence)
        self.assertNotIn("text", rec.presence)

    def test_oneof_same_member_merges(self):
        # 22 02 08 02                details = Info{code: 2}
        # 22 06 12 04 6b 65 70 74    details = Info{note: "kept"} -> merge
        payload = bytes([0x22, 0x02, 0x08, 0x02,
                         0x22, 0x06, 0x12, 0x04]) + b"kept"
        frames, _ = run(frame(payload))
        rec = frames[0]
        self.assertEqual(rec.values, {"details": {"code": 2, "note": "kept"}})
        self.assertEqual(rec.presence,
                         ["details", "details.code", "details.note"])

    def test_oneof_text_wins_over_details(self):
        # 22 02 08 09 1a 01 78 : details then text="x" -> only text remains
        payload = bytes([0x22, 0x02, 0x08, 0x09, 0x1A, 0x01, 0x78])
        frames, _ = run(frame(payload))
        rec = frames[0]
        self.assertEqual(rec.oneof, {"choice": "text"})
        self.assertEqual(rec.values, {"text": "x"})
        self.assertEqual(rec.presence, ["text"])

    def test_readings_mixed_packed_unpacked_wire_order(self):
        # 28 01          readings += zigzag(1) = -1   (unpacked)
        # 2a 02 04 00    readings += zigzag(4)=2, zigzag(0)=0  (packed)
        # 28 06          readings += zigzag(6) = 3    (unpacked)
        payload = bytes([0x28, 0x01, 0x2A, 0x02, 0x04, 0x00, 0x28, 0x06])
        frames, _ = run(frame(payload))
        self.assertEqual(frames[0].values["readings"], [-1, 2, 0, 3])
        self.assertEqual(frames[0].presence, ["readings"])

    def test_readings_packed_empty_still_present(self):
        # 2a 00 : packed block of length 0 -> field sent, zero elements
        frames, _ = run(frame(bytes([0x2A, 0x00])))
        rec = frames[0]
        self.assertEqual(rec.values, {"readings": []})
        self.assertEqual(rec.presence, ["readings"])

    def test_readings_zigzag_extremes(self):
        # zigzag(4294967294)= 2147483647, zigzag(4294967295)= -2147483648
        payload = bytes([0x28, 0xFE, 0xFF, 0xFF, 0xFF, 0x0F,
                         0x28, 0xFF, 0xFF, 0xFF, 0xFF, 0x0F])
        frames, _ = run(frame(payload))
        self.assertEqual(frames[0].values["readings"],
                         [2147483647, -2147483648])

    def test_unknown_varint_field_skipped(self):
        # 98 06 7b : key varint 0x3298 -> field 99, varint 123; not in schema
        payload = bytes([0x08, 0x2A, 0x98, 0x06, 0x7B])
        frames, _ = run(frame(payload))
        rec = frames[0]
        self.assertEqual(rec.status, "ok")
        self.assertEqual(rec.values, {"value": 42})
        self.assertEqual(rec.unknown_fields, [99])
        self.assertTrue(any("unknown" in d for d in rec.diagnostics))

    def test_unknown_length_delimited_field_skipped(self):
        # a2 06 02 41 42 : key varint -> field 100, len 2, payload "AB"
        payload = bytes([0xA2, 0x06, 0x02, 0x41, 0x42])
        frames, _ = run(frame(payload))
        rec = frames[0]
        self.assertEqual(rec.status, "ok")
        self.assertEqual(rec.values, {})
        self.assertEqual(rec.unknown_fields, [100])

    def test_unknown_group_field_skipped(self):
        # 93 03 08 2a 94 03 : field 50 start-group, inner field1 varint 42,
        #                      field 50 end-group
        payload = bytes([0x93, 0x03, 0x08, 0x2A, 0x94, 0x03])
        frames, _ = run(frame(payload))
        rec = frames[0]
        self.assertEqual(rec.status, "ok")
        # only the group number is reported; inner numbers are meaningless
        self.assertEqual(rec.unknown_fields, [50])

    def test_unknown_field_inside_known_submessage(self):
        # 12 05 08 07 c0 25 01 : info = Info{ code:7, field 600 varint 1 }
        #   c0 25 = key varint 4800 -> field 600, wire 0; 600 not in Info
        payload = bytes([0x12, 0x05, 0x08, 0x07, 0xC0, 0x25, 0x01])
        frames, _ = run(frame(payload))
        rec = frames[0]
        self.assertEqual(rec.values, {"info": {"code": 7}})
        self.assertEqual(rec.unknown_fields, [600])


class FramingRules(unittest.TestCase):
    """Frame boundary, truncation and corruption behaviour."""

    def test_two_frames_in_sequence(self):
        blob = frame(bytes([0x08, 0x01])) + frame(bytes([0x08, 0x02]))
        frames, summary = run(blob)
        self.assertTrue(summary["complete"])
        self.assertEqual([f.values["value"] for f in frames], [1, 2])
        self.assertEqual(frames[1].offset, 6)

    def test_truncated_length_prefix_stops_file(self):
        blob = frame(bytes([0x08, 0x01])) + b"\x00\x00"  # 2 of 4 prefix bytes
        frames, summary = run(blob)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["counts"]["ok"], 1)
        self.assertEqual(summary["counts"]["truncated"], 1)
        tail = frames[-1]
        self.assertEqual(tail.status, "truncated")
        self.assertIn("offset 6", tail.diagnostics[0])

    def test_truncated_payload_stops_file(self):
        blob = frame(bytes([0x08, 0x01])) + b"\x00\x00\x00\x06\x12\x04\x78"
        frames, summary = run(blob)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["stop_reason"], "truncated payload")
        tail = frames[-1]
        self.assertEqual(tail.declared_length, 6)
        self.assertEqual(tail.payload_range, (10, 13))  # only 3 of 6 bytes
        self.assertIn("only 3 remain", tail.diagnostics[0])

    def test_bad_payload_isolated_and_reading_continues(self):
        # frame 0: 12 7f -> info declares 127 bytes, 0 remain => corrupt
        # frame 1: 08 00 -> value = 0 (must still be decoded)
        blob = frame(bytes([0x12, 0x7F])) + frame(bytes([0x08, 0x00]))
        frames, summary = run(blob)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["counts"],
                         {"ok": 1, "bad_payload": 1,
                          "truncated": 0, "framing_error": 0})
        self.assertEqual(frames[0].status, "bad_payload")
        self.assertIn("file offset 4", frames[0].diagnostics[0])
        self.assertEqual(frames[1].values, {"value": 0})
        self.assertEqual(frames[1].presence, ["value"])

    def test_oversized_frame_stops_with_offset(self):
        blob = b"\x00\x00\x10\x01" + b"\x00" * 16  # declares 4097 > 4096
        frames, summary = run(blob)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["stop_reason"], "oversized frame")
        self.assertEqual(summary["counts"]["framing_error"], 1)
        self.assertIn("offset 0", frames[0].diagnostics[0])

    def test_frame_limit(self):
        limits = core.Limits(max_payload_bytes=4096, max_frames=2)
        blob = frame(b"") * 3
        frames, summary = core.ingest_bytes(blob, SCHEMA, limits)
        self.assertFalse(summary["complete"])
        self.assertEqual(len(frames), 2)
        self.assertIn("frame limit 2", summary["stop_reason"])

    def test_zero_length_frame_is_valid(self):
        frames, summary = run(frame(b""))
        self.assertTrue(summary["complete"])
        self.assertEqual(frames[0].payload_range, (4, 4))


class CrossCheckWithOfficialLibrary(unittest.TestCase):
    """Same descriptor, arbitrary legal field combinations and orders."""

    def _roundtrip(self, build, expect_values, expect_presence, expect_oneof):
        msg = Sample()
        build(msg)
        payload = msg.SerializeToString()
        frames, summary = run(frame(payload))
        self.assertTrue(summary["complete"])
        rec = frames[0]
        self.assertEqual(rec.status, "ok")
        self.assertEqual(rec.values, expect_values)
        self.assertEqual(sorted(rec.presence), sorted(expect_presence))
        self.assertEqual(rec.oneof, {"choice": expect_oneof})
        # and the official runtime must agree with itself
        again = official(payload)
        self.assertEqual(again, msg)

    def test_value_and_readings(self):
        def build(m):
            m.value = -5
            m.readings.extend([7, -8, 9])
        self._roundtrip(build,
                        {"value": -5, "readings": [7, -8, 9]},
                        ["value", "readings"], None)

    def test_info_only_note(self):
        def build(m):
            m.info.note = "只写 note"
        self._roundtrip(build, {"info": {"note": "只写 note"}},
                        ["info", "info.note"], None)

    def test_explicit_zero_roundtrip(self):
        def build(m):
            m.value = 0
        self._roundtrip(build, {"value": 0}, ["value"], None)

    def test_oneof_text(self):
        def build(m):
            m.text = "hello"
        self._roundtrip(build, {"text": "hello"}, ["text"], "text")

    def test_oneof_details_full(self):
        def build(m):
            m.details.code = 3
            m.details.note = "both"
        self._roundtrip(build, {"details": {"code": 3, "note": "both"}},
                        ["details", "details.code", "details.note"], "details")

    def test_everything_together(self):
        def build(m):
            m.value = 2147483647
            m.info.code = -1
            m.info.note = "n"
            m.details.code = 8
            m.readings.extend([0, -1, 1])
        self._roundtrip(build,
                        {"value": 2147483647,
                         "info": {"code": -1, "note": "n"},
                         "details": {"code": 8},
                         "readings": [0, -1, 1]},
                        ["value", "info", "info.code", "info.note",
                         "details", "details.code", "readings"],
                        "details")

    def test_handcrafted_matches_official_for_each_fixture_frame(self):
        """Fixture frames decoded by us must equal the official decode."""
        with open(os.path.join(FIXTURES, "messages.bin"), "rb") as fh:
            data = fh.read()
        frames, summary = run(data)
        self.assertTrue(summary["complete"])
        for rec in frames:
            msg = official(rec.payload)
            # presence parity
            self.assertEqual(
                rec.oneof.get("choice"), msg.WhichOneof("choice"),
                f"frame {rec.index} oneof mismatch")
            self.assertEqual("value" in rec.presence, msg.HasField("value"))
            self.assertEqual("info" in rec.presence, msg.HasField("info"))
            self.assertEqual(
                rec.values.get("readings", []), list(msg.readings))


class FixtureFiles(unittest.TestCase):
    """End-to-end runs over the provided materials."""

    def _ingest(self, name):
        return core.ingest_file(os.path.join(FIXTURES, name),
                                SCHEMA, LIMITS, descriptor_path=DESCRIPTOR)

    def test_messages_bin(self):
        report = self._ingest("messages.bin")
        self.assertTrue(report["complete"])
        self.assertEqual(report["counts"]["ok"], 5)
        frames = report["frames"]
        # frame 0: empty message; frame 1: explicit zero — presence differs
        self.assertEqual(frames[0]["presence"], [])
        self.assertEqual(frames[1]["presence"], ["value"])
        self.assertEqual(frames[1]["values"], {"value": 0})
        # frame 2: nested merge + mixed repeated
        self.assertEqual(frames[2]["values"]["info"],
                         {"code": 7, "note": "合并"})
        self.assertEqual(frames[2]["values"]["readings"], [-1, 2, 0, 3])
        # frame 3: oneof switched details -> text -> details
        self.assertEqual(frames[3]["oneof"], {"choice": "details"})
        self.assertEqual(frames[3]["values"], {"details": {"note": "new"}})
        # frame 4: same-member merge + unknown field 99
        self.assertEqual(frames[4]["values"],
                         {"details": {"code": 2, "note": "kept"}})
        self.assertEqual(frames[4]["unknown_fields"], [99])
        # every frame keeps digest + byte range
        digest = report["input"]["sha256"]
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # valid hex
        for f in frames:
            self.assertIsNotNone(f["payload_range"])

    def test_truncated_bin(self):
        report = self._ingest("truncated.bin")
        self.assertFalse(report["complete"])
        self.assertEqual(report["stop_reason"], "truncated payload")
        self.assertEqual(report["counts"]["ok"], 5)
        self.assertEqual(report["counts"]["truncated"], 1)
        tail = report["frames"][-1]
        self.assertEqual(tail["status"], "truncated")
        self.assertEqual(tail["payload_range"], [81, 84])
        # earlier good rows must not make the file "complete"
        self.assertFalse(report["complete"])

    def test_bad_payload_bin(self):
        report = self._ingest("bad-payload.bin")
        self.assertTrue(report["complete"])
        self.assertEqual(report["counts"]["bad_payload"], 1)
        self.assertEqual(report["counts"]["ok"], 1)
        bad, good = report["frames"]
        self.assertEqual(bad["status"], "bad_payload")
        self.assertIn("file offset 4", bad["diagnostics"][0])
        self.assertEqual(good["values"], {"value": 0})
        self.assertEqual(good["presence"], ["value"])

    def test_schema_proto_matches_descriptor(self):
        warnings = schema_mod.check_schema_proto(SCHEMA, SCHEMA_PROTO)
        self.assertEqual(warnings, [])

    def test_inspect_frame(self):
        with open(os.path.join(FIXTURES, "messages.bin"), "rb") as fh:
            data = fh.read()
        result = core.inspect_frame(data, SCHEMA, LIMITS, 2)
        self.assertEqual(result["frame"]["values"]["readings"], [-1, 2, 0, 3])
        paths = [e["path"] for e in result["wire_walk"]]
        self.assertEqual(paths, ["info", "info", "readings",
                                 "readings", "readings"])
        self.assertTrue(result["hex"])

    def test_inspect_out_of_range(self):
        with self.assertRaises(IndexError):
            core.inspect_frame(b"\x00\x00\x00\x00", SCHEMA, LIMITS, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
