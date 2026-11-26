"""The versions of a suite the benchmark compares: the original, Select only, and Select plus Merge."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from lesstests.api import ApiDescription, ValueItem
from lesstests.coverage import CoverageSet, SuiteCoverage
from lesstests.jacoco import CodeKept
from lesstests.java import JavaTest, Outcome
from lesstests.recording import RecordedTest
from lesstests.selection import CoverOptions, SmallestCover
from lesstests.traffic import TrafficCoverage


@dataclass(slots=True)
class SuiteVersion:
    """One version of the suite with the coverage, values and requests its recorded tests predict.

    `code` is the server code it keeps of the Original, for a recording with code coverage.
    """

    name: str
    tests: list[JavaTest]
    coverage: CoverageSet
    values: frozenset[ValueItem]
    requests: int
    code: CodeKept | None = None
    traffic: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Turn the version into JSON friendly data.

        Returns:
            The version as a dict.
        """
        return {
            "name": self.name,
            "tests": [test.id for test in self.tests],
            "coverage": self.coverage.to_list(),
            "values": [value.to_list() for value in sorted(self.values)],
            "requests": self.requests,
            **({"code": self.code.to_dict()} if self.code else {}),
            **({"traffic": self.traffic} if self.traffic is not None else {}),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read a version saved by `to_dict`.

        Returns:
            The version.
        """
        return cls(
            raw["name"],
            [JavaTest.from_id(test) for test in raw["tests"]],
            CoverageSet.from_list(raw["coverage"]),
            frozenset(ValueItem.from_list(value) for value in raw["values"]),
            raw["requests"],
            CodeKept.from_dict(raw["code"]) if "code" in raw else None,
            raw.get("traffic"),
        )


class VersionPlan:
    """The versions to benchmark, saved as `plan.json` next to a short report of what the recordings predict."""

    ORIGINAL = "Original"
    SELECT = "Select"
    SELECT_MERGE = "Select + Merge"

    def __init__(self, versions: list[SuiteVersion], options: CoverOptions | None = None) -> None:
        """Keep the versions, the original first."""
        self.versions = versions
        self.options = options

    @property
    def original(self) -> SuiteVersion:
        """The full suite."""
        return self.versions[0]

    def save(self, path: Path) -> None:
        """Write the plan as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = {"versions": [version.to_dict() for version in self.versions], **self.settings()}
        path.write_text(json.dumps(raw, indent=1), encoding="utf-8")

    def settings(self) -> dict[str, Any]:
        """Describe the selection rule for a saved result.

        Returns:
            The minimum cover settings when the user chose that mode.
        """
        if self.options is None:
            return {}
        return {
            "selection": {"objective": "tests", "families": list(self.options.families), "share": self.options.share}
        }

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read a plan written by `save`.

        Returns:
            The plan.
        """
        raw = json.loads(path.read_text(encoding="utf-8"))
        settings = raw.get("selection")
        options = CoverOptions(tuple(settings["families"]), settings["share"]) if settings else None
        return cls([SuiteVersion.from_dict(version) for version in raw["versions"]], options)

    def markdown(self) -> str:
        """Write what the recordings predict for each version, before any benchmark run.

        Returns:
            The report as Markdown.
        """
        full, values = self.original.coverage, self.original.values
        code = [kind.capitalize() + " kept" for kind in CodeKept.KINDS] if self.original.code else []
        lines = [
            "# Predicted from the recordings",
            "",
            "Each test ran alone. The benchmark measures every version again as a whole suite.",
            "",
            "| " + " | ".join(["Version", "Tests", "Requests", "RESTCov items kept", "Value items kept", *code]) + " |",
            "|---|" + "---:|" * (4 + len(code)),
        ]
        for version in self.versions:
            kept = len(version.coverage.items & full.items)
            kept_values = len(version.values & values)
            cells = [version.name, str(len(version.tests)), str(version.requests), f"{kept} of {len(full)}"]
            cells.append(f"{kept_values} of {len(values)}")
            if version.code is not None:
                cells += [CodeShare.cell(version.code, kind) for kind in CodeKept.KINDS]
            lines.append("| " + " | ".join(cells) + " |")
        if self.options is not None:
            lines += ["", "Select keeps the fewest tests, then the fewest requests."]
            if self.options.families:
                lines += [
                    "",
                    f"Select must keep at least {self.options.share * 100:g}% of the traffic items.",
                    "",
                    "| Family | Full | Kept |",
                    "|---|---:|---:|",
                ]
                for family in self.options.families:
                    full_count = (self.original.traffic or {})[family]
                    kept_count = (self.versions[1].traffic or {})[family]
                    lines.append(f"| {family} | {full_count} | {kept_count} |")
        return "\n".join(lines) + "\n"


class CodeShare:
    """Writes kept server code as a table cell."""

    @staticmethod
    def cell(kept: CodeKept, kind: str) -> str:
        """Write how many lines, branches or methods a version kept, like `120 of 150 (80.0%)`.

        Returns:
            The cell text.
        """
        return f"{kept.kept.of(kind)} of {kept.reference.of(kind)} ({kept.share(kind):.1%})"


class VersionPlanner:
    """Builds the versions with the smallest cover program.

    Select keeps the fewest original tests that reach every item of the full suite. Select plus Merge may also pick
    merged copies, which need fewer tests when one copy reaches the names of several originals.
    """

    def __init__(
        self,
        api: ApiDescription,
        original: tuple[SuiteCoverage, list[RecordedTest]],
    ) -> None:
        """Keep the coverage and records of the original suite."""
        self._api = api
        self._original_coverage, self._original_records = original

    def plan(self, options: CoverOptions | None = None, passing_only: bool = False) -> VersionPlan:
        """Pick the tests of Original and Select, for a suite without a Merge step.

        Returns:
            The plan with Original and Select.
        """
        coverage = dict(self._original_coverage.per_test)
        records = {record.test: record for record in self._original_records}
        candidates = {
            test: items
            for test, items in coverage.items()
            if not passing_only or records[test].outcome is Outcome.PASSED
        }
        traffic = None
        if options is not None:
            traffic = TrafficCoverage.collect(
                self._original_records, self._api, self._original_coverage.full, options.families
            )
        select = SmallestCover().select(
            candidates, self._requests(records), self._original_coverage.full, traffic, options.share if options else 1
        )
        original = self._version(VersionPlan.ORIGINAL, sorted(records), coverage, records)
        selected = self._version(VersionPlan.SELECT, select, coverage, records)
        if traffic is not None:
            original.traffic = traffic.counts(original.tests)
            selected.traffic = traffic.counts(select)
        return VersionPlan([original, selected], options)

    def plan_with_merge(self, merged: tuple[SuiteCoverage, list[RecordedTest]] | None) -> VersionPlan:
        """Add Select + Merge, which may also pick the kept merged copies. With none kept it equals Select.

        Returns:
            The plan with Original, Select and Select + Merge.
        """
        coverage = dict(self._original_coverage.per_test)
        records = {record.test: record for record in self._original_records}
        if merged is not None:
            merged_coverage, merged_records = merged
            coverage |= merged_coverage.per_test
            records |= {record.test: record for record in merged_records}
        select_merge = SmallestCover().select(coverage, self._requests(records), self._original_coverage.full)
        plan = self.plan()
        return VersionPlan([*plan.versions, self._version(VersionPlan.SELECT_MERGE, select_merge, coverage, records)])

    @staticmethod
    def _requests(records: dict[JavaTest, RecordedTest]) -> dict[JavaTest, int]:
        return {test: record.requests for test, record in records.items()}

    def _version(
        self,
        name: str,
        tests: list[JavaTest],
        coverage: dict[JavaTest, CoverageSet],
        records: dict[JavaTest, RecordedTest],
    ) -> SuiteVersion:
        exchanges = [exchange for test in tests for exchange in records[test].exchanges]
        return SuiteVersion(
            name,
            tests,
            CoverageSet.union(coverage[test] for test in tests),
            self._api.values(exchanges),
            sum(records[test].requests for test in tests),
        )
