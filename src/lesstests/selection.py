import math
import random
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, OptimizeResult, milp
from scipy.sparse import csr_array, lil_array

from lesstests.coverage import CoverageItem, CoverageSet, SuiteCoverage
from lesstests.java import JavaTest
from lesstests.traffic import FAMILIES, TrafficCoverage


@dataclass(frozen=True, slots=True)
class Budget:
    """The share of tests a smaller suite keeps, like 0.75 for 75 percent."""

    share: float

    def __post_init__(self) -> None:
        """Check the share.

        Raises:
            ValueError: If the share is not above 0 and at most 1. This also catches nan and inf.
        """
        if not 0 < self.share <= 1:
            raise ValueError(f"A budget keeps more than 0% and at most 100% of the tests, not {self.share * 100}%")

    def size(self, tests: int) -> int:
        """Turn the share into a number of tests, rounding halves up and keeping at least one.

        Returns:
            The number of tests to keep.
        """
        # Float maths can leave an exact half just under .5, like 0.29 * 50 = 14.499999999999998.
        return max(1, math.floor(round(self.share * tests, 9) + 0.5))

    @property
    def label(self) -> str:
        """The share as a percentage, like `75%` or `12.5%`."""
        return f"{self.share * 100:g}%"


class CoverageMatrix:
    """Which tests reach which coverage items, plus the requests each test sends."""

    def __init__(self, coverage: SuiteCoverage, requests: dict[JavaTest, int], tests: list[JavaTest]) -> None:
        """Build the matrix over the given tests. Items no test reaches are left out."""
        self.tests = sorted(tests)
        self.requests = [requests[test] for test in self.tests]
        self.items: list[CoverageItem] = sorted({item for test in self.tests for item in coverage.per_test[test].items})
        index = {item: number for number, item in enumerate(self.items)}
        self.reaches = [sorted(index[item] for item in coverage.per_test[test].items) for test in self.tests]


class Selector(Protocol):
    """Picks a given number of tests from a matrix."""

    name: str

    def select(self, matrix: CoverageMatrix, size: int) -> list[JavaTest]:
        """Pick tests.

        Returns:
            The picked tests.
        """
        ...


class RandomSelector:
    """Picks tests at random. One instance gives one draw, so a study makes one per draw with its own seed."""

    name = "random"

    def __init__(self, seed: int) -> None:
        """Seed the draw."""
        self._random = random.Random(seed)

    def select(self, matrix: CoverageMatrix, size: int) -> list[JavaTest]:
        """Pick tests at random.

        Returns:
            The picked tests, sorted.
        """
        return sorted(self._random.sample(matrix.tests, min(size, len(matrix.tests))))

    @staticmethod
    def seed_for(base: int, budget: Budget, draw: int) -> int:
        """Give every budget and draw its own seed, so adding a budget leaves the other draws unchanged.

        Returns:
            The seed.
        """
        return base * 1_000_003 + round(budget.share * 1000) * 1_009 + draw


class CoverageSelector:
    """The Select step. An integer program keeps the most coverage items, then the fewest requests among those.

    It solves two programs. The first finds the most items any group of that size can reach. The second keeps
    that many items and asks for the fewest requests. A tie left after both is broken by the solver.
    """

    name = "selected"

    def select(self, matrix: CoverageMatrix, size: int) -> list[JavaTest]:
        """Pick the tests.

        Returns:
            The picked tests, sorted.

        Raises:
            RuntimeError: If the solver finds no plan.
        """
        size = min(size, len(matrix.tests))
        if not matrix.items:
            ranked = sorted(zip(matrix.requests, matrix.tests, strict=True))
            return sorted(test for _, test in ranked[:size])
        tests, items = len(matrix.tests), len(matrix.items)
        constraints = self.constraints(matrix.reaches, items, size)
        integrality = np.concatenate([np.ones(tests), np.zeros(items)])
        bounds = Bounds(0, 1)

        first = self.maximum(tests, items, constraints)
        if not first.success:
            raise RuntimeError(f"The coverage program failed: {first.message}")
        reachable = round(-first.fun)

        keep_items = LinearConstraint(
            np.concatenate([np.zeros(tests), np.ones(items)]).reshape(1, -1), reachable, np.inf
        )
        fewest_requests = np.concatenate([np.asarray(matrix.requests, dtype=float), np.zeros(items)])
        second = milp(fewest_requests, constraints=[*constraints, keep_items], integrality=integrality, bounds=bounds)
        if not second.success:
            raise RuntimeError(f"The request program failed: {second.message}")
        return sorted(test for test, chosen in zip(matrix.tests, second.x[:tests], strict=True) if chosen > 0.5)

    @staticmethod
    def maximum(
        tests: int, items: int, constraints: list[LinearConstraint], seconds: float | None = None
    ) -> OptimizeResult:
        """Maximise the number of items reached.

        Returns:
            The first solve before breaking ties by request count.
        """
        return milp(
            np.r_[np.zeros(tests), -np.ones(items)],
            constraints=constraints,
            integrality=np.r_[np.ones(tests), np.zeros(items)],
            bounds=Bounds(0, 1),
            options={"time_limit": seconds, "mip_rel_gap": 0.0} if seconds is not None else None,
        )

    @staticmethod
    def constraints(reaches: list[list[int]], items: int, size: int) -> list[LinearConstraint]:
        """Item i counts only when a picked test reaches it, and exactly `size` tests are picked.

        Returns:
            The two constraint blocks.
        """
        tests = len(reaches)
        reach = lil_array((items, tests + items))
        for test, reached in enumerate(reaches):
            for item in reached:
                reach[item, test] = -1
        for item in range(items):
            reach[item, tests + item] = 1
        count = np.concatenate([np.ones(tests), np.zeros(items)]).reshape(1, -1)
        return [LinearConstraint(csr_array(reach), -np.inf, 0), LinearConstraint(count, size, size)]


@dataclass(frozen=True, slots=True)
class CoverOptions:
    """Traffic families and the minimum share of their combined items."""

    families: tuple[str, ...] = ()
    share: float = 1.0

    def __post_init__(self) -> None:
        """Check the cover settings.

        Raises:
            ValueError: If a family is unknown or the share is outside the unit interval.
        """
        unknown = set(self.families).difference(FAMILIES)
        if unknown:
            raise ValueError(f"Unknown traffic families: {', '.join(sorted(unknown))}")
        if not 0 <= self.share <= 1:
            raise ValueError("Traffic share must be between 0 and 1")


class SmallestCover:
    """Picks the fewest tests that still reach every item of a target, then the fewest requests among those.

    The first program finds the smallest size. The second keeps that size and asks for the fewest requests.
    """

    def select(
        self,
        candidates: dict[JavaTest, CoverageSet],
        requests: dict[JavaTest, int],
        target: CoverageSet,
        traffic: TrafficCoverage | None = None,
        share: float = 1.0,
    ) -> list[JavaTest]:
        """Pick the tests.

        Returns:
            The picked tests, sorted.

        Raises:
            ValueError: If the candidates cannot cover the required items or the share is invalid.
            RuntimeError: If the solver finds no plan.
        """
        if not 0 <= share <= 1:
            raise ValueError("Traffic share must be between 0 and 1")
        tests = sorted(candidates)
        items = sorted(target.items)

        # Items with the same witnesses count together even when only part of the traffic is required.
        groups: Counter[frozenset[JavaTest]] = Counter()
        if traffic is not None and share > 0:
            groups.update(
                frozenset(family[item].intersection(candidates))
                for family in traffic.witnesses.values()
                for item in sorted(family)
            )
        if not items and not groups:
            return []

        # Decimal multiplication keeps a share such as 0.14 from rounding just above seven of fifty.
        required = math.ceil(Decimal(str(share)) * groups.total())
        if groups.total() - groups[frozenset()] < required:
            raise ValueError("The eligible tests cannot cover the required traffic share")
        partial = 0 < required < groups.total()
        variables = len(tests) + (len(groups) if partial else 0)
        index = {item: number for number, item in enumerate(items)}
        reach = lil_array((len(items) + len(groups), variables))
        for column, test in enumerate(tests):
            for item in candidates[test].items:
                if item in index:
                    reach[index[item], column] = 1
        columns = {test: i for i, test in enumerate(tests)}
        for i, group in enumerate(groups):
            for test in group:
                reach[len(items) + i, columns[test]] = 1
            if partial:
                reach[len(items) + i, len(tests) + i] = -1
        covered = csr_array(reach)
        unreachable = [items[row] for row in range(len(items)) if covered.indptr[row] == covered.indptr[row + 1]]
        if unreachable:
            raise ValueError(f"No candidate reaches {len(unreachable)} target items, such as {unreachable[0]}")
        required_rows = len(items) if partial else len(items) + len(groups)
        constraints = [LinearConstraint(covered[:required_rows, :], 1, np.inf)]
        if partial:
            constraints.append(LinearConstraint(covered[required_rows:, :], 0, np.inf))

            # A group can count only if a selected test witnesses it. Its weight is the number of items it represents.
            weights = np.r_[np.zeros(len(tests)), list(groups.values())]
            constraints.append(LinearConstraint(weights, required, np.inf))
        indicators = np.zeros(variables - len(tests))
        count = np.r_[np.ones(len(tests)), indicators]
        bounds = Bounds(0, 1)
        options = {"mip_rel_gap": 0.0} if traffic is not None else None

        # The group indicators are whole numbers too. A fraction inside the solver's tolerance would count as an item.
        whole = np.ones(variables)
        fewest_tests = self.minimum(count, constraints, options)
        if not fewest_tests.success:
            raise RuntimeError(f"The cover program failed: {fewest_tests.message}")
        size = round(fewest_tests.fun)
        constraints.append(LinearConstraint(count, size, size))
        cost = np.r_[[requests[test] for test in tests], indicators]
        cheapest = milp(cost, constraints=constraints, integrality=whole, bounds=bounds, options=options)
        if not cheapest.success:
            raise RuntimeError(f"The request program failed: {cheapest.message}")
        return [test for test, chosen in zip(tests, cheapest.x[: len(tests)], strict=True) if chosen > 0.5]

    @staticmethod
    def minimum(
        count: np.ndarray, constraints: list[LinearConstraint], options: dict[str, float] | None
    ) -> OptimizeResult:
        """Find the fewest tests satisfying the cover.

        Returns:
            The size solve before breaking ties by request count.
        """
        return milp(
            count, constraints=constraints, integrality=np.ones(len(count)), bounds=Bounds(0, 1), options=options
        )
