import json

import pytest

from lesstests.api import ApiDescription
from lesstests.coverage import CoverageItem, CoverageSet, Criterion
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JavaTest, Outcome
from lesstests.recording import RecordedTest
from lesstests.traffic import TrafficCoverage


def _exchange(method: str, target: str, body: object = None, result: object = None, status: int = 200) -> Exchange:
    return Exchange(
        HttpRequest(method, target, Headers(), Body(json.dumps(body).encode())),
        HttpResponse(status, "", Headers(), Body(json.dumps(result).encode())),
    )


def _record(name: str, *exchanges: Exchange) -> RecordedTest:
    return RecordedTest(JavaTest("suite.Api", name), Outcome.PASSED, 1, list(exchanges))


def _traffic(records: list[RecordedTest], *families: str) -> TrafficCoverage:
    paths = ["/orders/new", "/orders/{id}"]
    full = CoverageSet(CoverageItem(Criterion.PATH, path) for path in paths)
    return TrafficCoverage.collect(records, ApiDescription([], ["/api"]), full, families)


def test_commands_and_fields_fold_ids_and_indices_without_merging_literal_paths() -> None:
    """Requirement: Commands and nested fields belong to their recorded operation."""
    a = _record("a", _exchange("PUT", "/api/orders/5?command=set%20x&command=start", {"rows": [{"x": 1}, {"x": 2}]}))
    b = _record("b", _exchange("PUT", "/api/orders/8/?command=start", {"rows": [{"x": 3}]}))
    c = _record("c", _exchange("PUT", "/api/orders/new?command=start", {"rows": []}))
    traffic = _traffic([a, b, c], "commands", "fields")
    assert traffic.witnesses["commands"] == {
        ("PUT /orders/{id}", "set x"): {a.test},
        ("PUT /orders/{id}", "start"): {a.test, b.test},
        ("PUT /orders/new", "start"): {c.test},
    }
    assert traffic.witnesses["fields"] == {
        ("PUT /orders/{id}", ".rows[].x"): {a.test, b.test},
        ("PUT /orders/new", ".rows"): {c.test},
    }


def test_modes_use_suite_wide_choices_per_operation() -> None:
    """Requirement: Modes keep typed boolean and named choices with two through twelve values."""
    records = [
        _record(
            str(i),
            _exchange(
                "POST",
                "/api/orders/new",
                {
                    "switch": bool(i % 2),
                    "rateType": i % 12,
                    "payMethod": i,
                    "alwaysEnabled": True,
                    "format": i % 2,
                    "flagType": True if i % 2 else 1,
                },
            ),
        )
        for i in range(13)
    ]
    modes = _traffic(records, "modes").witnesses["modes"]
    op = "POST /orders/new"
    assert set(modes) == {
        (op, ".switch", "true"),
        (op, ".switch", "false"),
        (op, ".flagType", "true"),
        (op, ".flagType", "1"),
        *((op, ".rateType", str(i)) for i in range(12)),
    }


def test_readback_requires_matching_resource_field_type_and_value_in_one_test() -> None:
    """Requirement: A successful read must echo a scalar written on creation in the same test."""
    create = _exchange("POST", "/people/42/orders", {"colour": "red", "active": True, "size": 2}, {"resourceId": 7})
    read = _exchange("GET", "/people/42/orders/7?full=yes", result={"colour": "red", "active": 1, "size": 3})
    a = _record("a", create, read)
    records = [a, _record("b", read, create), _record("c", read), _record("d", create)]
    assert _traffic(records, "readbacks").witnesses == {"readbacks": {("/people/{}/orders", "colour"): {a.test}}}


@pytest.mark.parametrize("event", ["update", "duplicate", "wrong id", "failed create", "failed read"])
def test_readback_excludes_invalid_witnesses(event: str) -> None:
    """Requirement: An ambiguous create or a failed or unrelated read gives no witness."""
    create = _exchange(
        "POST", "/orders", {"colour": "red"}, {"resourceId": 7}, 400 if event == "failed create" else 201
    )
    path = "/orders/8" if event == "wrong id" else "/orders/7"
    read = _exchange("GET", path, result={"colour": "red"}, status=400 if event == "failed read" else 200)
    between = {"update": [_exchange("PATCH", path)], "duplicate": [create]}.get(event, [])
    assert _traffic([_record("a", create, *between, read)], "readbacks").witnesses == {"readbacks": {}}
