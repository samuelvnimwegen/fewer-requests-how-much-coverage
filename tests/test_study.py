from pathlib import Path

import pytest
from fake_restcov import FakeRestCov

from lesstests.coverage import CoverageItem, CoverageSet, Criterion, SuiteCoverage
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.jacoco import CodeCounts, CodeKept
from lesstests.java import JavaTest, Outcome
from lesstests.recording import RecordedTest, Recording
from lesstests.selection import Budget
from lesstests.study import SelectionStudy, SmallerSuite, StudyReport


def _counts(**values: int) -> dict[str, int]:
    return {**{criterion.value: 0 for criterion in Criterion}, "all": 0, **values}


def test_smaller_suite_counts_what_it_loses() -> None:
    full = CoverageSet([CoverageItem(Criterion.PATH, "/a"), CoverageItem(Criterion.STATUS, "GET /a 200")])
    kept = CoverageSet([CoverageItem(Criterion.PATH, "/a")])
    suite = SmallerSuite("selected", Budget(0.5), 0, [JavaTest("s.A", "x")], kept, 3)
    assert suite.lost(full) == _counts(status=1, all=1)
    assert suite.to_dict(full)["kept"] == 1


def test_report_shows_full_selected_and_random_rows() -> None:
    result = {
        "tests": 4,
        "requests": 9,
        "pool": 4,
        "draws": 2,
        "full": _counts(path=2, all=2),
        "versions": [
            {"version": "selected", "budget": 0.5, "kept": 2, "requests": 3, "lost": _counts()},
            {"version": "random", "budget": 0.5, "kept": 2, "requests": 4, "lost": _counts(path=1, all=1)},
            {"version": "random", "budget": 0.5, "kept": 2, "requests": 6, "lost": _counts(path=2, all=2)},
        ],
    }
    lines = StudyReport(result).markdown().splitlines()
    assert "| 100% | 4 | Full suite | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 9 |" in lines
    assert "| 50% | 2 | Selected | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 |" in lines
    assert any(
        line.startswith("| 50% | 2 | Random | 1.5 (1, 2) |") and line.endswith("| 5.0 (4, 6) |") for line in lines
    )


def test_passing_only_sizes_budgets_from_the_pool(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "rec")
    outcomes = {"p1": Outcome.PASSED, "p2": Outcome.PASSED, "f1": Outcome.FAILED, "f2": Outcome.FAILED}
    for name, outcome in outcomes.items():
        request = HttpRequest("GET", f"/v2/{name}", Headers(), Body())
        exchange = Exchange(request, HttpResponse(200, "OK", Headers(), Body()))
        recording.save(RecordedTest(JavaTest("suite.Api", name), outcome, 1.0, [exchange]))
    restcov = FakeRestCov()
    coverage = SuiteCoverage.measure(recording, restcov, tmp_path / "coverage")
    study = SelectionStudy(recording, coverage, restcov, [Budget(0.5)], draws=2, seed=1, passing_only=True)

    # Half of the 2 passing tests is 1. Sizing from all 4 records would keep the whole pool.
    suites = study.run(tmp_path / "selection")
    assert [len(suite.tests) for suite in suites] == [1, 1, 1]
    assert all(test.method.startswith("p") for suite in suites for test in suite.tests)


def test_budgets_that_share_a_label_are_refused(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "rec")
    coverage = SuiteCoverage({}, CoverageSet(), {})
    with pytest.raises(ValueError, match="label"):
        SelectionStudy(recording, coverage, FakeRestCov(), [Budget(0.5), Budget(0.5)], draws=1, seed=1)


def _code(lines: int, lost: tuple[str, ...] = (), classes: tuple[str, ...] = ()) -> dict[str, object]:
    return CodeKept(CodeCounts(lines, 2, 1), CodeCounts(100, 4, 2), lost, classes).to_dict()


def test_report_adds_the_server_code_each_version_keeps_and_lists_what_selected_lost() -> None:
    result = {
        "tests": 4,
        "requests": 9,
        "pool": 4,
        "draws": 2,
        "full": _counts(path=2, all=2),
        "versions": [
            {
                "version": "selected",
                "budget": 0.5,
                "kept": 2,
                "requests": 3,
                "lost": _counts(),
                "code": _code(90, ("a.B.x()V",), ("a.B",)),
            },
            {"version": "random", "budget": 0.5, "kept": 2, "requests": 4, "lost": _counts(), "code": _code(60)},
            {"version": "random", "budget": 0.5, "kept": 2, "requests": 6, "lost": _counts(), "code": _code(62)},
            {"version": "random", "budget": 0.5, "kept": 2, "requests": 5, "lost": _counts(), "code": _code(100)},
        ],
    }
    report = StudyReport(result)
    lines = report.markdown().splitlines()
    assert "| 100% | 4 | Full suite | 100 | 4 | 2 |  |  |" in lines
    assert "| 50% | 2 | Selected | 90.0% | 50.0% | 50.0% | 1 | 1 |" in lines
    assert "| 50% | 2 | Random | 74.0% (60.0%, 100.0%) | 50.0% (50.0%, 50.0%) | 50.0% (50.0%, 50.0%) |  |  |" in lines
    lost = report.code_lost()
    assert "Classes that lost every method the full suite reached: 1. Methods no longer reached: 1." in lost
    assert "### Classes\n\n* `a.B`\n\n### Methods\n\n* `a.B.x()V`\n" in lost


def test_a_report_without_code_coverage_has_no_code_table() -> None:
    result = {
        "tests": 1,
        "requests": 1,
        "pool": 1,
        "draws": 0,
        "full": _counts(),
        "versions": [{"version": "selected", "budget": 0.5, "kept": 1, "requests": 1, "lost": _counts()}],
    }
    report = StudyReport(result)
    assert not report.has_code
    assert "Server code" not in report.markdown()


class FakeRecordingCode:
    def __init__(self) -> None:
        self.asked: tuple[dict[str, list[JavaTest]], list[JavaTest], frozenset[str]] | None = None

    def kept(
        self, versions: dict[str, list[JavaTest]], reference: list[JavaTest], listed: frozenset[str] = frozenset()
    ) -> dict[str, CodeKept]:
        self.asked = (versions, reference, listed)
        return {
            name: CodeKept(CodeCounts(len(tests), 0, 0), CodeCounts(len(reference), 0, 0))
            for name, tests in versions.items()
        }


def test_the_study_counts_every_smaller_suite_in_one_go_and_lists_only_selected(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "rec")
    for name in ("a", "b", "c", "d"):
        request = HttpRequest("GET", f"/v2/{name}", Headers(), Body())
        exchange = Exchange(request, HttpResponse(200, "OK", Headers(), Body()))
        recording.save(RecordedTest(JavaTest("suite.Api", name), Outcome.PASSED, 1.0, [exchange]))
    restcov = FakeRestCov()
    coverage = SuiteCoverage.measure(recording, restcov, tmp_path / "coverage")
    code = FakeRecordingCode()
    study = SelectionStudy(recording, coverage, restcov, [Budget(0.5)], draws=2, seed=1, code=code)  # type: ignore[arg-type]
    suites = study.run(tmp_path / "selection")

    assert code.asked is not None
    versions, reference, listed = code.asked
    assert sorted(versions) == ["random-50-1", "random-50-2", "selected-50-0"]
    assert len(reference) == 4
    assert listed == frozenset({"selected-50-0"})
    assert [suite.code.share("lines") for suite in suites if suite.code] == [0.5, 0.5, 0.5]
    assert (tmp_path / "selection" / "code-lost.md").exists()

    # A later run without code coverage must not leave the old list behind.
    SelectionStudy(recording, coverage, restcov, [Budget(0.5)], draws=2, seed=1).run(tmp_path / "selection")
    assert not (tmp_path / "selection" / "code-lost.md").exists()


def test_the_code_table_lists_the_largest_budget_first() -> None:
    def version(budget: float, lines: int) -> dict[str, object]:
        return {
            "version": "selected",
            "budget": budget,
            "kept": 1,
            "requests": 1,
            "lost": _counts(),
            "code": _code(lines),
        }

    result = {
        "tests": 4,
        "requests": 4,
        "pool": 4,
        "draws": 0,
        "full": _counts(),
        "versions": [version(0.25, 50), version(0.75, 90)],
    }
    lines = StudyReport(result).markdown().splitlines()
    rows = [line for line in lines if line.startswith(("| 75% | 1 | Selected | 90.0%", "| 25% | 1 | Selected | 50.0%"))]
    assert [row[:5] for row in rows] == ["| 75%", "| 25%"]


@pytest.mark.parametrize(
    ("gap", "endpoint", "label"), [(None, None, "n/a"), (0.5, 0.4, "40.0% to 50.0%"), (-0.5, -0.4, "-50.0% to -40.0%")]
)
def test_the_ceiling_report_shows_both_ends_of_an_unproven_gap(
    gap: float | None, endpoint: float | None, label: str
) -> None:
    """Every code table row lines up with the header and shows the available proof."""
    row = dict(budget=0.5, kept=1, requests=1, lost=_counts(), code=_code(40))
    result = dict(
        tests=2,
        pool=2,
        draws=1,
        requests=2,
        full=_counts(),
        versions=[
            dict(
                row,
                version="selected",
                gap_closed=gap,
                gap_closed_at_bound=endpoint,
                code_ceiling=dict(status="time limit", lines=44, bound=46, gap=2 / 44),
            ),
            dict(row, version="random"),
        ],
        minimum_code_covers={"0.95": dict(tests=["C.a"], lines=95, bound=1, status="optimal")},
    )
    report = StudyReport(result).markdown()
    table = report.split("## Server code kept per budget")[1].split("The ceiling picks")[0]
    assert {line.count("|") for line in table.splitlines() if line.startswith("|")} == {12}
    assert f"| 44 | {label} | time limit (upper bound 46, gap 4.5%) |" in table
    assert "| 95% | 1 | 95 | optimal | 1 |" in report
