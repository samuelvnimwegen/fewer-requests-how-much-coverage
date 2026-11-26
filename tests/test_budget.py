import pytest
from scipy.optimize import OptimizeResult

from lesstests.budget import Bound, BudgetAnalysis, Count, ItemBudget
from lesstests.coverage import CoverageItem, CoverageSet, Criterion, SuiteCoverage
from lesstests.java import JavaTest
from lesstests.selection import CoverageSelector
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
    assert restricted.kinds["fields"] == ItemBudget(8, Count(0), Count(0), 8)
    assert "Keeping all traffic items takes 1 test." in warning.message()
    assert "This pick loses 8 of your 9 recorded items." in warning.message()
    assert "8 of your 9 items come only from tests that did not pass." in restricted.message()
    extra = JavaTest("Api", "both")
    suite.per_test[extra] = items
    traffic.witnesses["fields"] = {key: {extra, b} for key in traffic.witnesses["fields"]}
    avoidable = BudgetAnalysis(suite, [a, b, extra], traffic).measure([a])
    assert "That size could keep every item, but this pick loses 8 of them." in avoidable.message()


def test_time_limit_keeps_the_solver_bound_and_the_known_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement: A time limit keeps the proven loss and the best known coverage."""
    a, b, c = (JavaTest("Api", name) for name in "abc")
    items = [CoverageItem(Criterion.PATH, name) for name in "abcdef"]
    per_test = {test: CoverageSet(items[i : i + 2]) for i, test in zip(range(0, 6, 2), [a, b, c], strict=True)}
    suite = SuiteCoverage(per_test, CoverageSet(items), {})
    limit = OptimizeResult(success=False, status=1, fun=-1, mip_dual_bound=-3.8)
    monkeypatch.setattr(CoverageSelector, "maximum", lambda *args, **kwargs: limit)
    warning = BudgetAnalysis(suite, [a, b, c], None).measure([a])
    assert warning.restcov.most == Count(2, Bound.LOWER)
    assert warning.restcov.forced_loss == 3
    assert "Any 1 test loses at least 3 of your 6 RESTCov items." in warning.message()
    assert "stopped after 30 seconds" in warning.message()
    limit.fun = None
    limit.mip_dual_bound = None
    warning = BudgetAnalysis(suite, [a, b, c], None).measure([a])
    assert warning.restcov.most == Count(2, Bound.LOWER)
    assert warning.restcov.forced_loss == 0
    assert str(Count.from_solve(limit, 9)) == "at most 9"


def test_empty_recording_has_no_loss_rows() -> None:
    """Requirement: An empty recording explains that there are no items to lose."""
    suite = SuiteCoverage({}, CoverageSet(), {})
    warning = BudgetAnalysis(suite, [], None).measure([])
    assert "There are no recorded items to lose." in warning.message()
    assert "| paths |" not in warning.report()
