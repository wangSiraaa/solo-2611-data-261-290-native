"""Exact, inspectable sampling over a three-table one-to-many join.

The join in scope is::

    roots r
      INNER JOIN left_rows  l ON l.root_id = r.id
      INNER JOIN right_rows t ON t.root_id = r.id

A root with ``a`` left rows and ``b`` right rows contributes ``a * b`` rows to
the join (bag semantics).  The join is ordered lexicographically by
``(root id, left id, right id)`` using Unicode code-point order.  The index in
this module stores only per-root ordered child ids, cardinalities and
cumulative rank bounds; it never materialises the N joined rows.

Sampling is *with replacement* and uniform over the N joined rows:

    rng = random.Random(seed)
    for _ in range(k):
        rank = rng.randrange(N)
        result = locate(index, rank)

Roots are not sampled first.  In particular, a root's probability of appearing
is proportional to its contribution ``a * b``, not to ``a`` or ``b`` alone.

Public API
----------
``index(roots, left_rows, right_rows)``
    Read/validate the three row sources and build a :class:`JoinIndex`.

``locate(index, rank)``
    Return the unique ``(root, left, right)`` source rows for a global rank.

``rank_of(index, (root_id, left_id, right_id))``
    Inverse of ``locate`` for a joined combination.

``sample(index, k, seed)``
    Draw k ranks with the exact protocol above.

``audit(index, ..., k=..., seed=...)``
    Independently enumerate a small join and verify every rank, the inverse
    rank and the sample protocol.

Row sources may be a JSONL path, a directory of ``*.jsonl`` shards, a list of
paths, an iterable of mappings, an open text file, or a single mapping.  This
makes line shuffling and file sharding irrelevant to the result.
"""

from __future__ import annotations

import bisect
import json
import os
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "JoinSamplingError",
    "InvalidInputError",
    "DuplicateIdError",
    "UnknownForeignKeyError",
    "OutOfRangeRank",
    "NotAJoinResult",
    "NoPopulation",
    "AuditError",
    "RootEntry",
    "JoinIndex",
    "SampleResult",
    "index",
    "locate",
    "rank_of",
    "rank",
    "build_index",
    "sample",
    "audit",
    "main",
]

JOIN_SQL = (
    "roots r INNER JOIN left_rows l ON l.root_id=r.id "
    "INNER JOIN right_rows t ON t.root_id=r.id"
)
ORDERING = ("root_id", "left_id", "right_id")
SAMPLING_METHOD = "random.Random(seed).randrange(N) repeated k times, with replacement"

_MISSING = object()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JoinSamplingError(ValueError):
    """Base class for all errors raised by this module."""


class InvalidInputError(JoinSamplingError):
    """A source row or JSONL file is malformed or violates the schema."""


class DuplicateIdError(InvalidInputError):
    """An id is not unique within its table."""


class UnknownForeignKeyError(InvalidInputError):
    """A child row references a root that does not exist."""


class OutOfRangeRank(IndexError, JoinSamplingError):
    """A rank is not an integer in ``[0, N)``."""


class NotAJoinResult(JoinSamplingError):
    """The supplied id tuple is not one of the join's combinations."""


class NoPopulation(JoinSamplingError):
    """Sampling was requested from an empty join."""


class AuditError(JoinSamplingError):
    """The independent audit found an inconsistency."""


# ---------------------------------------------------------------------------
# Source loading and validation
# ---------------------------------------------------------------------------


def _read_jsonl_file(path: Path, table: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise InvalidInputError(f"{table}: cannot open {path}: {exc}") from exc

    with handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InvalidInputError(
                    f"{table}: invalid JSON at {path}:{lineno}: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise InvalidInputError(
                    f"{table}: record at {path}:{lineno} is not a JSON object"
                )
            rows.append(value)
    return rows


def _read_text_stream(stream: Any, table: str) -> list[dict[str, Any]]:
    data = stream.read()
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidInputError(f"{table}: stream is not UTF-8: {exc}") from exc
    if not isinstance(data, str):
        raise InvalidInputError(f"{table}: .read() did not return text")

    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InvalidInputError(
                f"{table}: invalid JSON on line {lineno}: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise InvalidInputError(
                f"{table}: record on line {lineno} is not a JSON object"
            )
        rows.append(value)
    return rows


def _read_path(path: Path, table: str) -> list[dict[str, Any]]:
    if path.is_dir():
        shards = sorted(
            (p for p in path.iterdir() if p.is_file() and p.suffix == ".jsonl"),
            key=lambda p: p.name,
        )
        if not shards:
            raise InvalidInputError(f"{table}: no .jsonl shards found in {path}")
        rows: list[dict[str, Any]] = []
        for shard in shards:
            rows.extend(_read_jsonl_file(shard, table))
        return rows
    if not path.is_file():
        raise InvalidInputError(f"{table}: path does not exist or is not a file: {path}")
    return _read_jsonl_file(path, table)


def _load_source(source: Any, table: str) -> list[dict[str, Any]]:
    """Normalise a source into a list of row mappings.

    Accepted forms: one path, a directory of shards, a sequence of paths, a
    file-like object, a mapping (one row), or an iterable of mappings.
    """

    if isinstance(source, Mapping):
        return [dict(source)]

    if isinstance(source, (str, os.PathLike)):
        return _read_path(Path(source), table)

    if hasattr(source, "read"):
        return _read_text_stream(source, table)

    if isinstance(source, Iterable):
        items = list(source)
        if not items:
            return []
        if all(isinstance(item, (str, os.PathLike)) for item in items):
            rows: list[dict[str, Any]] = []
            for item in items:
                rows.extend(_read_path(Path(item), table))
            return rows
        rows = []
        for position, item in enumerate(items, 1):
            if not isinstance(item, Mapping):
                raise InvalidInputError(
                    f"{table}: item {position} is not a mapping/record"
                )
            rows.append(dict(item))
        return rows

    raise InvalidInputError(f"{table}: unsupported source type {type(source).__name__}")


def _check_id(value: Any, table: str, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise InvalidInputError(
            f"{table}: {field} must be a non-empty string, got {value!r}"
        )
    return value


def _validate_rows(
    rows: list[dict[str, Any]], table: str
) -> tuple[dict[str, dict[str, Any]], tuple[dict[str, Any], ...]]:
    by_id: dict[str, dict[str, Any]] = {}
    for position, row in enumerate(rows, 1):
        if "id" not in row:
            raise InvalidInputError(f"{table}: record {position} has no 'id'")
        row_id = _check_id(row["id"], table, "id")
        if row_id in by_id:
            raise DuplicateIdError(f"{table}: duplicate id {row_id!r}")
        by_id[row_id] = row

    # Python string ordering is Unicode code-point ordering.
    ordered = tuple(by_id[row_id] for row_id in sorted(by_id))
    return by_id, ordered


def _validate_child_root_ids(
    rows: Iterable[dict[str, Any]], table: str, root_ids: set[str]
) -> None:
    for row in rows:
        if "root_id" not in row:
            raise InvalidInputError(
                f"{table}: record {row['id']!r} has no 'root_id'"
            )
        root_id = _check_id(row["root_id"], table, "root_id")
        if root_id not in root_ids:
            raise UnknownForeignKeyError(
                f"{table}: record {row['id']!r} has unknown root_id {root_id!r}"
            )


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RootEntry:
    """One root's index entry.

    ``start`` and ``end`` delimit the root's half-open global rank interval
    ``[start, end)``.  When either side is empty, ``start == end``.
    """

    root_id: str
    root: Mapping[str, Any]
    left_ids: tuple[str, ...]
    right_ids: tuple[str, ...]
    left_rows: tuple[Mapping[str, Any], ...]
    right_rows: tuple[Mapping[str, Any], ...]
    join_count: int
    start: int
    end: int

    @property
    def left_count(self) -> int:
        return len(self.left_ids)

    @property
    def right_count(self) -> int:
        return len(self.right_ids)


class JoinIndex:
    """Immutable, inspectable index of the joined population.

    The index stores, for each root, the ordered left/right ids and rows, its
    contribution ``a * b``, and its cumulative rank bounds.  It does not store
    the N joined combinations.
    """

    def __init__(
        self,
        roots: tuple[dict[str, Any], ...],
        left_rows: tuple[dict[str, Any], ...],
        right_rows: tuple[dict[str, Any], ...],
    ) -> None:
        self.roots = roots
        self.left_rows = left_rows
        self.right_rows = right_rows

        left_by_root: dict[str, list[dict[str, Any]]] = {
            root["id"]: [] for root in roots
        }
        right_by_root: dict[str, list[dict[str, Any]]] = {
            root["id"]: [] for root in roots
        }
        for row in left_rows:
            left_by_root[row["root_id"]].append(row)
        for row in right_rows:
            right_by_root[row["root_id"]].append(row)

        entries: list[RootEntry] = []
        ends: list[int] = []
        total = 0
        for root in roots:
            root_id = root["id"]
            # rows entering this method are globally sorted by id
            left_group = left_by_root[root_id]
            right_group = right_by_root[root_id]
            left_ids = tuple(row["id"] for row in left_group)
            right_ids = tuple(row["id"] for row in right_group)
            join_count = len(left_ids) * len(right_ids)
            start = total
            total += join_count
            entries.append(
                RootEntry(
                    root_id=root_id,
                    root=root,
                    left_ids=left_ids,
                    right_ids=right_ids,
                    left_rows=tuple(left_group),
                    right_rows=tuple(right_group),
                    join_count=join_count,
                    start=start,
                    end=total,
                )
            )
            ends.append(total)

        self.entries = tuple(entries)
        self._ends = tuple(ends)
        self._entry_by_root_id = {
            entry.root_id: position for position, entry in enumerate(self.entries)
        }
        self._left_lookup: dict[str, tuple[int, int]] = {}
        self._right_lookup: dict[str, tuple[int, int]] = {}
        for position, entry in enumerate(self.entries):
            for offset, left_id in enumerate(entry.left_ids):
                self._left_lookup[left_id] = (position, offset)
            for offset, right_id in enumerate(entry.right_ids):
                self._right_lookup[right_id] = (position, offset)

    # -- basic inspection -------------------------------------------------

    @property
    def N(self) -> int:
        """Total number of joined rows (bag semantics)."""

        return self._ends[-1] if self._ends else 0

    @property
    def n(self) -> int:
        """Alias for :attr:`N`."""

        return self.N

    @property
    def total(self) -> int:
        """Alias for :attr:`N`."""

        return self.N

    @property
    def index_entry_count(self) -> int:
        """Number of per-root index entries (one per root)."""

        return len(self.entries)

    @property
    def stored_id_count(self) -> int:
        """Number of ordered child ids stored in the index."""

        return len(self.left_rows) + len(self.right_rows)

    def __len__(self) -> int:
        return self.N

    def __getitem__(self, rank: int) -> dict[str, Any]:
        return self.locate(rank)

    def __repr__(self) -> str:
        return (
            f"JoinIndex(roots={len(self.roots)}, "
            f"left_rows={len(self.left_rows)}, "
            f"right_rows={len(self.right_rows)}, N={self.N})"
        )

    # -- rank -> rows -----------------------------------------------------

    def locate(self, rank: int) -> dict[str, Any]:
        """Return the joined row at ``rank``.

        The returned mapping contains the global ``rank``, the three source
        ids and the original three source records.
        """

        if isinstance(rank, bool) or not isinstance(rank, int):
            raise OutOfRangeRank(f"rank must be an integer, got {rank!r}")
        if rank < 0 or rank >= self.N:
            raise OutOfRangeRank(
                f"rank {rank} is outside the join's rank range [0, {self.N})"
            )

        # First entry whose end is strictly greater than rank.  This skips
        # zero-contribution roots automatically, since their end == start.
        entry_position = bisect.bisect_right(self._ends, rank)
        entry = self.entries[entry_position]
        offset = rank - entry.start
        right_count = len(entry.right_ids)
        left_position, right_position = divmod(offset, right_count)

        return {
            "rank": rank,
            "root_id": entry.root_id,
            "left_id": entry.left_ids[left_position],
            "right_id": entry.right_ids[right_position],
            "root": entry.root,
            "left": entry.left_rows[left_position],
            "right": entry.right_rows[right_position],
        }

    # -- rows -> rank -----------------------------------------------------

    def rank(self, *key: Any) -> int:
        """Return the global rank of an id tuple.

        Accepts either ``index.rank(root_id, left_id, right_id)`` or
        ``index.rank((root_id, left_id, right_id))``.  A tuple of ids that is
        not a row of the join raises :class:`NotAJoinResult`.
        """

        if len(key) == 1 and isinstance(key[0], (tuple, list)):
            key = tuple(key[0])
        if len(key) != 3:
            raise NotAJoinResult(
                "expected a (root_id, left_id, right_id) triple of length 3"
            )

        root_id, left_id, right_id = key
        for label, value in (
            ("root_id", root_id),
            ("left_id", left_id),
            ("right_id", right_id),
        ):
            if not isinstance(value, str) or value == "":
                raise NotAJoinResult(f"{label} must be a non-empty string, got {value!r}")

        entry_position = self._entry_by_root_id.get(root_id)
        if entry_position is None:
            raise NotAJoinResult(f"unknown root_id {root_id!r}")

        left_info = self._left_lookup.get(left_id)
        if left_info is None:
            raise NotAJoinResult(f"unknown left id {left_id!r}")
        right_info = self._right_lookup.get(right_id)
        if right_info is None:
            raise NotAJoinResult(f"unknown right id {right_id!r}")

        left_entry_position, left_offset = left_info
        right_entry_position, right_offset = right_info
        if left_entry_position != entry_position:
            raise NotAJoinResult(
                f"left id {left_id!r} does not belong to root {root_id!r}"
            )
        if right_entry_position != entry_position:
            raise NotAJoinResult(
                f"right id {right_id!r} does not belong to root {root_id!r}"
            )

        entry = self.entries[entry_position]
        return entry.start + left_offset * len(entry.right_ids) + right_offset

    def rank_of(self, *key: Any) -> int:
        """Alias for :meth:`rank`."""

        return self.rank(*key)

    # -- manifests --------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        """Return per-root cardinalities, bounds and the population size."""

        per_root = [
            {
                "root_id": entry.root_id,
                "left_count": entry.left_count,
                "right_count": entry.right_count,
                "join_count": entry.join_count,
                "start": entry.start,
                "end": entry.end,
                "rank_interval": [entry.start, entry.end],
            }
            for entry in self.entries
        ]
        return {
            "join": JOIN_SQL,
            "ordering": list(ORDERING),
            "N": self.N,
            "root_count": len(self.roots),
            "left_row_count": len(self.left_rows),
            "right_row_count": len(self.right_rows),
            "index_entry_count": self.index_entry_count,
            "stored_id_count": self.stored_id_count,
            "rank_range": [0, self.N],
            "roots": per_root,
            "sampling_method": SAMPLING_METHOD,
            "sample_distribution": (
                "Uniform over the N joined rows (bag semantics); roots are not "
                "sampled first, so each root's share is proportional to "
                "left_count * right_count."
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the complete inspectable index as JSON-serialisable data."""

        return {
            "manifest": self.manifest(),
            "entries": [
                {
                    "root_id": entry.root_id,
                    "left_ids": list(entry.left_ids),
                    "right_ids": list(entry.right_ids),
                    "left_count": entry.left_count,
                    "right_count": entry.right_count,
                    "join_count": entry.join_count,
                    "start": entry.start,
                    "end": entry.end,
                }
                for entry in self.entries
            ],
        }


def index(
    roots: Any, left_rows: Any, right_rows: Any
) -> JoinIndex:
    """Read, validate and index the three row sources.

    Unknown child foreign keys, duplicate ids and malformed ids are rejected;
    they are never silently dropped.  Rows for a root with no children simply
    contribute zero joined rows.
    """

    root_rows = _load_source(roots, "roots")
    left_input = _load_source(left_rows, "left_rows")
    right_input = _load_source(right_rows, "right_rows")

    root_by_id, roots_ordered = _validate_rows(root_rows, "roots")
    left_by_id, left_ordered = _validate_rows(left_input, "left_rows")
    right_by_id, right_ordered = _validate_rows(right_input, "right_rows")

    root_ids = set(root_by_id)
    _validate_child_root_ids(left_ordered, "left_rows", root_ids)
    _validate_child_root_ids(right_ordered, "right_rows", root_ids)

    return JoinIndex(roots_ordered, left_ordered, right_ordered)


def locate(idx: JoinIndex, rank: int) -> dict[str, Any]:
    """Return the joined row at global ``rank``.

    Module-level form of :meth:`JoinIndex.locate`.
    """

    if not isinstance(idx, JoinIndex):
        raise TypeError("locate() expects a JoinIndex")
    return idx.locate(rank)


def rank_of(idx: JoinIndex, *key: Any) -> int:
    """Inverse of :func:`locate`: map a joined id triple back to its rank."""

    if not isinstance(idx, JoinIndex):
        raise TypeError("rank_of() expects a JoinIndex")
    return idx.rank(*key)


def rank(idx: JoinIndex, *key: Any) -> int:
    """Alias for :func:`rank_of`."""

    return rank_of(idx, *key)


def build_index(roots: Any, left_rows: Any, right_rows: Any) -> JoinIndex:
    """Alias for :func:`index`."""

    return index(roots, left_rows, right_rows)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


class SampleResult:
    """Result of :func:`sample`.

    The object behaves like the list of sample rows (iteration, ``len`` and
    integer indexing) and additionally exposes ``samples``, ``manifest``,
    ``k``, ``seed`` and ``N``.  String indexing is accepted as a convenience:
    ``result["samples"]`` and ``result["manifest"]`` both work.
    """

    def __init__(
        self,
        samples: list[dict[str, Any]],
        manifest: dict[str, Any],
        *,
        k: int,
        seed: int,
        N: int,
    ) -> None:
        self.samples = samples
        self.manifest = manifest
        self.k = k
        self.seed = seed
        self.N = N

    @property
    def items(self) -> list[dict[str, Any]]:
        return self.samples

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.samples

    def __iter__(self):
        return iter(self.samples)

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, key):
        if isinstance(key, str):
            if key in ("samples", "items", "rows"):
                return self.samples
            if key == "manifest":
                return self.manifest
            if key == "k":
                return self.k
            if key == "seed":
                return self.seed
            if key == "N":
                return self.N
            raise KeyError(key)
        return self.samples[key]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SampleResult):
            return (
                self.samples == other.samples
                and self.manifest == other.manifest
                and self.k == other.k
                and self.seed == other.seed
                and self.N == other.N
            )
        if isinstance(other, list):
            return self.samples == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"SampleResult(k={self.k}, seed={self.seed}, N={self.N}, samples={self.samples!r})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "seed": self.seed,
            "N": self.N,
            "samples": self.samples,
            "manifest": self.manifest,
        }


def _check_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise JoinSamplingError(f"{name} must be an integer, got {value!r}")
    if value < 0:
        raise JoinSamplingError(f"{name} must be non-negative, got {value!r}")
    return value


def _check_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise JoinSamplingError(f"{name} must be an integer, got {value!r}")
    return value


def sample(
    idx: JoinIndex | Any,
    k: int,
    seed: int,
    *,
    left_rows: Any = None,
    right_rows: Any = None,
) -> SampleResult:
    """Draw ``k`` joined rows uniformly with replacement.

    Exactly the specified protocol is used::

        rng = random.Random(seed)
        for _ in range(k):
            rank = rng.randrange(N)
            locate(index, rank)

    Repeated ranks (and therefore repeated source rows) are valid.  When
    ``N == 0``, ``k == 0`` succeeds with an empty result, while ``k > 0``
    raises :class:`NoPopulation`.
    """

    if not isinstance(idx, JoinIndex):
        if left_rows is None or right_rows is None:
            raise TypeError("sample() expects a JoinIndex or all three sources")
        idx = index(idx, left_rows, right_rows)

    k = _check_nonnegative_int(k, "k")
    seed = _check_int(seed, "seed")

    if idx.N == 0 and k > 0:
        raise NoPopulation(
            "cannot draw a sample: the join population is empty (N=0)"
        )

    results: list[dict[str, Any]] = []
    if idx.N > 0:
        rng = random.Random(seed)
        for _ in range(k):
            # randrange is called in sequence and the result is looked up
            # immediately; no root is chosen with a float probability.
            results.append(idx.locate(rng.randrange(idx.N)))

    return SampleResult(results, idx.manifest(), k=k, seed=seed, N=idx.N)


# ---------------------------------------------------------------------------
# Independent audit
# ---------------------------------------------------------------------------


def _enumerate_join(idx: JoinIndex) -> list[dict[str, Any]]:
    """Brute-force the join from the source rows, independently of locate()."""

    left_by_root: dict[str, list[Mapping[str, Any]]] = {}
    right_by_root: dict[str, list[Mapping[str, Any]]] = {}
    for row in idx.left_rows:
        left_by_root.setdefault(row["root_id"], []).append(row)
    for row in idx.right_rows:
        right_by_root.setdefault(row["root_id"], []).append(row)

    enumerated: list[dict[str, Any]] = []
    for root in sorted(idx.roots, key=lambda row: row["id"]):
        root_id = root["id"]
        left_group = sorted(left_by_root.get(root_id, []), key=lambda row: row["id"])
        right_group = sorted(right_by_root.get(root_id, []), key=lambda row: row["id"])
        for left in left_group:
            for right in right_group:
                enumerated.append(
                    {
                        "rank": len(enumerated),
                        "root_id": root_id,
                        "left_id": left["id"],
                        "right_id": right["id"],
                        "root": root,
                        "left": left,
                        "right": right,
                    }
                )
    return enumerated


def _audit_matches(
    actual: Mapping[str, Any], expected: Mapping[str, Any], context: str
) -> None:
    for field in ("rank", "root_id", "left_id", "right_id", "root", "left", "right"):
        if field not in actual:
            raise AuditError(f"{context}: locate/sample result has no {field!r}")
        if actual[field] != expected[field]:
            raise AuditError(
                f"{context}: field {field!r} is {actual[field]!r}, "
                f"expected {expected[field]!r}"
            )


def audit(
    index_or_roots: JoinIndex | Any,
    left_rows: Any = None,
    right_rows: Any = None,
    k: int | None = None,
    seed: int | None = None,
    *,
    sample_result: SampleResult | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Independently verify the index, inverse ranks and sampling protocol.

    The audit brute-force enumerates the small join from the source records
    (it does not use ``locate`` to discover the order), checks every global
    rank, checks that every enumerated triple maps back to its rank, and then
    checks the sample against ``random.Random(seed).randrange(N)`` repeated k
    times.  This is a protocol/consistency audit, not a finite-frequency proof
    of uniformity.

    ``audit(index, k=..., seed=...)`` and
    ``audit(roots, left_rows, right_rows, k=..., seed=...)`` are both accepted.
    If ``sample_result`` is supplied, its k/seed are used when omitted and its
    rows are checked as well.
    """

    if isinstance(index_or_roots, JoinIndex):
        idx = index_or_roots
        if left_rows is not None or right_rows is not None:
            # Permit audit(index, k, seed).
            if (
                isinstance(left_rows, int)
                and not isinstance(left_rows, bool)
                and isinstance(right_rows, int)
                and not isinstance(right_rows, bool)
                and k is None
                and seed is None
            ):
                k, seed = left_rows, right_rows
            else:
                raise TypeError(
                    "audit(): when passing a JoinIndex, pass k and seed as "
                    "keywords (or as the second and third positional values)"
                )
    else:
        if left_rows is None or right_rows is None:
            raise TypeError("audit() needs either a JoinIndex or all three sources")
        idx = index(index_or_roots, left_rows, right_rows)

    if sample_result is not None:
        if isinstance(sample_result, SampleResult):
            if k is None:
                k = sample_result.k
            elif k != sample_result.k:
                raise AuditError(
                    f"audit(): k={k} differs from sample result k={sample_result.k}"
                )
            if seed is None:
                seed = sample_result.seed
            elif seed != sample_result.seed:
                raise AuditError(
                    f"audit(): seed={seed} differs from sample result "
                    f"seed={sample_result.seed}"
                )
            if sample_result.N != idx.N:
                raise AuditError(
                    f"audit(): sample N={sample_result.N} differs from index N={idx.N}"
                )
        else:
            if k is None:
                k = len(sample_result)

    if k is None:
        k = 0
    if seed is None:
        seed = 0
    k = _check_nonnegative_int(k, "k")
    seed = _check_int(seed, "seed")

    # --- independent full enumeration ------------------------------------
    expected_rows = _enumerate_join(idx)
    enumerated_n = len(expected_rows)
    if enumerated_n != idx.N:
        raise AuditError(
            f"audit(): enumerated {enumerated_n} joined rows but index says N={idx.N}"
        )

    tuples = [
        (row["root_id"], row["left_id"], row["right_id"]) for row in expected_rows
    ]
    if tuples != sorted(tuples):
        raise AuditError("audit(): enumerated join is not in lexicographic order")
    if len(set(tuples)) != enumerated_n:
        raise AuditError("audit(): enumerated join contains duplicate combinations")

    # --- every rank, and the inverse rank --------------------------------
    for expected in expected_rows:
        rank = expected["rank"]
        located = idx.locate(rank)
        _audit_matches(located, expected, f"locate({rank})")

        triple = (expected["root_id"], expected["left_id"], expected["right_id"])
        inverse = idx.rank(triple)
        if inverse != rank:
            raise AuditError(
                f"audit(): rank{triple} returned {inverse}, expected {rank}"
            )

    # Out-of-range ranks must be rejected (including when N == 0).
    for bad_rank in (-1, idx.N):
        try:
            idx.locate(bad_rank)
        except (IndexError, ValueError):
            pass
        else:
            raise AuditError(f"audit(): locate({bad_rank}) did not raise")

    # --- sample protocol -------------------------------------------------
    samples_checked = 0
    expected_no_population = False
    if sample_result is None:
        computed = None
        if idx.N == 0 and k > 0:
            try:
                sample(idx, k, seed)
            except NoPopulation:
                expected_no_population = True
            else:
                raise AuditError(
                    "audit(): sample() did not raise NoPopulation for N=0, k>0"
                )
        else:
            computed = sample(idx, k, seed)
        actual_samples = computed.samples if computed is not None else []
        actual_k = computed.k if computed is not None else k
        actual_seed = computed.seed if computed is not None else seed
        actual_N = computed.N if computed is not None else idx.N
        actual_manifest = computed.manifest if computed is not None else None
    else:
        if isinstance(sample_result, SampleResult):
            actual_samples = sample_result.samples
            actual_k = sample_result.k
            actual_seed = sample_result.seed
            actual_N = sample_result.N
            actual_manifest = sample_result.manifest
        else:
            actual_samples = list(sample_result)
            actual_k = len(actual_samples)
            actual_seed = seed
            actual_N = idx.N
            actual_manifest = None

        if idx.N == 0 and len(actual_samples) > 0:
            raise AuditError("audit(): supplied sample is non-empty but N=0")
        if actual_k != k:
            raise AuditError(f"audit(): sample has k={actual_k}, expected k={k}")
        if actual_seed != seed:
            raise AuditError(
                f"audit(): sample has seed={actual_seed}, expected seed={seed}"
            )
        if actual_N != idx.N:
            raise AuditError(
                f"audit(): sample has N={actual_N}, expected N={idx.N}"
            )

    if actual_manifest is not None and actual_manifest.get("N") != idx.N:
        raise AuditError("audit(): sample manifest N differs from the index")

    if idx.N > 0:
        rng = random.Random(seed)
        expected_ranks = [rng.randrange(idx.N) for _ in range(k)]
        if len(actual_samples) != k:
            raise AuditError(
                f"audit(): sample has {len(actual_samples)} rows, expected {k}"
            )
        for position, (actual, expected_rank) in enumerate(
            zip(actual_samples, expected_ranks)
        ):
            if actual.get("rank") != expected_rank:
                raise AuditError(
                    f"audit(): sample {position} rank is {actual.get('rank')!r}, "
                    f"expected randrange result {expected_rank}"
                )
            _audit_matches(
                actual,
                expected_rows[expected_rank],
                f"sample({position}) at rank {expected_rank}",
            )
        samples_checked = k
    else:
        if k > 0 and not expected_no_population:
            raise AuditError("audit(): N=0 with k>0 must not produce samples")
        samples_checked = 0

    return {
        "ok": True,
        "N": idx.N,
        "index_entry_count": idx.index_entry_count,
        "stored_id_count": idx.stored_id_count,
        "rank_count_checked": enumerated_n,
        "one_to_one_and_order_ok": True,
        "inverse_rank_ok": True,
        "k": k,
        "seed": seed,
        "samples_checked": samples_checked,
        "expected_no_population": expected_no_population,
        "independent_enumeration": True,
        "uniformity_note": (
            "The sample protocol is verified against randrange; no finite "
            "frequency test is used or needed."
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_index_from_args(args: Any) -> JoinIndex:
    return index(args.roots, args.left, args.right)


def main(argv: list[str] | None = None) -> int:
    """Command-line interface: ``python join_sampling.py --help``."""

    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Exact uniform sampling over roots INNER JOIN left_rows INNER JOIN "
            "right_rows without materialising all joined rows."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_sources(subparser: Any) -> None:
        subparser.add_argument("roots", help="roots JSONL path (or directory of shards)")
        subparser.add_argument("left", help="left_rows JSONL path (or shard directory)")
        subparser.add_argument("right", help="right_rows JSONL path (or shard directory)")

    index_parser = subparsers.add_parser(
        "index", help="build and print the inspectable index"
    )
    add_sources(index_parser)

    locate_parser = subparsers.add_parser(
        "locate", help="show the joined row at a global rank"
    )
    add_sources(locate_parser)
    locate_parser.add_argument("rank", type=int)

    sample_parser = subparsers.add_parser(
        "sample", help="draw a with-replacement uniform sample"
    )
    add_sources(sample_parser)
    sample_parser.add_argument("--k", type=int, required=True)
    sample_parser.add_argument("--seed", type=int, required=True)

    audit_parser = subparsers.add_parser(
        "audit", help="independently verify all ranks and the sample protocol"
    )
    add_sources(audit_parser)
    audit_parser.add_argument("--k", type=int, default=0)
    audit_parser.add_argument("--seed", type=int, default=0)

    args = parser.parse_args(argv)

    try:
        idx = _build_index_from_args(args)
        if args.command == "index":
            payload: Any = idx.to_dict()
        elif args.command == "locate":
            payload = idx.locate(args.rank)
        elif args.command == "sample":
            payload = sample(idx, args.k, args.seed).to_dict()
        elif args.command == "audit":
            samples = sample(idx, args.k, args.seed) if not (idx.N == 0 and args.k > 0) else None
            payload = audit(idx, k=args.k, seed=args.seed, sample_result=samples)
        else:  # pragma: no cover - argparse prevents this
            parser.error(f"unknown command {args.command!r}")
            return 2
    except JoinSamplingError as exc:
        parser.exit(2, f"error: {exc}\n")
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
