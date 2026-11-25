import gzip
import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from scipy.sparse import csr_matrix

from lesstests.coverage import CoverageItem, CoverageSet, Criterion, SuiteCoverage
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.filling import FILLING_RULES, CoverTwice, FillPool, MostDifferent, MostNewValues
from lesstests.java import JavaTest, Outcome
from lesstests.recording import RecordedTest, Recording
from lesstests.selection import CoverageMatrix
from lesstests.traffic_values import FieldValues, Traffic


def _pool(tmp_path: Path) -> FillPool:
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
    recording = Recording(tmp_path)
    recording.write_meta({"restcov_base_path": "/prefix"})
    return FillPool(CoverageMatrix(coverage, lengths, list(records)), coverage, lengths, records, recording, 17)


@pytest.mark.parametrize("rule", [MostNewValues, MostDifferent])
def test_condition_on_existing_floor(tmp_path: Path, rule: type[MostNewValues] | type[MostDifferent]) -> None:
    """Requirement: A test with a new value beats a duplicate of a test in the floor."""
    picker = rule(_pool(tmp_path))
    assert [t.method for t in picker.select(2)] == ["a", "c"]
    assert [t.method for t in picker.select(3)] == ["a", "b", "c"]
    assert len(set(picker.select(4))) == 4


def test_second_hits(tmp_path: Path) -> None:
    """Requirement: An item already covered twice gives no further gain."""
    picker = CoverTwice(_pool(tmp_path))
    assert [t.method for t in picker.select(3)] == ["a", "b", "c"]


@pytest.mark.parametrize("name", FILLING_RULES)
def test_empty_eligible_pool(tmp_path: Path, name: str) -> None:
    """Requirement: A budget can return an empty suite when no test is eligible."""
    coverage = SuiteCoverage({}, CoverageSet(), {})
    pool = FillPool(CoverageMatrix(coverage, {}, []), coverage, {}, {}, Recording(tmp_path), 1)
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


def test_roundoff_after_two_directions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement: Once the floor spans both token blocks, additions follow original squared length."""
    pool = _pool(tmp_path)
    tests = [JavaTest("C", name) for name in "abcde"]
    pool.matrix.tests = tests
    rule = MostDifferent(pool)
    weight = 1 + math.log(6 / 4)
    a, b = 10000 * weight**2, 10 * weight**2
    kernel = np.array([[a, 0, a, 0, a], [0, b, 0, b, b], [a, 0, a, 0, a], [0, b, 0, b, b], [a, b, a, b, a + b]])
    vectors = csr_matrix(np.array([[1, 0], [0, 1], [1, 0], [0, 1], [1, 1]]) * np.sqrt([a, b]))
    monkeypatch.setattr(MostDifferent, "kernel", kernel)
    monkeypatch.setattr(MostDifferent, "vectors", vectors)
    assert rule.fill(tests[:2], tests[2:], 3) == [tests[4], tests[2], tests[3]]
    assert rule.fill(tests[:3], tests[3:], 2) == [tests[4], tests[3]]


def test_feasible_second_hits(tmp_path: Path) -> None:
    """Requirement: Two free slots give every item a second hit when a pair covers them all."""
    tests = [JavaTest("C", name) for name in "fabc"]
    hits = {
        t: CoverageSet(CoverageItem(Criterion.PATH, x) for x in items)
        for t, items in zip(tests, ["abcdef", "abcd", "abe", "cdf"], strict=True)
    }
    coverage = SuiteCoverage(hits, hits[tests[0]], {})
    requests = dict.fromkeys(tests, 1)
    pool = FillPool(CoverageMatrix(coverage, requests, tests), coverage, requests, {}, Recording(tmp_path), 1)
    assert set(CoverTwice(pool).select(3)) == {tests[0], tests[2], tests[3]}


@pytest.mark.parametrize("rule", [MostNewValues, MostDifferent])
def test_group_paths_before_breaking_ties(tmp_path: Path, rule: type[MostNewValues] | type[MostDifferent]) -> None:
    """Requirement: Numeric paths with a final newline group together and full IDs resolve tied scores."""
    pool = _pool(tmp_path)
    tests = [JavaTest.from_id(name) for name in ("C.a", "C.b", "C$Inner.a")]
    exchange = next(iter(pool.records.values())).exchanges[0]
    for test, suffix in zip(tests, ("12", "12%0A", "13"), strict=True):
        request = replace(exchange.request, target="/v2/things/" + suffix)
        pool.records[test] = RecordedTest(test, Outcome.PASSED, 1, [replace(exchange, request=request)])
    pool.matrix.tests = sorted(tests)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"servers": [{"url": "/"}, {"url": "/v2"}]}))
    pool.recording.write_meta({"spec": str(spec)})
    picker = rule(pool)
    assert picker.prefix == ""
    assert picker.fill([], pool.matrix.tests, 1) == [tests[2]]
