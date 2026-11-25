from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from scipy.optimize import OptimizeResult

from lesstests.code_ceiling import CeilingStudy, FewestTests, LineMatrix, MostLines
from lesstests.java import JavaTest


def matrix() -> LineMatrix:
    return LineMatrix(
        {
            JavaTest("C", "a"): {0, 1, 2, 3},
            JavaTest("C", "b"): {0, 1, 4},
            JavaTest("C", "c"): {2, 3, 5},
        }
    )


def test_shared_rows_keep_their_line_counts() -> None:
    """Lines reached by the same tests share a row and retain their weight."""
    lines = matrix()
    assert sorted(lines.weights) == [1, 1, 2, 2]


def test_the_smallest_cover_beats_the_greedy_pick() -> None:
    """Two tests reach every line."""
    cover = FewestTests(matrix(), 5).select(1)
    assert cover.tests == ["C.b", "C.c"]
    assert (cover.status, cover.bound, cover.gap) == ("optimal", 2, 0)


def test_a_fixed_size_suite_can_beat_the_greedy_pick() -> None:
    """The best two tests reach all six lines."""
    assert MostLines(matrix(), 5).select(2).lines == 6


def test_a_partial_target_rounds_up_to_a_whole_line() -> None:
    """A target just above four lines needs two tests."""
    assert len(FewestTests(matrix(), 5).select(0.67).tests) == 2


def test_a_time_limit_without_an_incumbent_keeps_the_greedy_suite() -> None:
    """A time limit without a solver suite still reports a feasible suite and its uncertainty."""
    timeout = OptimizeResult(status=1, x=None, mip_dual_bound=None)
    with patch("lesstests.code_ceiling.milp", return_value=timeout):
        result = FewestTests(matrix(), 5).select(1)
    assert (len(result.tests), result.lines, result.bound) == (3, 6, 1)
    assert result.gap == pytest.approx(2 / 3)
    assert "time limit" in result.status


def test_the_integer_bound_proves_the_improved_suite_is_optimal() -> None:
    """A feasible suite at the whole line bound proves the optimum."""
    timeout = OptimizeResult(status=1, x=np.array([0, 1, 1, 1, 1, 1, 1]), mip_dual_bound=-6.6)
    with patch("lesstests.code_ceiling.milp", return_value=timeout):
        result = MostLines(matrix(), 5).select(2)
    assert (result.tests, result.lines, result.bound, result.gap, result.status) == (
        ["C.b", "C.c"],
        6,
        6,
        0,
        "optimal by bound",
    )


def test_the_ratio_is_undefined_when_random_ties_the_ceiling() -> None:
    """The ceiling uses the study pool and leaves the ratio undefined when Random ties it."""
    a, b = JavaTest("C", "a"), JavaTest("C", "b")
    study = CeilingStudy({a: {0, 1}, b: {2}}, [b], 2)
    row = dict(budget=1, tests=[b.id], kept=1, code={"kept": {"lines": 1}, "of": {"lines": 3}})
    versions: list[dict[str, Any]] = [dict(version=name, **row) for name in ("selected", "random")]
    study.measure(dict(pool=1, tests=2, versions=versions), [1])
    assert versions[0]["code_ceiling"]["lines"] == 1
    assert versions[0]["gap_closed"] is None


def test_a_greedy_suite_at_the_bound_skips_the_solver() -> None:
    """A one test full cover already proves the minimum."""
    with patch("lesstests.code_ceiling.milp") as solve:
        result = FewestTests(matrix(), 2).select(0.5)
    assert result.status == "optimal by bound"
    solve.assert_not_called()


def test_fractional_test_bounds_round_up_and_reject_an_infeasible_incumbent() -> None:
    """The reported suite meets the line target at the time limit."""
    timeout = OptimizeResult(status=1, x=np.array([1, 0, 0]), mip_dual_bound=1.2)
    with patch("lesstests.code_ceiling.milp", return_value=timeout):
        result = FewestTests(matrix(), 2).select(1)
    assert (result.lines, len(result.tests), result.bound) == (6, 3, 2)


def test_a_solver_suite_with_an_extra_test_cannot_raise_the_ceiling() -> None:
    """Only suites of the requested size count."""
    timeout = OptimizeResult(status=1, x=np.ones(3), mip_dual_bound=-5.6)
    with patch("lesstests.code_ceiling.milp", return_value=timeout):
        result = MostLines(matrix(), 2).select(1)
    assert (len(result.tests), result.lines, result.bound) == (1, 4, 5)


@pytest.mark.parametrize("saved", [1, 2])
def test_the_gap_uses_the_random_mean_and_checks_saved_line_counts(saved: int) -> None:
    """The fraction uses recounted lines for every suite."""
    a, b, c = [JavaTest("C", name) for name in "abc"]
    study = CeilingStudy({a: {1, 2, 3}, b: {4}, c: {1, 2, 3, 4}}, [a, b, c], 2)
    rows: list[dict[str, Any]] = [
        dict(version=name, budget=0.3, tests=[test.id], kept=1, code={"kept": {"lines": count}, "of": {"lines": 4}})
        for name, test, count in [("selected", a, 3), ("random", b, saved), ("random", a, 3)]
    ]
    result = dict(pool=3, tests=3, versions=rows)
    if saved == 2:
        with pytest.raises(ValueError, match="saved line counts"):
            study.measure(result, [])
    else:
        study.measure(result, [])
        assert rows[0]["gap_closed"] == 0.5


def test_the_time_limit_ratio_uses_the_feasible_count_and_the_upper_bound() -> None:
    """The unknown ceiling leaves a range for the share of the gap Selected closes."""
    a, b, c = [JavaTest("C", name) for name in "abc"]
    lines = {a: set(range(40)), b: set(range(36)), c: set(range(2, 46))}
    study = CeilingStudy(lines, [a, b, c], 2)
    versions: list[dict[str, Any]] = [
        dict(
            version=name,
            budget=1 / 3,
            tests=[test.id],
            kept=1,
            code={"kept": {"lines": len(lines[test])}, "of": {"lines": 46}},
        )
        for name, test in [("selected", a), ("random", b)]
    ]
    with patch("lesstests.code_ceiling.milp", return_value=OptimizeResult(status=1, x=None, mip_dual_bound=-46)):
        study.measure(dict(pool=3, tests=3, versions=versions), [])
    assert (versions[0]["gap_closed_at_bound"], versions[0]["gap_closed"]) == (0.4, 0.5)
