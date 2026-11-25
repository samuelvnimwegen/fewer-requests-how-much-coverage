from lesstests.java import JavaTest
from lesstests.ranking import MethodNovelty


def test_novelty_tracks_methods_from_visits_without_new_items() -> None:
    """Requirement: An earlier test can reach a method even when it adds no family item."""
    a, b = JavaTest("suite.Api", "a"), JavaTest("suite.Api", "b")
    study = MethodNovelty({a: {"same"}, b: {"same"}})
    both, new = study.count({("only",): {a}})
    assert new == 30
    assert 0 < both < new
    assert study.count({("both",): {a, b}}) == (30, 30)
    assert study.count({}) == (0, 0)
