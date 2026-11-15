"""Tests for join_sampling.py (standard library only, Python 3.11)."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

import join_sampling as js

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOTS = FIXTURES / "roots.jsonl"
LEFT = FIXTURES / "left_rows.jsonl"
RIGHT = FIXTURES / "right_rows.jsonl"
BAD_LEFT = FIXTURES / "bad-left_rows.jsonl"


def fixture_index() -> js.JoinIndex:
    return js.index(ROOTS, LEFT, RIGHT)


class IndexTests(unittest.TestCase):
    def test_bag_cardinalities_and_zero_sides(self) -> None:
        idx = fixture_index()
        # a:1*1=1, b:3*2=6, c:2*0=0, 空:0*0=0, 茶:1*3=3
        self.assertEqual(idx.N, 10)
        self.assertEqual([e.root_id for e in idx.entries], ["a", "b", "c", "空", "茶"])
        self.assertEqual(
            [e.join_count for e in idx.entries], [1, 6, 0, 0, 3]
        )
        self.assertEqual([(e.start, e.end) for e in idx.entries],
                         [(0, 1), (1, 7), (7, 7), (7, 7), (7, 10)])
        self.assertEqual(idx.index_entry_count, 5)
        self.assertEqual(idx.stored_id_count, 13)

    def test_index_does_not_store_joined_rows(self) -> None:
        idx = fixture_index()
        self.assertEqual(idx.index_entry_count, len(idx.roots))
        # Joined population is larger than the stored child ids.
        self.assertGreater(idx.N, idx.index_entry_count)
        self.assertNotEqual(idx.N, len(idx.left_rows) + len(idx.right_rows))

    def test_locate_order(self) -> None:
        idx = fixture_index()
        expected = [
            ("a", "la0", "ra0"),
            ("b", "lb0", "rb0"), ("b", "lb0", "rb1"),
            ("b", "lb1", "rb0"), ("b", "lb1", "rb1"),
            ("b", "lb2", "rb0"), ("b", "lb2", "rb1"),
            ("茶", "l茶0", "r茶0"), ("茶", "l茶0", "r茶1"), ("茶", "l茶0", "r茶2"),
        ]
        self.assertEqual(
            [(idx.locate(r)["root_id"], idx.locate(r)["left_id"], idx.locate(r)["right_id"])
             for r in range(idx.N)],
            expected,
        )
        for rank in range(idx.N):
            row = js.locate(idx, rank)
            self.assertEqual(row["rank"], rank)
            self.assertEqual(js.rank_of(idx, (row["root_id"], row["left_id"], row["right_id"])), rank)

    def test_unicode_codepoint_order(self) -> None:
        idx = fixture_index()
        self.assertEqual([e.root_id for e in idx.entries], sorted(e.root_id for e in idx.entries))
        # 空 U+7A7B sorts before 茶 U+8336, both after ASCII.
        self.assertLess("空", "茶")

    def test_equal_values_with_distinct_ids_are_distinct_records(self) -> None:
        idx = js.index(
            [{"id": "r", "label": "same"}],
            [{"id": "z1", "root_id": "r", "payload": "same"},
             {"id": "z0", "root_id": "r", "payload": "same"}],
            [{"id": "t", "root_id": "r", "payload": "same"}],
        )
        self.assertEqual(idx.N, 2)
        self.assertEqual([e.left_ids for e in idx.entries], [("z0", "z1")])

    def test_manifest(self) -> None:
        m = fixture_index().manifest()
        self.assertEqual(m["N"], 10)
        self.assertEqual(len(m["roots"]), 5)
        self.assertIn("not sampled first", m["sample_distribution"])
        self.assertEqual(m["rank_range"], [0, 10])


class SampleTests(unittest.TestCase):
    def test_protocol_matches_randrange(self) -> None:
        idx = fixture_index()
        result = js.sample(idx, 12, 261270)
        rng = random.Random(261270)
        self.assertEqual([s["rank"] for s in result.samples], [rng.randrange(10) for _ in range(12)])
        self.assertEqual(result.k, 12)
        self.assertEqual(result.seed, 261270)
        self.assertEqual(result.N, 10)
        self.assertEqual(result.manifest["N"], 10)

    def test_samples_include_sources_and_identity(self) -> None:
        for row in js.sample(fixture_index(), 12, 261270).samples:
            self.assertEqual(set(row), {"rank", "root_id", "left_id", "right_id",
                                        "root", "left", "right"})
            self.assertEqual(row["root"]["id"], row["root_id"])
            self.assertEqual(row["left"]["id"], row["left_id"])
            self.assertEqual(row["right"]["id"], row["right_id"])
            self.assertEqual(row["left"]["root_id"], row["root_id"])
            self.assertEqual(row["right"]["root_id"], row["root_id"])

    def test_reproducible_and_repeats_allowed(self) -> None:
        idx = fixture_index()
        a = js.sample(idx, 12, 261270)
        b = js.sample(idx, 12, 261270)
        self.assertEqual(a.samples, b.samples)
        # The fixture draw contains repeated ranks, which is legitimate.
        self.assertGreater(len(a.samples), len({s["rank"] for s in a.samples}))

    def test_k_and_seed_changes(self) -> None:
        idx = fixture_index()
        self.assertEqual(js.sample(idx, 0, 1).samples, [])
        self.assertEqual(len(js.sample(idx, 1, 0).samples), 1)
        self.assertEqual(len(js.sample(idx, 20, 5).samples), 20)
        r0 = js.sample(idx, 3, 261270)
        r1 = js.sample(idx, 3, 261271)
        self.assertEqual(len(r0), 3)
        self.assertEqual(len(r1), 3)
        # Not required that different seeds differ, only that each follows the protocol.

    def test_empty_population(self) -> None:
        idx = js.index([], [], [])
        empty = js.sample(idx, 0, 7)
        self.assertEqual(empty.samples, [])
        self.assertEqual(empty.N, 0)
        with self.assertRaises(js.NoPopulation):
            js.sample(idx, 1, 7)
        # A root with children on only one side also yields N=0.
        one_sided = js.index([{"id": "r"}], [{"id": "l", "root_id": "r"}], [])
        self.assertEqual(one_sided.N, 0)
        with self.assertRaises(js.NoPopulation):
            js.sample(one_sided, 2, 1)

    def test_root_probability_not_parent_first(self) -> None:
        # Root with a large left fanout but no right rows contributes zero;
        # a root with 1 left and 5 right rows contributes 5.
        idx = js.index(
            [{"id": "big"}, {"id": "narrow"}],
            [{"id": f"big{i}", "root_id": "big"} for i in range(9)]
            + [{"id": "narrow0", "root_id": "narrow"}],
            [{"id": f"n{i}", "root_id": "narrow"} for i in range(5)],
        )
        self.assertEqual(idx.N, 5)
        self.assertTrue(all(s["root_id"] == "narrow"
                            for s in js.sample(idx, 50, 1).samples))


class ErrorTests(unittest.TestCase):
    def test_unknown_foreign_key_rejected(self) -> None:
        with self.assertRaises(js.UnknownForeignKeyError):
            js.index(ROOTS, BAD_LEFT, RIGHT)
        with self.assertRaises(js.UnknownForeignKeyError):
            js.index([{"id": "r"}], [], [{"id": "t", "root_id": "missing"}])

    def test_duplicate_and_invalid_ids_rejected(self) -> None:
        with self.assertRaises(js.DuplicateIdError):
            js.index([{"id": "r"}, {"id": "r"}], [], [])
        with self.assertRaises(js.InvalidInputError):
            js.index([{"id": ""}], [], [])
        with self.assertRaises(js.InvalidInputError):
            js.index([{"id": 5}], [], [])
        with self.assertRaises(js.InvalidInputError):
            js.index([{}], [], [])
        with self.assertRaises(js.InvalidInputError):
            js.index([], [{"id": "l"}], [])
        with self.assertRaises(js.InvalidInputError):
            js.index([], [], [{"id": "t", "root_id": ""}])

    def test_out_of_range_ranks(self) -> None:
        idx = fixture_index()
        for bad in (-1, idx.N, idx.N + 1, 1.0, True, "0"):
            with self.assertRaises(js.OutOfRangeRank):
                idx.locate(bad)

    def test_non_join_combinations(self) -> None:
        idx = fixture_index()
        cases = [
            ("unknown", "la0", "ra0"),   # unknown root
            ("b", "lc0", "rb0"),         # left belongs elsewhere
            ("b", "lb0", "ra0"),         # right belongs elsewhere
            ("b", "lc0", "r茶0"),        # both belong elsewhere
            ("c", "lc0", "rb0"),         # c has no right rows
            ("c", "lc0", "r茶0"),        # combination never occurs
            ("", "la0", "ra0"),          # empty id
        ]
        for case in cases:
            with self.assertRaises(js.NotAJoinResult):
                idx.rank(case)

    def test_malformed_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roots.jsonl"
            path.write_text('{"id": "a"}\nnot json\n', encoding="utf-8")
            with self.assertRaises(js.InvalidInputError):
                js.index(path, [], [])


class InvarianceTests(unittest.TestCase):
    def _shatter(self, source: Path, directory: Path) -> Path:
        lines = [line for line in source.read_text(encoding="utf-8").splitlines() if line]
        target = directory / source.stem
        target.mkdir()
        for shard in range(3):
            subset = list(reversed(lines[shard::3]))
            (target / f"shard-{shard}.jsonl").write_text(
                "\n".join(subset) + ("\n" if subset else ""), encoding="utf-8"
            )
        return target

    def test_line_order_and_sharding_invariance(self) -> None:
        idx = fixture_index()
        with tempfile.TemporaryDirectory() as tmp:
            shuffled_idx = js.index(
                self._shatter(ROOTS, Path(tmp)),
                self._shatter(LEFT, Path(tmp)),
                self._shatter(RIGHT, Path(tmp)),
            )
        self.assertEqual(shuffled_idx.N, idx.N)
        for rank in range(idx.N):
            self.assertEqual(shuffled_idx.locate(rank), idx.locate(rank))
        self.assertEqual(
            js.sample(shuffled_idx, 12, 261270).samples,
            js.sample(idx, 12, 261270).samples,
        )

    def test_in_memory_iterables_give_same_result(self) -> None:
        idx = fixture_index()
        rows = lambda path: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        from_ram = js.index(rows(ROOTS), rows(LEFT), rows(RIGHT))
        self.assertEqual(from_ram.N, idx.N)
        self.assertEqual(js.sample(from_ram, 12, 261270).samples,
                         js.sample(idx, 12, 261270).samples)

    def test_add_remove_rows_within_schema(self) -> None:
        idx = fixture_index()
        added = js.index(
            ROOTS,
            [json.loads(line) for line in LEFT.read_text(encoding="utf-8").splitlines() if line]
            + [{"id": "lb3", "root_id": "b", "payload": "same"}],
            [json.loads(line) for line in RIGHT.read_text(encoding="utf-8").splitlines() if line],
        )
        # extra left row under b: 3*2 -> 4*2
        self.assertEqual(added.N, idx.N + 2)
        # b's interval grows by 2 and later roots shift.
        self.assertEqual(added.entries[1].join_count, 8)
        self.assertEqual(added.entries[4].start, 9)
        self.assertEqual(added.rank(("茶", "l茶0", "r茶0")), 9)

        removed = js.index(
            [{"id": "a"}, {"id": "b"}, {"id": "茶"}],  # drop c, 空
            [json.loads(line) for line in LEFT.read_text(encoding="utf-8").splitlines()
             if line and json.loads(line)["root_id"] in {"a", "b", "茶"}],
            [json.loads(line) for line in RIGHT.read_text(encoding="utf-8").splitlines() if line],
        )
        self.assertEqual(removed.N, 10)
        self.assertEqual([e.root_id for e in removed.entries], ["a", "b", "茶"])
        removed_all_left = js.index([{"id": "b"}], [], [])
        self.assertEqual(removed_all_left.N, 0)


class AuditTests(unittest.TestCase):
    def test_audit_covers_all_ranks_and_sample(self) -> None:
        report = js.audit(ROOTS, LEFT, RIGHT, k=12, seed=261270)
        self.assertTrue(report["ok"])
        self.assertEqual(report["N"], 10)
        self.assertEqual(report["rank_count_checked"], 10)
        self.assertEqual(report["samples_checked"], 12)
        self.assertTrue(report["one_to_one_and_order_ok"])
        self.assertTrue(report["inverse_rank_ok"])

    def test_audit_accepts_index_and_sample_result(self) -> None:
        idx = fixture_index()
        result = js.sample(idx, 12, 261270)
        report = js.audit(idx, k=result.k, seed=result.seed, sample_result=result)
        self.assertTrue(report["ok"])
        self.assertTrue(js.audit(idx, 3, 42)["ok"])

    def test_audit_empty_population(self) -> None:
        idx = js.index([], [], [])
        self.assertTrue(js.audit(idx, k=0, seed=1)["ok"])
        self.assertTrue(js.audit(idx, k=3, seed=1)["expected_no_population"])

    def test_audit_detects_bad_sample(self) -> None:
        idx = fixture_index()
        result = js.sample(idx, 4, 5)
        tampered = js.SampleResult(
            list(result.samples), result.manifest, k=result.k, seed=result.seed,
            N=result.N,
        )
        tampered.samples[0] = dict(tampered.samples[0], rank=((tampered.samples[0]["rank"] + 1) % idx.N))
        with self.assertRaises(js.AuditError):
            js.audit(idx, k=4, seed=5, sample_result=tampered)


class ScopeTests(unittest.TestCase):
    def test_scope_is_respected(self) -> None:
        scope = json.loads((FIXTURES / "scope.json").read_text(encoding="utf-8"))
        idx = fixture_index()
        self.assertLessEqual(len(idx.roots), scope["max_rows_per_table"])
        self.assertEqual(js.JOIN_SQL, scope["join"])
        self.assertEqual(list(js.ORDERING), ["root_id", "left_id", "right_id"])


if __name__ == "__main__":
    unittest.main()
