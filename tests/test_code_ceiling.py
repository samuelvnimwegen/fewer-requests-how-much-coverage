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


def test_exact_program_beats_greedy_and_weights_shared_rows() -> None:
    """Requirement: Both programs count every source line and prove the best feasible answer."""
    lines = matrix()
    assert len(lines.weights) == 4
    cover = FewestTests(lines, 5).select(1, 6)
    assert cover.tests == ["C.b", "C.c"]
    assert (cover.status, cover.bound, cover.gap) == ("optimal", 2, 0)
    assert MostLines(lines, 5).select(2).lines == 6
    assert len(FewestTests(lines, 5).select(0.67, 6).tests) == 2


def test_timeout_without_incumbent_keeps_greedy_and_natural_bound() -> None:
    """Requirement: A time limit without a solver suite still reports a feasible suite and its uncertainty."""
    timeout = OptimizeResult(status=1, x=None, mip_dual_bound=None)
    with patch("lesstests.code_ceiling.milp", return_value=timeout):
        result = FewestTests(matrix(), 5).select(1, 6)
    assert (len(result.tests), result.lines, result.bound) == (3, 6, 1)
    assert result.gap == pytest.approx(2 / 3)
    assert "time limit" in result.status


def test_timeout_keeps_improved_incumbent_and_code_bound() -> None:
    """Requirement: A timed incumbent carries an upper bound in lines and no claim of a solver proof."""
    timeout = OptimizeResult(status=1, x=np.array([0, 1, 1, 1, 1, 1, 1]), mip_dual_bound=-6)
    with patch("lesstests.code_ceiling.milp", return_value=timeout):
        result = MostLines(matrix(), 5).select(2)
    assert (result.tests, result.lines, result.bound, result.gap, result.status) == (
        ["C.b", "C.c"],
        6,
        6,
        0,
        "time limit",
    )


def test_passing_pool_and_random_tie() -> None:
    """Requirement: The ceiling uses the study pool and leaves the ratio undefined when Random ties it."""
    a, b = JavaTest("C", "a"), JavaTest("C", "b")
    study = CeilingStudy({a: {0, 1}, b: {2}}, [b], 2)
    row = dict(budget=1, tests=[b.id], kept=1, code={"kept": {"lines": 1}, "of": {"lines": 3}})
    versions: list[dict[str, Any]] = [dict(version=name, **row) for name in ("selected", "random")]
    study.measure(dict(pool=1, tests=2, versions=versions), [1])
    assert versions[0]["code_ceiling"]["lines"] == 1
    assert versions[0]["gap_closed"] is None
