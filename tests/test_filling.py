import gzip

import pytest

from lesstests.coverage import CoverageItem, CoverageSet, Criterion, SuiteCoverage
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.filling import FILLING_RULES, CoverTwice, FillPool, MostDifferent, MostNewValues
from lesstests.java import JavaTest, Outcome
from lesstests.recording import RecordedTest
from lesstests.selection import CoverageMatrix
from lesstests.traffic_values import FieldValues, Traffic


def _pool() -> FillPool:
    records = {}
    hits = {}
    for name, body, paths in [
        ("a", b'{"x":"same"}', "xy"),
        ("b", b'{"x":"same"}', "x"),
        ("c", b'{"x":null}', "y"),
        ("d", b'{"x":"same"}', ""),
    ]:
        test = JavaTest("Suite", name)
        req = HttpRequest("POST", "/prefix/widgets/12?blank=", Headers(), Body(body))
        res = HttpResponse(200, "", Headers(), Body())
        records[test] = RecordedTest(test, Outcome.PASSED, 1, [Exchange(req, res)])
        hits[test] = CoverageSet(CoverageItem(Criterion.PATH, p) for p in paths)
    full = hits[JavaTest("Suite", "a")]
    coverage = SuiteCoverage(hits, full, {})
    lengths = dict.fromkeys(records, 1)
    return FillPool(CoverageMatrix(coverage, lengths, list(records)), coverage, lengths, records, "/prefix", 17)


@pytest.mark.parametrize("rule", [MostNewValues, MostDifferent])
def test_condition_on_existing_floor(rule: type[MostNewValues] | type[MostDifferent]) -> None:
    """Requirement: A test with a new value beats a duplicate of a test in the floor."""
    picker = rule(_pool())
    assert [t.method for t in picker.select(2)] == ["a", "c"]
    assert [t.method for t in picker.select(3)] == ["a", "b", "c"]
    assert len(set(picker.select(4))) == 4


def test_second_hits() -> None:
    """Requirement: An item already covered twice gives no further gain."""
    picker = CoverTwice(_pool())
    assert [t.method for t in picker.select(3)] == ["a", "b", "c"]


@pytest.mark.parametrize("name", FILLING_RULES)
def test_empty_eligible_pool(name: str) -> None:
    """Requirement: A budget can return an empty suite when no test is eligible."""
    coverage = SuiteCoverage({}, CoverageSet(), {})
    pool = FillPool(CoverageMatrix(coverage, {}, []), coverage, {}, {}, "", 1)
    assert FILLING_RULES[name](pool).select(1) == []


def test_field_boundaries() -> None:
    """Requirement: Field statistics count each value once per test and find extremes across tests."""
    field = FieldValues()
    for index, value in enumerate([1, "1", 1, 2, 2, -0.5]):
        field.observe(index, value)
        field.observe(index, value)
    tags = dict(field.boundaries())
    assert tags["negative",] == tags["fraction",] == tags["minimum",] == {5}
    assert tags["maximum",] == {3, 4}
    assert tags["number", "1.0"] == {0, 1, 2}


def test_traffic_fields() -> None:
    """Requirement: Compressed JSON keeps array fields, nulls and empty containers distinct."""
    body = Body(gzip.compress(b'{"xs":[{"a":null},{}],"empty":[]}'))
    assert list(Traffic.fields(body, Headers())) == [
        ("/xs[]/a", None),
        ("/xs[]", "<EMPTY_OBJ>"),
        ("/empty", "<EMPTY_LIST>"),
    ]
    assert list(Traffic.fields(Body(b"not JSON"), Headers())) == []
    request = HttpRequest("DELETE", "/api/12/abc123456789?blank=&x=3", Headers(), Body())
    assert Traffic.operation(request, "/api") == ("DELETE /{id}/{tok}", [("blank", ""), ("x", "3")])
