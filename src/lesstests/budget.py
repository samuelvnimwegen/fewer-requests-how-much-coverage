"""Describe what a fixed test budget can cover."""

from collections import defaultdict
from dataclasses import asdict, dataclass
from enum import StrEnum
from math import floor, isfinite
from typing import Any

import numpy as np
from scipy.optimize import LinearConstraint, OptimizeResult
from scipy.sparse import csr_array

from lesstests.coverage import Criterion, SuiteCoverage
from lesstests.java import JavaTest
from lesstests.selection import CoverageSelector, SmallestCover
from lesstests.traffic import Item, TrafficCoverage


class Bound(StrEnum):
    """Words for a solver count."""

    EXACT = ""
    LOWER = "at least"
    UPPER = "at most"


@dataclass(slots=True)
class Count:
    """An exact optimum or a bound after the time limit."""

    value: int
    qualifier: Bound = Bound.EXACT

    @classmethod
    def from_solve(cls, result: OptimizeResult, fallback: int, sign: int = 1) -> "Count":
        """Read the optimum or a feasible count when time ran out.

        Returns:
            The count with its direction.

        Raises:
            RuntimeError: If the solver failed.
        """
        if result.success:
            return cls(round(sign * result.fun))
        if result.status != 1:
            raise RuntimeError(f"The budget program failed: {result.message}")
        value = fallback if result.fun is None else round(sign * result.fun)
        return cls(min(value, fallback), Bound.UPPER) if sign == 1 else cls(max(value, fallback), Bound.LOWER)

    def __str__(self) -> str:
        """Format the count.

        Returns:
            The count with any bound.
        """
        return f"{self.qualifier} {self.value}".strip()


@dataclass(slots=True)
class ItemBudget:
    """The best coverage possible for one kind of item."""

    total: int
    most: Count
    minimum: Count
    unavailable: int = 0
    upper: int | None = None

    @property
    def reachable(self) -> int:
        """Number of reachable items.

        Returns:
        The items seen in tests we can pick.
        """
        return self.total - self.unavailable

    @property
    def forced_loss(self) -> int:
        """Loss due to the budget alone.

        Returns:
        The proven loss among reachable items.
        """
        return self.reachable - (self.most.value if self.upper is None else self.upper)

    @property
    def keeps_all(self) -> bool:
        """Whether a full cover fits this budget.

        Returns:
        Whether a pick this size can keep every reachable item.
        """
        return self.most.value == self.reachable


@dataclass(slots=True)
class BudgetWarning:
    """The limits of a budget and the items missing from its pick."""

    size: int
    pool: int
    families: dict[str, ItemBudget]
    restcov: ItemBudget
    traffic: ItemBudget
    combined: ItemBudget
    lost: dict[str, list[Item]]

    @property
    def kinds(self) -> dict[str, ItemBudget]:
        """Counts for each kind and the combined targets.

        Returns:
        The rows for the table and saved results.
        """
        rows = {**self.families, "RESTCov items": self.restcov}
        if self.traffic.total:
            rows.update({"traffic items": self.traffic, "recorded items": self.combined})
        return rows

    def message(self) -> str:
        """Describe forced losses separately from the pick.

        Returns:
            The plain language summary.
        """
        unit = "test" if self.pool == 1 else "tests"
        lines = [f"You asked to keep {self.size} of {self.pool} {unit}."]
        joint = self.combined
        actual = sum(len(items) for items in self.lost.values())
        if not joint.total:
            return " ".join([*lines, "There are no recorded items to lose."])
        if joint.unavailable:
            verb = "comes" if joint.unavailable == 1 else "come"
            lines.append(f"{joint.unavailable} of your {joint.total} items {verb} only from tests that did not pass.")
        adjective = "reachable " if joint.unavailable else ""
        target = f"{adjective}item"
        if joint.keeps_all:
            if actual > joint.unavailable:
                lines.append(
                    f"That size could keep every {target}, but this pick loses {actual - joint.unavailable} of them."
                )
            elif joint.reachable:
                lines.append(f"This pick keeps every {target}.")
        else:
            tests = "test loses" if self.size == 1 else "tests lose"
            if self.restcov.forced_loss:
                lines.append(
                    f"Any {self.size} {tests} at least {self.restcov.forced_loss} of your "
                    f"{self.restcov.reachable} {adjective}RESTCov items."
                )
            elif self.restcov.reachable and self.restcov.keeps_all:
                lines.append(f"That size could keep all {self.restcov.reachable} {adjective}RESTCov items.")
            if self.traffic.reachable and joint.forced_loss:
                lines.append(
                    f"Counting the {self.traffic.reachable} {adjective}traffic items too, "
                    f"the loss is at least {joint.forced_loss}."
                )
            covers = []
            if self.restcov.forced_loss:
                covers.append(("RESTCov items", self.restcov))
            if self.traffic.reachable:
                covers.append(("traffic items", self.traffic))
                if joint.forced_loss:
                    covers.append(("recorded items", joint))
            for kind, row in covers:
                unit = "test" if row.minimum.value == 1 else "tests"
                lines.append(f"Keeping all {adjective}{kind} takes {row.minimum} {unit}.")
            lines.append(f"This pick loses {actual} of your {joint.total} recorded items.")
        if actual:
            lines.append("See the list below.")
        if any(
            row.most.qualifier is not Bound.EXACT or row.minimum.qualifier is not Bound.EXACT
            for row in self.kinds.values()
        ):
            lines.append(
                "The solver stopped after 30 seconds, so counts marked with a bound in the table may not be exact."
            )
        return " ".join(lines)

    def report(self) -> str:
        """Show all counts and a short sample of the actual losses.

        Returns:
            The Markdown shared by the command and saved report.
        """
        lines = [
            self.message(),
            "",
            "| Kind | Recorded | Outside the pool | Most kept | Minimum size loss | Tests for all reachable |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for kind, row in self.kinds.items():
            if row.total:
                loss = Count(row.forced_loss, Bound.LOWER if row.upper is not None else Bound.EXACT)
                lines.append(f"| {kind} | {row.total} | {row.unavailable} | {row.most} | {loss} | {row.minimum} |")
        for kind, items in self.lost.items():
            if items:
                sample = ", ".join(" ".join(item) for item in items[:5])
                lines += ["", f"Missing {kind} ({len(items)}). {sample}" + (" …" if len(items) > 5 else "")]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Save the complete losses with the counts.

        Returns:
            JSON ready fields.
        """
        return {
            "size": self.size,
            "pool": self.pool,
            "kinds": {kind: asdict(row) for kind, row in self.kinds.items()},
            "lost": self.lost,
            "report": self.report(),
            "message": self.message(),
        }


class BudgetAnalysis:
    """Work out the most a pick of each size can keep."""

    def __init__(self, coverage: SuiteCoverage, pool: list[JavaTest], traffic: TrafficCoverage | None) -> None:
        """Index RESTCov and enabled traffic items by family and together."""
        self._pool = sorted(pool)
        names = {
            Criterion.PATH: "paths",
            Criterion.OPERATION: "operations",
            Criterion.PARAMETER: "parameters",
            Criterion.REQUEST_TYPE: "request types",
            Criterion.RESPONSE_TYPE: "response types",
            Criterion.STATUS: "operation and status pairs",
            Criterion.STATUS_CLASS: "status classes",
        }
        self._families: dict[str, dict[Item, set[JavaTest]]] = {kind: defaultdict(set) for kind in names.values()}
        for test, reached in coverage.per_test.items():
            for item in reached.items:
                self._families[names[item.criterion]][(item.key,)].add(test)
        self._minima: dict[str, Count] = {}
        self._groups = dict(self._families)
        self._groups["RESTCov items"] = self._combined(self._families)
        if traffic and traffic.witnesses:
            self._families.update(traffic.witnesses)
            self._groups.update(traffic.witnesses)
            self._groups["traffic items"] = self._combined(traffic.witnesses)
            self._groups["recorded items"] = self._combined(self._families)

    @staticmethod
    def _combined(families: dict[str, dict[Item, set[JavaTest]]]) -> dict[Item, set[JavaTest]]:
        """Prefix each item with its family.

        Returns:
            The witnesses of the combined target.
        """
        return {(name, *item): tests for name, items in families.items() for item, tests in items.items()}

    def measure(self, picked: list[JavaTest]) -> BudgetWarning:
        """Measure the limits for the size of the actual pick.

        Returns:
            The warning with losses grouped by kind.
        """
        kinds = {}
        lost = {}
        for kind, items in self._groups.items():
            missing = sorted(item for item, witnesses in items.items() if witnesses.isdisjoint(picked))
            if kind in self._families:
                lost[kind] = missing
            kinds[kind] = self._limits(kind, list(items.values()), len(picked), len(missing))
        restcov = kinds.pop("RESTCov items")
        traffic = kinds.pop("traffic items", ItemBudget(0, Count(0), Count(0)))
        joint = kinds.pop("recorded items", restcov)
        return BudgetWarning(len(picked), len(self._pool), kinds, restcov, traffic, joint, lost)

    def _limits(self, kind: str, witnesses: list[set[JavaTest]], size: int, missing: int) -> ItemBudget:
        """Solve the cover and the fixed size problem over the tests we can pick.

        Returns:
            The best coverage and the size needed for every item.
        """
        total = len(witnesses)
        indices = {test: i for i, test in enumerate(self._pool)}
        witnesses = [tests for tests in witnesses if not tests.isdisjoint(indices)]
        reachable = len(witnesses)
        if not reachable:
            return ItemBudget(total, Count(0), Count(0), total)
        rows, columns = [], []
        reaches: list[list[int]] = [[] for _ in self._pool]
        for row, tests in enumerate(witnesses):
            for test in tests.intersection(indices):
                column = indices[test]
                rows.append(row)
                columns.append(column)
                reaches[column].append(row)
        minimum = self._minima.get(kind)
        if minimum is None:
            reach = csr_array((np.ones(len(rows)), (rows, columns)), shape=(reachable, len(indices)))
            solved = SmallestCover.minimum(
                np.ones(len(indices)), [LinearConstraint(reach, 1, np.inf)], {"time_limit": 30.0, "mip_rel_gap": 0.0}
            )
            minimum = Count.from_solve(solved, len(indices))
            self._minima[kind] = minimum
        upper = None
        kept = total - missing
        if kept == reachable or minimum.value <= size:
            most = Count(reachable)
        else:
            # The selector allows a relative solver gap. An independent solve can certify the exact RESTCov maximum.
            constraints = CoverageSelector.constraints(reaches, reachable, size)
            solved = CoverageSelector.maximum(len(indices), reachable, constraints, seconds=30.0)
            most = Count.from_solve(solved, kept, -1)
            if most.qualifier is Bound.LOWER:
                dual = solved.get("mip_dual_bound")
                upper = reachable if dual is None or not isfinite(dual) else min(reachable, floor(-dual + 1e-6))
                upper = max(upper, most.value)
        return ItemBudget(total, most, minimum, total - reachable, upper)
