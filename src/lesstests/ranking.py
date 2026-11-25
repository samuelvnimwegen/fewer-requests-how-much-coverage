"""Compare full covers of recorded item families."""

import json
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from lesstests.api import ApiDescription
from lesstests.code_coverage import RecordingCode
from lesstests.coverage import CoverageSet, Criterion, SuiteCoverage
from lesstests.jacoco import CodeCounts
from lesstests.java import JavaTest
from lesstests.recording import RecordedTest
from lesstests.selection import SmallestCover
from lesstests.traffic import FAMILIES, Item, TrafficCoverage


class MethodNovelty:
    """Compare first observations of items and methods in the same test order."""

    def __init__(self, methods: dict[JavaTest, set[str]]) -> None:
        """Save the tests that add a method in each of thirty reproducible orders."""
        self.orders: list[list[tuple[JavaTest, bool]]] = []
        for seed in range(30):
            tests = sorted(methods)
            random.Random(seed).shuffle(tests)
            seen: set[str] = set()
            order = []
            for test in tests:
                order.append((test, not methods[test].issubset(seen)))
                seen.update(methods[test])
            self.orders.append(order)

    def count(self, witnesses: dict[Item, set[JavaTest]]) -> tuple[int, int]:
        """Count visits that add an item and also add a method.

        Returns:
            Visits adding both and visits adding an item.
        """
        items: dict[JavaTest, set[int]] = defaultdict(set)
        for number, group in enumerate(witnesses.values()):
            for test in group:
                items[test].add(number)
        both = new = 0
        for order in self.orders:
            seen: set[int] = set()
            for test, adds_method in order:
                if not items[test].issubset(seen):
                    both += adds_method
                    new += 1
                    seen.update(items[test])
        return both, new


@dataclass(slots=True)
class FamilyCover:
    """One full cover and the code reached by its tests."""

    family: str
    with_restcov: bool
    items: int
    singletons: int
    tests: list[str]
    requests: int
    novelty: tuple[int, int] | None
    kept: CodeCounts | None = None
    per_test: dict[str, float] | None = None

    def markdown(self) -> str:
        """Show the cover cost, code counts and novelty.

        Returns:
            One row for the report.
        """
        code = self.kept
        counts = [str(code.of(k)) if code else "n/a" for k in CodeCounts.KINDS]
        ratios = [f"{self.per_test[k]:.2f}" if self.per_test else "n/a" for k in CodeCounts.KINDS]
        novelty = "n/a" if self.novelty is None else f"{self.novelty[0]}/{self.novelty[1]}"
        return (
            f"| {self.family} | {self.items} | {self.singletons} | {len(self.tests)} | {self.requests} | "
            + " | ".join([*counts, *ratios, novelty])
            + " |"
        )


class RankingStudy:
    """Pick tests using only recorded items, then score the selected tests."""

    def __init__(self, records: list[RecordedTest], coverage: SuiteCoverage, api: ApiDescription) -> None:
        """Collect each family across the full recording."""
        self.requests = {r.test: r.requests for r in records}
        self.coverage = coverage
        self.families = TrafficCoverage.collect(records, api, coverage.full, tuple(FAMILIES)).witnesses
        for criterion in Criterion:
            self.families[criterion.value] = defaultdict(set)
        for test, covered in coverage.per_test.items():
            for item in covered.items:
                self.families[item.criterion.value][(item.key,)].add(test)

    def run(self, out: Path, code: RecordingCode | None) -> None:
        """Write the cover of each family alone and alongside every RESTCov level."""
        tests = sorted(self.requests)
        novelty = MethodNovelty(code.methods_by_test(tests)) if code else None
        covers: list[FamilyCover] = []
        picks = {}
        solver = SmallestCover()
        empty = CoverageSet()
        baseline = solver.select(self.coverage.per_test, self.requests, self.coverage.full, TrafficCoverage({}))
        for name, witnesses in self.families.items():
            family = TrafficCoverage({name: witnesses})
            common = novelty.count(witnesses) if novelty else None
            for with_restcov in (False, True):
                if with_restcov and (name in Criterion or not witnesses):
                    selected = baseline
                else:
                    target = self.coverage.full if with_restcov else empty
                    selected = solver.select(self.coverage.per_test, self.requests, target, family)
                picks[f"{name}/{with_restcov}"] = selected
                covers.append(
                    FamilyCover(
                        name,
                        with_restcov,
                        len(witnesses),
                        sum(len(group) == 1 for group in witnesses.values()),
                        [test.id for test in selected],
                        sum(self.requests[test] for test in selected),
                        common,
                    )
                )
        reference = None
        if code:
            measured = code.kept(picks, tests)
            for cover in covers:
                score = measured[f"{cover.family}/{cover.with_restcov}"]
                reference = score.reference
                cover.kept = score.kept
                if cover.tests:
                    cover.per_test = {kind: score.kept.of(kind) / len(cover.tests) for kind in CodeCounts.KINDS}
        self._write(out, covers, reference)

    def _write(self, out: Path, covers: list[FamilyCover], reference: CodeCounts | None) -> None:
        """Write the ranked rows and the identifiers of all selected tests."""
        out.mkdir(parents=True, exist_ok=True)
        covers.sort(key=lambda c: (c.with_restcov, -(c.per_test["lines"] if c.per_test else -1), c.family))
        summary = {
            "full_tests": len(self.requests),
            "full_requests": sum(self.requests.values()),
            "full_code": reference.to_dict() if reference else None,
            "covers": [asdict(cover) for cover in covers],
        }
        (out / "ranking.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        report = [
            "# Coverage item families",
            "",
            f"The recording has {len(self.requests)} tests and {sum(self.requests.values())} requests.",
        ]
        if reference:
            report.append(
                f"The full suite reached {reference.lines} lines, {reference.branches} branches and "
                f"{reference.methods} methods."
            )
        report += [
            "Each cover minimizes tests first and requests second. Rows with code are sorted by lines per kept test.",
            "Singletons count family items seen in just one test. Novelty counts visits that add both a family item",
            "and a method out of visits that add a family item, across 30 orders with seeds 0 through 29.",
            "A short response value has fewer than 64 characters. A field needs 2 to 12 distinct short values",
            "within an operation and status. This can include an ID or date with few observed values.",
            "Singletons and novelty describe the family alone. A denominator of zero means the family has no items.",
        ]
        for combined, title in ((False, "Family alone"), (True, "Family with RESTCov")):
            report += [
                "",
                f"## {title}",
                "",
                "| Family | Items | Singletons | Tests | Requests | Lines | Branches | Methods | Lines/test | "
                "Branches/test | Methods/test | Novelty |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                *(cover.markdown() for cover in covers if cover.with_restcov == combined),
            ]
        (out / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
