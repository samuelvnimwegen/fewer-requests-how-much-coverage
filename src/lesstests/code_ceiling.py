"""A white box yardstick for one recording. The selection method never uses server lines."""

import math
import statistics
import time
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_array

from lesstests.java import JavaTest
from lesstests.selection import Budget, CoverageSelector, RandomSelector
from lesstests.versions import VersionPlan


@dataclass(slots=True)
class CeilingResult:
    """A feasible suite and the proof available for its objective, in tests or lines."""

    tests: list[str]
    lines: int
    bound: int
    gap: float
    status: str
    seconds: float


class LineMatrix:
    """Groups source lines reached by exactly the same tests into one row."""

    def __init__(self, lines: dict[JavaTest, set[int]]) -> None:
        """Keep test bitmaps for counting and one sparse constraint per distinct row."""
        self.tests = sorted(lines)
        owners: dict[int, int] = {}
        self.bitmaps: list[int] = []
        for index, test in enumerate(self.tests):
            bit = 1 << index
            reached = lines[test]
            bitmap = bytearray(max(reached, default=0) // 8 + 1)
            for line in reached:
                owners[line] = owners.get(line, 0) | bit
                bitmap[line // 8] |= 1 << (line % 8)
            self.bitmaps.append(int.from_bytes(bitmap, "little"))
        self.total = len(owners)
        patterns = Counter(owners.values())
        self.weights = np.array(list(patterns.values()), dtype=float)
        rows, columns = [], []
        for row, mask in enumerate(patterns):
            while mask:
                bit = mask & -mask
                rows.append(row)
                columns.append(bit.bit_length() - 1)
                mask ^= bit
        incidence = csr_array((np.ones(len(rows)), (rows, columns)), shape=(len(patterns), len(self.tests)))
        self.reach = CoverageSelector.reach_constraint(incidence)

    def count(self, indices: list[int]) -> int:
        """Count the distinct lines reached.

        Returns:
            The number of lines.
        """
        covered = 0
        for index in indices:
            covered |= self.bitmaps[index]
        return covered.bit_count()

    def greedy(self, size: int, target: int) -> list[int]:
        """Pick the test with the largest gain until either limit is reached.

        Returns:
            The test indices.
        """
        chosen = []
        remaining = list(range(len(self.tests)))
        covered = 0
        while len(chosen) < size and covered.bit_count() < target:
            best = max(remaining, key=lambda index: (self.bitmaps[index] & ~covered).bit_count())
            chosen.append(best)
            covered |= self.bitmaps[best]
            remaining.remove(best)
        return chosen


class CeilingProgram(ABC):
    """Solve a white box reference problem and keep a feasible answer at the time limit."""

    def __init__(self, matrix: LineMatrix, seconds: float) -> None:
        """Set the time limit for one HiGHS call.

        Raises:
            ValueError: If the time limit is invalid.
        """
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("The solver needs a positive finite time limit")
        self.matrix = matrix
        self.seconds = seconds
        self.test_count = np.r_[np.ones(len(matrix.tests)), np.zeros(len(matrix.weights))]
        self.line_count = np.r_[np.zeros(len(matrix.tests)), matrix.weights]

    def _solve(
        self,
        objective: np.ndarray,
        constraint: LinearConstraint,
        fallback: list[int],
        natural_bound: int,
        start: float,
        feasible: Callable[[list[int]], bool],
    ) -> CeilingResult:
        """Keep the best known suite and a bound in the original objective units.

        Returns:
            The feasible suite and proof status.

        Raises:
            RuntimeError: If the solver fails before finding a solution or reaching the limit.
        """
        size = len(self.matrix.tests)
        value = self._objective(fallback)
        chosen = fallback
        bound = natural_bound
        status = "optimal by bound"
        if value != natural_bound:
            solved = milp(
                objective,
                integrality=self.test_count,
                bounds=Bounds(0, 1),
                constraints=[self.matrix.reach, constraint],
                options={"time_limit": self.seconds, "mip_rel_gap": 0.0},
            )
            if solved.status not in (0, 1):
                raise RuntimeError(f"The server code program failed: {solved.message}")
            status = "optimal" if solved.status == 0 else "time limit"
            if solved.x is not None:
                candidate = np.flatnonzero(solved.x[:size] > 0.5).tolist()
                if feasible(candidate) and self._objective(candidate) <= value:
                    chosen = candidate
            elif solved.status == 1:
                status = "time limit without an incumbent"
            dual = solved.mip_dual_bound
            if dual is not None and math.isfinite(dual):
                bound = max(bound, math.ceil(dual - 1e-6))
            value = self._objective(chosen)
            if bound == value and solved.status == 1:
                status = "optimal by bound"
            if status == "optimal" and value != bound:
                status = "feasible fallback"
        gap = max(0.0, value - bound) / max(1, abs(value))
        return CeilingResult(
            [self.matrix.tests[i].id for i in sorted(chosen)],
            self.matrix.count(chosen),
            abs(bound),
            gap,
            status,
            time.monotonic() - start,
        )

    @abstractmethod
    def _objective(self, indices: list[int]) -> int:
        """Score a feasible suite for minimisation.

        Returns:
            The score.
        """


class MostLines(CeilingProgram):
    """Maximise lines at exactly the study size."""

    def select(self, size: int, known: list[list[int]] | None = None) -> CeilingResult:
        """Find the best suite at this size, including any measured suites as candidates.

        Returns:
            The suite and an upper bound on its lines.

        Raises:
            ValueError: If the size is outside the pool or a candidate has a different size.
        """
        start = time.monotonic()
        if not 0 <= size <= len(self.matrix.tests):
            raise ValueError("The requested size is outside the test pool")
        candidates = [self.matrix.greedy(size, self.matrix.total + 1), *(known or [])]
        if any(len(set(indices)) != size or len(indices) != size for indices in candidates):
            raise ValueError("Every candidate must have the requested size")
        fallback = min(candidates, key=self._objective)
        constraint = LinearConstraint(self.test_count, size, size)
        return self._solve(
            -self.line_count, constraint, fallback, -self.matrix.total, start, lambda ids: len(ids) == size
        )

    def _objective(self, indices: list[int]) -> int:
        """Score the suite.

        Returns:
            The negative line count.
        """
        return -self.matrix.count(indices)


class FewestTests(CeilingProgram):
    """Minimise tests for a given share of the lines reached by the full recording."""

    def select(self, share: float) -> CeilingResult:
        """Round the target up to a whole line.

        Returns:
            The suite and a lower bound on its test count.

        Raises:
            ValueError: If the share is invalid.
        """
        start = time.monotonic()
        if not 0 < share <= 1:
            raise ValueError("The target share must be above zero and at most one")
        target = math.ceil(Decimal(str(share)) * self.matrix.total)
        greedy = self.matrix.greedy(len(self.matrix.tests), target)
        constraint = LinearConstraint(self.line_count, target, np.inf)
        return self._solve(
            self.test_count, constraint, greedy, int(target > 0), start, lambda ids: self.matrix.count(ids) >= target
        )

    def _objective(self, indices: list[int]) -> int:
        """Score the suite.

        Returns:
            The test count.
        """
        return len(indices)


class CeilingStudy:
    """Compare a saved study with white box picks from the same eligible tests."""

    def __init__(self, lines: dict[JavaTest, set[int]], pool: list[JavaTest], seconds: float) -> None:
        """Use every recorded test for the reference and only eligible tests for fixed sizes."""
        self.full = LineMatrix(lines)
        self.pool = self.full if len(pool) == len(lines) else LineMatrix({test: lines[test] for test in pool})
        self.indices = {test.id: i for i, test in enumerate(self.pool.tests)}
        self.seconds = seconds

    def measure(self, study: dict[str, Any], shares: list[float], plan: VersionPlan | None = None) -> None:
        """Add the white box results and the fraction of the gap Selected closes.

        Raises:
            ValueError: If the saved study disagrees with the recording.
        """
        if study["tests"] != len(self.full.tests) or study["pool"] != len(self.pool.tests):
            raise ValueError("The saved study and recording differ in size")
        fewest = FewestTests(self.full, self.seconds)
        study["minimum_code_covers"] = {str(share): asdict(fewest.select(share)) for share in shares}
        for selected in study["versions"]:
            if selected["version"] != "selected":
                continue
            drawn = [v for v in study["versions"] if v["budget"] == selected["budget"] and v["version"] == "random"]
            suites = [selected, *drawn]
            indices = [[self.indices[test] for test in suite["tests"]] for suite in suites]
            counts = [self.pool.count(group) for group in indices]
            for suite, count in zip(suites, counts, strict=True):
                if count != suite["code"]["kept"]["lines"] or self.full.total != suite["code"]["of"]["lines"]:
                    raise ValueError("The saved line counts differ from the recording")
            selected.update(self._compare(selected["kept"], indices, counts))
        if plan is not None:
            version = next((v for v in plan.versions if v.name == VersionPlan.SELECT), None)
            if version is None or any(test.id not in self.indices for test in version.tests):
                raise ValueError("The plan needs a Select suite from the eligible pool")
            size = len(version.tests)
            indices = [[self.indices[test.id] for test in version.tests]]
            for draw in range(1, study["draws"] + 1):
                if size:
                    budget = Budget(size / len(self.pool.tests))
                    selector = RandomSelector(RandomSelector.seed_for(study["seed"], budget, draw))
                    sample = selector.draw(self.pool.tests, size)
                    indices.append([self.indices[test.id] for test in sample])
                else:
                    indices.append([])
            row = self._compare(size, indices, [self.pool.count(group) for group in indices])
            if version.code and version.code.kept.lines != row["selected_lines"]:
                raise ValueError("The plan line count differs from the recording")
            study["plan_select"] = {"tests": [t.id for t in version.tests], **row}

    def _compare(self, size: int, indices: list[list[int]], counts: list[int]) -> dict[str, Any]:
        """Compare Selected and its Random draws at the same test count.

        Returns:
            Line counts, the ceiling and the fraction of the gap closed.
        """
        selected, *draws = counts
        random = statistics.mean(draws) if draws else None
        ceiling = MostLines(self.pool, self.seconds).select(size, indices)

        def ratio(lines: int) -> float | None:
            """Calculate the fraction of the gap closed.

            Returns:
                The ratio when the denominator is positive.
            """
            return (selected - random) / (lines - random) if random is not None and lines > random else None

        return dict(
            selected_lines=selected,
            random_mean=random,
            code_ceiling=asdict(ceiling),
            gap_closed=ratio(ceiling.lines),
            gap_closed_at_bound=ratio(ceiling.bound),
        )
