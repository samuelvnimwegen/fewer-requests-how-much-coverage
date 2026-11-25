import json
import statistics
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lesstests.code_coverage import RecordingCode
from lesstests.coverage import CoverageSet, Criterion, RestCov, SuiteCoverage
from lesstests.jacoco import CodeKept
from lesstests.java import JavaTest, Outcome
from lesstests.recording import Recording
from lesstests.selection import Budget, CoverageMatrix, CoverageSelector, RandomSelector, Selector


@dataclass(frozen=True, slots=True)
class SmallerSuite:
    """One smaller version of a suite and what it lost against the full suite."""

    version: str
    budget: Budget
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
        budgets: list[Budget],
        draws: int,
        seed: int,
        passing_only: bool = False,
        code: RecordingCode | None = None,
    ) -> None:
        """Set up the study. With `passing_only`, both selectors pick only from tests that passed alone.

        With `code`, every smaller suite also gets the server code it keeps, and Selected the methods it lost.

        Raises:
            ValueError: If two budgets share a label, since they would write to the same folders.
        """
        labels = [budget.label for budget in budgets]
        if len(set(labels)) != len(labels):
            raise ValueError(f"Every budget needs its own label, got {', '.join(labels)}")
        self._recording = recording
        self._coverage = coverage
        self._restcov = restcov
        self._budgets = budgets
        self._draws = draws
        self._seed = seed
        self._passing_only = passing_only
        self._code = code

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
            size = budget.size(len(pool))
            suites.append(self._build(CoverageSelector(), matrix, budget, 0, size, requests, out))
            for draw in range(1, self._draws + 1):
                selector = RandomSelector(RandomSelector.seed_for(self._seed, budget, draw))
                suites.append(self._build(selector, matrix, budget, draw, size, requests, out))
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
        selector: Selector,
        matrix: CoverageMatrix,
        budget: Budget,
        draw: int,
        size: int,
        requests: dict[JavaTest, int],
        out: Path,
    ) -> SmallerSuite:
        tests = selector.select(matrix, size)
        joined = self._coverage.of(tests)
        suite = SmallerSuite(selector.name, budget, draw, tests, joined, sum(requests[test] for test in tests))
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
            f"Random shows the mean over {self._result['draws']} draws, with the lowest and highest draw.",
            "",
            "| Budget | Kept | Version | " + " | ".join(columns) + " |",
            "|---|---:|---|" + "---:|" * len(columns),
            f"| 100% | {self._result['tests']} | Full suite | " + " | ".join(full) + " |",
        ]
        for budget, selected, drawn in self._by_budget():
            label = Budget(budget).label
            lost = [str(selected["lost"][key]) for key in keys] + [str(selected["requests"])]
            lines.append(f"| {label} | {selected['kept']} | Selected | " + " | ".join(lost) + " |")
            if drawn:
                spread = [self._spread([version["lost"][key] for version in drawn]) for key in keys]
                spread.append(self._spread([version["requests"] for version in drawn]))
                lines.append(f"| {label} | {selected['kept']} | Random | " + " | ".join(spread) + " |")
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
        yardstick = any("code_ceiling" in version for version in versions)
        extra = " Ceiling lines | Gap closed | Solver |" if yardstick else ""
        blanks = "  |  |  |" if yardstick else ""
        lines = [
            "## Server code kept per budget",
            "",
            "Counted by JaCoCo on the server, without the code the server runs on its own when no test runs. The full "
            "suite row counts what all tests cover. Every other row gives the share of that the version keeps. "
            "Classes lost counts the classes where Selected reaches none of the methods the full suite reached, and "
            "`code-lost.md` names them and every lost method.",
            "",
            "| Budget | Kept | Version | Lines | Branches | Methods | Classes lost | Methods lost |" + extra,
            "|---|---:|---|---:|---:|---:|---:|---:|" + ("---:|---:|---|" if yardstick else ""),
            f"| 100% | {self._result['tests']} | Full suite | " + " | ".join(full) + " |  |  |" + blanks,
        ]
        for budget, selected, drawn in self._by_budget():
            label = Budget(budget).label
            code = CodeKept.from_dict(selected["code"])
            kept = [f"{code.share(kind):.1%}" for kind in CodeKept.KINDS]
            kept += [str(len(code.lost_classes)), str(len(code.lost_methods))]
            if yardstick:
                kept += self._ceiling_cells(selected)
            lines.append(f"| {label} | {selected['kept']} | Selected | " + " | ".join(kept) + " |")
            if drawn:
                shares = [
                    [CodeKept.from_dict(version["code"]).share(kind) for version in drawn] for kind in CodeKept.KINDS
                ]
                spread = [self._share_spread(values) for values in shares]
                lines.append(f"| {label} | {selected['kept']} | Random | " + " | ".join(spread) + " |  |  |" + blanks)
        if yardstick:
            lines += [
                "",
                "The ceiling picks on server lines. It is a white box yardstick and never part of the method. "
                "Gap closed is `(Selected - Random) / (Ceiling - Random)`, using line counts and the Random mean. "
                "At a time limit the range uses the best suite found and the whole line upper bound. "
                "With no draws or no gap it is n/a. "
                "The solver bounds, gaps, times and minimum covers are in selection.json.",
            ]
        if plan := self._result.get("plan_select"):
            mean = plan["random_mean"]
            cells = [str(len(plan["tests"])), str(plan["selected_lines"]), "n/a" if mean is None else f"{mean:.1f}"]
            lines += [
                "",
                "## Select from the saved plan",
                "",
                "| Tests | Selected lines | Random mean | Ceiling lines | Gap closed | Solver |",
                "|---:|---:|---:|---:|---:|---|",
                "| " + " | ".join(cells + self._ceiling_cells(plan)) + " |",
            ]
        if covers := self._result.get("minimum_code_covers"):
            lines += [
                "",
                "## Fewest tests for each line target",
                "",
                "| Target | Tests | Lines | Solver | Lower bound in tests |",
                "|---:|---:|---:|---|---:|",
            ]
            for share, cover in covers.items():
                lines.append(
                    f"| {100 * float(share):g}% | {len(cover['tests'])} | {cover['lines']} | "
                    f"{cover['status']} | {cover['bound']} |"
                )
        return lines

    @staticmethod
    def _ceiling_cells(row: dict[str, Any]) -> list[str]:
        """Format the feasible line count and the proof available.

        Returns:
            The ceiling, gap closed and solver cells.
        """
        ceiling = row["code_ceiling"]
        gap = row["gap_closed"]
        text = "n/a" if gap is None else f"{gap:.1%}"
        status = ceiling["status"]
        if status.startswith("time limit") or status == "feasible fallback":
            status += f" (upper bound {ceiling['bound']:g}, gap {ceiling['gap']:.1%})"
            endpoint = row["gap_closed_at_bound"]
            if endpoint is not None and gap is not None:
                text = f"{min(endpoint, gap):.1%} to {max(endpoint, gap):.1%}"
            elif endpoint is not None:
                text = f"n/a at incumbent, {endpoint:.1%} at bound"
        return [str(ceiling["lines"]), text, status]

    def code_lost(self) -> str:
        """List the classes and methods each Selected version lost against the full suite.

        Returns:
            The list as Markdown.
        """
        lines = ["# Server code Selected lost", ""]
        for budget, selected, _ in self._by_budget():
            code = CodeKept.from_dict(selected["code"])
            lines += [
                f"## {Budget(budget).label}, {selected['kept']} tests",
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

    def _by_budget(self) -> list[tuple[float, dict[str, Any], list[dict[str, Any]]]]:
        versions = self._result["versions"]
        found = []
        for budget in sorted({version["budget"] for version in versions}, reverse=True):
            mine = [version for version in versions if version["budget"] == budget]
            selected = next(version for version in mine if version["version"] == "selected")
            found.append((budget, selected, [version for version in mine if version["version"] == "random"]))
        return found

    @staticmethod
    def _share_spread(values: list[float]) -> str:
        return f"{statistics.mean(values):.1%} ({min(values):.1%}, {max(values):.1%})"

    @staticmethod
    def _spread(values: list[int]) -> str:
        return f"{statistics.mean(values):.1f} ({min(values)}, {max(values)})"
