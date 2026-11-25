"""Describe what a fixed test budget can cover."""

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from scipy.optimize import LinearConstraint, OptimizeResult
from scipy.sparse import csr_array

from lesstests.coverage import Criterion, SuiteCoverage
from lesstests.java import JavaTest
from lesstests.selection import CoverageSelector, SmallestCover
from lesstests.traffic import Item, TrafficCoverage


@dataclass(slots=True)
class Count:
    """An exact optimum or a bound after the time limit."""

    value: int
    qualifier: str = ""

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
        return cls(value, "at most" if sign == 1 else "at least")

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
    minimum: Count | None


@dataclass(slots=True)
class BudgetWarning:
    """The limits of a budget and the items missing from its pick."""

    size: int
    pool: int
    kinds: dict[str, ItemBudget]
    lost: dict[str, list[Item]]

    def message(self) -> str:
        """Describe forced losses separately from the pick.

        Returns:
            The plain language summary.
        """
        lines = [f"You asked to keep {self.size} of {self.pool} tests."]
        joint = self.kinds.get("recorded items", self.kinds["RESTCov items"])
        if joint.most.value == joint.total and not joint.most.qualifier:
            lines.append("That size can keep every recorded item.")
        else:
            for kind, row in self.kinds.items():
                if row.most.qualifier:
                    lines.append(f"A pick can keep {row.most} of {row.total} {kind}.")
                elif row.most.value < row.total:
                    lines.append(
                        f"At that size at least {row.total - row.most.value} of your {row.total} {kind} disappear."
                    )
            for kind, row in self.kinds.items():
                if row.minimum is None:
                    lines.append(f"The eligible tests cannot keep all {kind}.")
                elif row.most.value < row.total or kind == "traffic items":
                    lines.append(f"Keeping all {kind} takes {row.minimum} tests.")
        if any(row.most.qualifier or (row.minimum and row.minimum.qualifier) for row in self.kinds.values()):
            lines.append("The solver reached its time limit. Counts marked at least or at most are bounds.")
        return " ".join(lines)

    def report(self) -> str:
        """Show all counts and a short sample of the actual losses.

        Returns:
            The Markdown shared by the command and saved report.
        """
        lines = [self.message(), "", "| Kind | Items | Most kept | Tests for all |", "|---|---:|---:|---:|"]
        for kind, row in self.kinds.items():
            lines.append(f"| {kind} | {row.total} | {row.most} | {row.minimum if row.minimum else 'impossible'} |")
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
        return {**asdict(self), "report": self.report(), "message": self.message()}


class BudgetAnalysis:
    """Find independent optima from the tests that witness each recorded item."""

    def __init__(self, coverage: SuiteCoverage, pool: list[JavaTest], traffic: TrafficCoverage | None) -> None:
        """Index RESTCov and enabled traffic items by family and together."""
        self.pool = sorted(pool)
        names = {
            Criterion.PATH: "paths",
            Criterion.OPERATION: "operations",
            Criterion.PARAMETER: "parameters",
            Criterion.REQUEST_TYPE: "request types",
            Criterion.RESPONSE_TYPE: "response types",
            Criterion.STATUS: "operation and status pairs",
            Criterion.STATUS_CLASS: "status classes",
        }
        self.families: dict[str, dict[Item, set[JavaTest]]] = {kind: defaultdict(set) for kind in names.values()}
        for test, reached in coverage.per_test.items():
            for item in reached.items:
                self.families[names[item.criterion]][(item.key,)].add(test)
        self.minima: dict[str, Count | None] = {}
        self.groups = dict(self.families)
        self.groups["RESTCov items"] = self._combined(self.families)
        if traffic and traffic.witnesses:
            self.families.update(traffic.witnesses)
            self.groups.update(traffic.witnesses)
            self.groups["traffic items"] = self._combined(traffic.witnesses)
            self.groups["recorded items"] = self._combined(self.families)

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
        for kind, items in self.groups.items():
            missing = sorted(item for item, witnesses in items.items() if witnesses.isdisjoint(picked))
            if kind in self.families:
                lost[kind] = missing
            kinds[kind] = self._limits(kind, list(items.values()), len(picked), len(missing))
        return BudgetWarning(len(picked), len(self.pool), kinds, lost)

    def _limits(self, kind: str, witnesses: list[set[JavaTest]], size: int, missing: int) -> ItemBudget:
        """Solve the cover and the fixed size problem over eligible tests.

        Returns:
            The best coverage and the size needed for every item.
        """
        total = len(witnesses)
        if not total:
            return ItemBudget(0, Count(0), Count(0))
        indices = {test: i for i, test in enumerate(self.pool)}
        rows, columns = [], []
        reaches: list[list[int]] = [[] for _ in self.pool]
        for row, tests in enumerate(witnesses):
            for test in tests.intersection(indices):
                column = indices[test]
                rows.append(row)
                columns.append(column)
                reaches[column].append(row)
        minimum = self.minima.get(kind)
        if kind not in self.minima and len(set(rows)) == total:
            reach = csr_array((np.ones(len(rows)), (rows, columns)), shape=(total, len(indices)))
            solved = SmallestCover.minimum(
                np.ones(len(indices)), [LinearConstraint(reach, 1, np.inf)], {"time_limit": 30.0, "mip_rel_gap": 0.0}
            )
            minimum = Count.from_solve(solved, len(indices))
            self.minima[kind] = minimum
        if not missing or (minimum and minimum.value <= size):
            most = Count(total)
        elif not self.pool:
            most = Count(0)
        else:
            constraints = CoverageSelector.constraints(reaches, total, size)
            solved = CoverageSelector.maximum(len(indices), total, constraints, seconds=30.0)
            most = Count.from_solve(solved, total - missing, -1)
        return ItemBudget(total, most, minimum)
