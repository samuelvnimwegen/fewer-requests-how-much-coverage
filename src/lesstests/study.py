import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lesstests.baselines import DEFAULT_BASELINES, Baseline
from lesstests.code_coverage import RecordingCode
from lesstests.coverage import CoverageSet, Criterion, RestCov, SuiteCoverage
from lesstests.jacoco import CodeKept
from lesstests.java import JavaTest, Outcome
from lesstests.recording import Recording
from lesstests.selection import Budget, CoverageMatrix, CoverageSelector, CoverBudget, RandomSelector


@dataclass(frozen=True, slots=True)
class SmallerSuite:
    """One smaller version of a suite and what it lost against the full suite."""

    version: str
    budget: Budget | CoverBudget
    draw: int
    tests: list[JavaTest]
    coverage: CoverageSet
    requests: int
    code: CodeKept | None = None

    @property
    def name(self) -> str:
        """Name the version for its folders, like `selected-75-0` or `random-50-12`."""
        return f"{self.version}-{self.budget.label.rstrip('%')}-{self.draw}"

    def lost(self, full: CoverageSet) -> dict[str, int]:
        """Count the items the full suite reaches and this version misses, per criterion and in total.

        Returns:
            The lost items per criterion, plus `all`.
        """
        missing = full - self.coverage
        return {**missing.counts(), "all": len(missing)}

    def to_dict(self, full: CoverageSet) -> dict[str, Any]:
        """Turn the version into JSON friendly data.

        Returns:
            The version as a dict.
        """
        return {
            "version": self.version,
            "budget": self.budget.share,
            "draw": self.draw,
            "kept": len(self.tests),
            "requests": self.requests,
            "covered": {**self.coverage.counts(), "all": len(self.coverage)},
            "lost": self.lost(full),
            **({"code": self.code.to_dict()} if self.code else {}),
            "tests": [test.id for test in self.tests],
        }


class SelectionStudy:
    """The RQ2 comparison without new runs. Random and Selected only drop tests, so the recording is enough.

    Every smaller suite is measured twice: by joining the items of its tests, and by RESTCov on its joined logs.
    The study stops if the two ever differ.
    """

    def __init__(
        self,
        recording: Recording,
        coverage: SuiteCoverage,
        restcov: RestCov,
        budgets: Sequence[Budget | CoverBudget],
        draws: int,
        seed: int,
        passing_only: bool = False,
        code: RecordingCode | None = None,
        baselines: Sequence[Baseline] = DEFAULT_BASELINES,
    ) -> None:
        """Set up the study. With `passing_only`, every suite uses tests that passed alone.

        With `code`, every smaller suite also gets the server code it keeps, and Selected the methods it lost.

        Raises:
            ValueError: If budgets share a label, baselines share a name or a baseline uses the reserved name selected.
        """
        labels = [budget.label for budget in budgets]
        if len(set(labels)) != len(labels):
            raise ValueError(f"Every budget needs its own label, got {', '.join(labels)}")
        names = [baseline.name for baseline in baselines]
        if "selected" in names or len(set(names)) != len(names):
            raise ValueError("Baseline names must be unique and cannot be selected")
        self._recording = recording
        self._coverage = coverage
        self._restcov = restcov
        self._budgets = budgets
        self._draws = draws
        self._seed = seed
        self._passing_only = passing_only
        self._code = code
        self._baselines = tuple(baselines)

    def run(self, out: Path) -> list[SmallerSuite]:
        """Build every smaller suite, measure it and write the results to `out`.

        Returns:
            The smaller suites.
        """
        records = self._recording.records()
        requests = {record.test: record.requests for record in records}
        pool = [record.test for record in records if record.outcome is Outcome.PASSED or not self._passing_only]
        matrix = CoverageMatrix(self._coverage, requests, pool)
        suites: list[SmallerSuite] = []
        for budget in self._budgets:
            selected = budget.pick(matrix, self._coverage, requests)
            suites.append(self._build("selected", selected, budget, 0, requests, out))
            for draw in range(1, self._draws + 1):
                seed = RandomSelector.seed_for(self._seed, budget, draw)
                for baseline in self._baselines:
                    tests = baseline.draw(matrix, selected, seed)
                    suites.append(self._build(baseline.name, tests, budget, draw, requests, out))
        if self._code is not None:
            suites = self._with_code(self._code, [record.test for record in records], suites)
        self._write(out, len(records), sum(requests.values()), len(pool), suites)
        return suites

    @staticmethod
    def _with_code(code: RecordingCode, full: list[JavaTest], suites: list[SmallerSuite]) -> list[SmallerSuite]:
        """Count the server code of every smaller suite in one go, and list what Selected lost.

        Returns:
            The suites with their code.
        """
        listed = frozenset(suite.name for suite in suites if suite.version == CoverageSelector().name)
        kept = code.kept({suite.name: suite.tests for suite in suites}, full, listed)
        return [replace(suite, code=kept[suite.name]) for suite in suites]

    def _build(
        self,
        name: str,
        tests: list[JavaTest],
        budget: Budget | CoverBudget,
        draw: int,
        requests: dict[JavaTest, int],
        out: Path,
    ) -> SmallerSuite:
        joined = self._coverage.of(tests)
        suite = SmallerSuite(name, budget, draw, tests, joined, sum(requests[test] for test in tests))
        logs = self._recording.gather_logs(tests, out / "logs" / suite.name)
        measured = self._restcov.measure(logs, out / "restcov" / suite.name).items()
        if measured != joined:
            raise ValueError(f"RESTCov and the joined items differ for {suite.name}")
        return suite

    def _write(self, out: Path, records_count: int, requests: int, pool: int, suites: list[SmallerSuite]) -> None:
        full = self._coverage.full
        result = {
            "tests": records_count,
            "requests": requests,
            "pool": pool,
            "passing_only": self._passing_only,
            "seed": self._seed,
            "draws": self._draws,
            "totals": self._coverage.totals,
            "full": {**full.counts(), "all": len(full)},
            "versions": [suite.to_dict(full) for suite in suites],
        }
        out.mkdir(parents=True, exist_ok=True)
        (out / "selection.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        report = StudyReport(result)
        (out / "report.md").write_text(report.markdown(), encoding="utf-8")
        lost = out / "code-lost.md"
        lost.unlink(missing_ok=True)
        if report.has_code:
            lost.write_text(report.code_lost(), encoding="utf-8")


class StudyReport:
    """Turns `selection.json` into a short Markdown table of coverage lost per budget."""

    _LABELS = {
        Criterion.PATH: "Paths",
        Criterion.OPERATION: "Operations",
        Criterion.PARAMETER: "Parameters",
        Criterion.REQUEST_TYPE: "Request types",
        Criterion.RESPONSE_TYPE: "Response types",
        Criterion.STATUS: "Status codes",
        Criterion.STATUS_CLASS: "Status classes",
    }

    def __init__(self, result: dict[str, Any]) -> None:
        """Keep the parsed results."""
        self._result = result

    def markdown(self) -> str:
        """Write the report.

        Returns:
            The report as Markdown.
        """
        columns = [*self._LABELS.values(), "All", "Requests"]
        keys = [*(criterion.value for criterion in self._LABELS), "all"]
        full = [str(self._result["full"][key]) for key in keys] + [str(self._result["requests"])]
        lines = [
            "# Coverage lost per budget",
            "",
            f"{self._result['tests']} tests, of which {self._result['pool']} could be picked. "
            "The full suite row counts the items the suite covers. Every other row counts the items that version "
            "loses against the full suite. Requests counts what the kept tests send. "
            f"Random shows the mean over {self._result['draws']} draws, with the lowest and highest draw. "
            "Length matched random keeps the same number of tests at each exact request count as Selected.",
            "",
            "| Budget | Kept | Version | " + " | ".join(columns) + " |",
            "|---|---:|---|" + "---:|" * len(columns),
            f"| 100% | {self._result['tests']} | Full suite | " + " | ".join(full) + " |",
        ]
        for label, selected, baselines in self._by_budget():
            lost = [str(selected["lost"][key]) for key in keys] + [str(selected["requests"])]
            lines.append(f"| {label} | {selected['kept']} | Selected | " + " | ".join(lost) + " |")
            for baseline, drawn in baselines.items():
                name = self._baseline_label(baseline)
                spread = [self._spread([version["lost"][key] for version in drawn]) for key in keys]
                spread.append(self._spread([version["requests"] for version in drawn]))
                lines.append(f"| {label} | {selected['kept']} | {name} | " + " | ".join(spread) + " |")
        if self.has_code:
            lines += ["", *self._code_table()]
        return "\n".join(lines) + "\n"

    @property
    def has_code(self) -> bool:
        """Whether the versions carry server code coverage."""
        return any("code" in version for version in self._result["versions"])

    def _code_table(self) -> list[str]:
        """Write the server code each version keeps of the full suite.

        Returns:
            The table lines.
        """
        versions = self._result["versions"]
        reference = CodeKept.from_dict(versions[0]["code"]).reference
        full = [str(reference.of(kind)) for kind in CodeKept.KINDS]
        lines = [
            "## Server code kept per budget",
            "",
            "Counted by JaCoCo on the server, without the code the server runs on its own when no test runs. The full "
            "suite row counts what all tests cover. Every other row gives the share of that the version keeps. "
            "Classes lost counts the classes where Selected reaches none of the methods the full suite reached, and "
            "`code-lost.md` names them and every lost method. Rank counts Selected and all draws. "
            "Rank 1 keeps the most code. Ties share the better rank.",
            "",
            "| Budget | Kept | Version | Lines | Branches | Methods | Classes lost | Methods lost |",
            "|---|---:|---|---:|---:|---:|---:|---:|",
            f"| 100% | {self._result['tests']} | Full suite | " + " | ".join(full) + " |  |  |",
        ]
        for label, selected, baselines in self._by_budget():
            code = CodeKept.from_dict(selected["code"])
            kept = [f"{code.share(kind):.1%}" for kind in CodeKept.KINDS]
            kept += [str(len(code.lost_classes)), str(len(code.lost_methods))]
            lines.append(f"| {label} | {selected['kept']} | Selected | " + " | ".join(kept) + " |")
            for baseline, drawn in baselines.items():
                name = self._baseline_label(baseline)
                codes = [CodeKept.from_dict(version["code"]) for version in drawn]
                spread = [
                    self._share_spread([draw.share(kind) for draw in codes], code.share(kind))
                    for kind in CodeKept.KINDS
                ]
                lines.append(f"| {label} | {selected['kept']} | {name} | " + " | ".join(spread) + " |  |  |")
        return lines

    def code_lost(self) -> str:
        """List the classes and methods each Selected version lost against the full suite.

        Returns:
            The list as Markdown.
        """
        lines = ["# Server code Selected lost", ""]
        for label, selected, _ in self._by_budget():
            code = CodeKept.from_dict(selected["code"])
            lines += [
                f"## {label}, {selected['kept']} tests",
                "",
                f"Classes that lost every method the full suite reached: {len(code.lost_classes)}. "
                f"Methods no longer reached: {len(code.lost_methods)}.",
                "",
                "### Classes",
                "",
                *(f"* `{name}`" for name in code.lost_classes),
                "",
                "### Methods",
                "",
                *(f"* `{name}`" for name in code.lost_methods),
                "",
            ]
        return "\n".join(lines)

    def _by_budget(self) -> list[tuple[str, dict[str, Any], dict[str, list[dict[str, Any]]]]]:
        """Group each Selected suite with the comparison draws at its budget.

        Returns:
            Budget labels and versions in order of decreasing suite size.
        """
        versions = self._result["versions"]
        found = []
        for selected in sorted(
            (v for v in versions if v["version"] == "selected"),
            key=lambda v: (v["kept"], 0 if v["budget"] == "cover" else v["budget"]),
            reverse=True,
        ):
            budget = selected["budget"]
            label = "cover" if budget == "cover" else Budget(budget).label
            baselines: dict[str, list[dict[str, Any]]] = {}
            for version in versions:
                if version["budget"] == budget and version["version"] != "selected":
                    baselines.setdefault(version["version"], []).append(version)
            found.append((label, selected, baselines))
        return found

    @staticmethod
    def _baseline_label(name: str) -> str:
        """Keep custom names recognisable in the report.

        Returns:
            The display name of the baseline.
        """
        return {"random": "Random", "length-matched-random": "Length matched random"}.get(name, name)

    @staticmethod
    def _share_spread(values: list[float], selected: float) -> str:
        """Compare the unrounded shares so close draws keep their true rank.

        Returns:
            The mean, extremes and rank of Selected within this baseline.
        """
        rank = 1 + sum(value > selected for value in values)
        return f"{statistics.mean(values):.1%} ({min(values):.1%}, {max(values):.1%}), rank {rank}/{len(values) + 1}"

    @staticmethod
    def _spread(values: list[int]) -> str:
        return f"{statistics.mean(values):.1f} ({min(values)}, {max(values)})"
