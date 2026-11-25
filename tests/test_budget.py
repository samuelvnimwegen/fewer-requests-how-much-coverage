from scipy.optimize import OptimizeResult

from lesstests.budget import BudgetAnalysis, BudgetWarning, Count, ItemBudget
from lesstests.coverage import CoverageItem, CoverageSet, Criterion, SuiteCoverage
from lesstests.java import JavaTest
from lesstests.traffic import TrafficCoverage


def test_pick_can_miss_more_than_the_budget_forces() -> None:
    """Requirement: Independent family optima and the losses of the actual pick are measured separately."""
    a, b = JavaTest("Api", "a"), JavaTest("Api", "b")
    items = CoverageSet([CoverageItem(Criterion.STATUS, "GET /a 200")])
    suite = SuiteCoverage({a: items, b: CoverageSet()}, items, {})
    traffic = TrafficCoverage({"fields": {(str(i),): {b} for i in range(8)}})
    warning = BudgetAnalysis(suite, [a, b], traffic).measure([a])
    assert warning.kinds["fields"] == ItemBudget(8, Count(8), Count(1))
    assert warning.kinds["recorded items"] == ItemBudget(9, Count(8), Count(2))
    assert len(warning.lost["fields"]) == 8
    assert "0, 1, 2, 3, 4 …" in warning.report()
    restricted = BudgetAnalysis(suite, [a], traffic).measure([a])
    assert restricted.kinds["fields"] == ItemBudget(8, Count(0), None)


def test_time_limit_reports_only_feasible_bounds() -> None:
    """Requirement: Unfinished solves report feasible bounds even without an incumbent."""
    limit = OptimizeResult(success=False, status=1, fun=None)
    most = Count.from_solve(limit, 3, -1)
    assert str(most) == "at least 3"
    limit.fun = 7
    minimum = Count.from_solve(limit, 9)
    assert str(minimum) == "at most 7"
    warning = BudgetWarning(2, 9, {"RESTCov items": ItemBudget(8, most, minimum)}, {})
    assert "time limit" in warning.message()
    assert "at most 7 tests" in warning.message()
    assert "disappear" not in warning.message()
