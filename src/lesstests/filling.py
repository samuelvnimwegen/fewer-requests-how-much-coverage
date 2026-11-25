import math
import random
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cached_property

import numpy as np
from numpy.typing import NDArray
from scipy.sparse import csr_matrix

from lesstests.coverage import SuiteCoverage
from lesstests.java import JavaTest
from lesstests.recording import RecordedTest
from lesstests.selection import CoverageMatrix, CoverageSelector, CoverBudget
from lesstests.traffic_values import FieldValues, Item, Traffic


@dataclass(slots=True)
class FillPool:
    """The eligible tests and the data available before measuring any server code."""

    matrix: CoverageMatrix
    coverage: SuiteCoverage
    requests: dict[JavaTest, int]
    records: dict[JavaTest, RecordedTest]
    prefix: str
    seed: int


class FillingRule(ABC):
    """Choose the tests at a percentage budget."""

    name: str
    uses_traffic = False

    def __init__(self, pool: FillPool) -> None:
        """Keep the eligible pool."""
        self.pool = pool

    @abstractmethod
    def select(self, size: int) -> list[JavaTest]:
        """Pick from the eligible pool.

        Returns:
            The selected suite.
        """


class FewestRequests(FillingRule):
    """Use the original fixed size solver so its choices among ties also reproduce."""

    name = "fewest-requests"

    def select(self, size: int) -> list[JavaTest]:
        """Minimise requests among suites with the greatest reach.

        Returns:
            The selected suite.
        """
        return CoverageSelector().select(self.pool.matrix, size)


class FloorFill(FillingRule):
    """Spend only the places left after keeping a smallest cover."""

    @cached_property
    def floor(self) -> list[JavaTest]:
        """The smallest cover of the eligible tests."""
        return CoverBudget().pick(self.pool.matrix, self.pool.coverage, self.pool.requests)

    def select(self, size: int) -> list[JavaTest]:
        """Use ordinary Select when the floor does not fit.

        Returns:
            The sorted suite at this size.
        """
        size = min(size, len(self.pool.matrix.tests))
        if size < len(self.floor):
            return CoverageSelector().select(self.pool.matrix, size)
        if size == len(self.floor):
            return self.floor
        remaining = sorted(set(self.pool.matrix.tests).difference(self.floor))
        return sorted([*self.floor, *self.fill(self.floor, remaining, size - len(self.floor))])

    @abstractmethod
    def fill(self, floor: list[JavaTest], remaining: list[JavaTest], count: int) -> list[JavaTest]:
        """Choose more tests from the remaining pool.

        Returns:
            The additions.
        """


class RandomFill(FloorFill):
    """Sample from the tests outside the cover."""

    name = "random-fill"

    def fill(self, floor: list[JavaTest], remaining: list[JavaTest], count: int) -> list[JavaTest]:
        """Draw without replacement.

        Returns:
            The sampled tests.
        """
        return random.Random(self.pool.seed).sample(remaining, count)


class CoverTwice(FloorFill):
    """Prefer a second test for each RESTCov item reached only once."""

    name = "cover-twice"

    def fill(self, floor: list[JavaTest], remaining: list[JavaTest], count: int) -> list[JavaTest]:
        """Break equal gains by fewer requests and then test name.

        Returns:
            The tests that add the most second hits.
        """
        coverage = self.pool.coverage.per_test
        hits = Counter(item for test in floor for item in coverage[test].items)
        once = {item for item, n in hits.items() if n == 1}
        candidates = {test: once.intersection(coverage[test].items) for test in remaining}
        additions = []
        for _ in range(count):
            best = max(candidates, key=lambda t: (len(candidates[t]), -self.pool.requests[t]))
            gained = candidates.pop(best)
            additions.append(best)
            for items in candidates.values():
                items.difference_update(gained)
        return additions


class MostNewValues(FloorFill):
    """Prefer tests with unusual request values that the floor has not shown."""

    name = "most-new-values"
    uses_traffic = True

    @cached_property
    def items(self) -> dict[JavaTest, set[Item]]:
        """The operations, statuses, fields and statistical boundary items of each test."""
        tests = self.pool.matrix.tests
        items: list[set[Item]] = [set() for _ in tests]
        fields: dict[Item, FieldValues] = defaultdict(FieldValues)
        for index, test in enumerate(tests):
            found = items[index]
            for exchange in self.pool.records[test].exchanges:
                request = exchange.request
                op, query = Traffic.operation(request, self.pool.prefix)
                found.update([("operation", op), ("status", op, str(exchange.response.status))])
                for source, values in (("query", query), ("body", Traffic.fields(request.body, request.headers))):
                    for path, value in values:
                        field = (source, op, path)
                        found.add(field)
                        edge = fields[field].observe(index, value)
                        if edge:
                            found.add(field + edge)
        for field, observed in fields.items():
            for value_item, indices in observed.boundaries():
                for index in indices:
                    items[index].add(field + value_item)
        return dict(zip(tests, items, strict=True))

    def fill(self, floor: list[JavaTest], remaining: list[JavaTest], count: int) -> list[JavaTest]:
        """Break ties in new weight by total weight and then by test name.

        Returns:
            The tests with greatest successive gains.
        """
        frequency = Counter(item for items in self.items.values() for item in items)
        weights = {item: -math.log2(n / len(self.items)) for item, n in frequency.items()}
        covered = {item for test in floor for item in self.items[test]}
        total = {test: round(math.fsum(map(weights.__getitem__, self.items[test])), 12) for test in remaining}
        additions = []
        candidates = {test: self.items[test] - covered for test in remaining}
        for _ in range(count):
            best = max(
                candidates, key=lambda t: (round(math.fsum(map(weights.__getitem__, candidates[t])), 12), total[t])
            )
            gained = candidates.pop(best)
            additions.append(best)
            for unseen in candidates.values():
                unseen.difference_update(gained)
        return additions


class MostDifferent(FloorFill):
    """Prefer traffic vectors with the greatest squared distance from the kept span."""

    name = "most-different"
    uses_traffic = True

    def _tokens(self, record: RecordedTest) -> Iterator[Item]:
        previous = ""
        for exchange in record.exchanges:
            req, res = exchange.request, exchange.response
            op, query = Traffic.operation(req, self.pool.prefix)
            yield ("operation", op)
            if previous:
                yield ("sequence", previous, op)
            previous = op
            for key, value in query:
                yield ("query", op, key, Traffic.token(value))
            for path, value in Traffic.fields(req.body, req.headers):
                yield ("request field", op, path)
                yield ("request value", op, path, Traffic.token(value))
            status = str(res.status)
            yield ("status", op, status)
            for path, value in Traffic.fields(res.body, res.headers):
                yield ("response field", op, status, path)
                if res.status >= 400 or isinstance(value, bool):
                    yield ("response value", op, status, path, Traffic.token(value))

    @cached_property
    def kernel(self) -> NDArray[np.float64]:
        """The inner products of all pairs of weighted traffic vectors."""
        rows = [set(self._tokens(self.pool.records[test])) for test in self.pool.matrix.tests]
        frequency = Counter(item for row in rows for item in row)
        columns = {item: i for i, item in enumerate(sorted(frequency))}
        weights = [1 + math.log((1 + len(rows)) / (1 + frequency[item])) for item in columns]
        indices = [sorted(columns[item] for item in row) for row in rows]
        offsets = np.cumsum([0, *map(len, indices)])
        flat = [i for row in indices for i in row]
        vectors = csr_matrix(([weights[i] for i in flat], flat, offsets), shape=(len(rows), len(columns)))
        return (vectors @ vectors.T).toarray()

    def fill(self, floor: list[JavaTest], remaining: list[JavaTest], count: int) -> list[JavaTest]:
        """Project out the floor first, skipping dependent vectors in it.

        Returns:
            The additions in greedy order.
        """
        tests = self.pool.matrix.tests
        index = {test: i for i, test in enumerate(tests)}
        eligible = np.zeros(len(tests), dtype=bool)
        eligible[[index[test] for test in remaining]] = True
        diagonal = self.kernel.diagonal()
        residual = diagonal.copy()
        basis = np.zeros((len(floor) + count, len(tests)))
        picks = []
        for step in range(len(basis)):
            if step < len(floor):
                pivot = index[floor[step]]
            else:
                score = np.where(eligible, residual, -1)
                pivot = int(np.argmax(score))
                if score[pivot] <= 1e-12:
                    pivot = int(np.argmax(np.where(eligible, diagonal, -1)))
                picks.append(tests[pivot])
                eligible[pivot] = False
            if residual[pivot] > 1e-12:
                # Subtract the projection before normalising the new basis vector.
                projection = basis[:step, pivot] @ basis[:step]
                basis[step] = (self.kernel[pivot] - projection) / math.sqrt(residual[pivot])
                residual = np.maximum(0, residual - basis[step] ** 2)
        return picks


FILLING_RULES = {rule.name: rule for rule in (FewestRequests, RandomFill, CoverTwice, MostNewValues, MostDifferent)}
