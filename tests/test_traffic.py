import gzip
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


def test_a_gzip_body_gives_the_same_fields_as_a_plain_one() -> None:
    """Requirement: A request body sent with gzip counts its fields like an uncompressed body."""
    data = json.dumps({"items": [[{"colour": "red"}]]}).encode()
    packed = Exchange(
        HttpRequest("POST", "/api/orders/new", Headers((("Content-Encoding", "gzip"),)), Body(gzip.compress(data))),
        HttpResponse(200, "", Headers(), Body()),
    )
    broken = Exchange(
        HttpRequest("POST", "/api/orders/new", Headers((("Content-Encoding", "gzip"),)), Body(data)),
        HttpResponse(200, "", Headers(), Body()),
    )
    a, b = _record("a", packed), _record("b", broken)
    assert _traffic([a, b], "fields").witnesses["fields"] == {("POST /orders/new", ".items[][].colour"): {a.test}}


def test_a_path_of_the_requests_own_method_wins_over_a_literal_path_of_another_method() -> None:
    """Requirement: A request belongs to an operation of its own method when one covers its path."""
    full = CoverageSet(
        [
            CoverageItem(Criterion.PATH, "/orders/new"),
            CoverageItem(Criterion.PATH, "/orders/{id}"),
            CoverageItem(Criterion.OPERATION, "GET /orders/new"),
            CoverageItem(Criterion.OPERATION, "put /orders/{id}"),
        ]
    )
    a = _record("a", _exchange("PUT", "/api/orders/new?command=x"), _exchange("GET", "/api/orders/new?command=y"))
    b = _record("b", _exchange("PUT", "/api/orders/old?command=x"))
    commands = TrafficCoverage.collect([a, b], ApiDescription([], ["/api"]), full, ("commands",)).witnesses["commands"]
    assert commands == {("PUT /orders/{id}", "x"): {a.test, b.test}, ("GET /orders/new", "y"): {a.test}}


def test_a_named_choice_inside_an_array_is_a_mode() -> None:
    """Requirement: A choice field that holds a list still gives one mode per value."""
    a = _record("a", _exchange("POST", "/api/orders/new", {"paymentMethod": ["cash", "card"]}))
    assert set(_traffic([a], "modes").witnesses["modes"]) == {
        ("POST /orders/new", ".paymentMethod[]", '"cash"'),
        ("POST /orders/new", ".paymentMethod[]", '"card"'),
    }


def test_a_body_nested_deeper_than_the_decoder_allows_gives_no_fields() -> None:
    """Requirement: A body the JSON decoder cannot walk gives no fields and no crash."""
    deep = Exchange(
        HttpRequest("POST", "/api/orders/new", Headers(), Body(b"[" * 100_000 + b"0" + b"]" * 100_000)),
        HttpResponse(200, "", Headers(), Body()),
    )
    assert _traffic([_record("a", deep)], "fields").witnesses["fields"] == {}


def test_a_rejected_update_keeps_the_readback_and_another_host_gives_none() -> None:
    """Requirement: Only a successful change on the same host ends a pending read back."""

    def on(host: str, exchange: Exchange) -> Exchange:
        request = exchange.request
        return Exchange(
            HttpRequest(request.method, request.target, Headers((("Host", host),)), request.body), exchange.response
        )

    create = _exchange("POST", "/orders", {"colour": "red"}, {"resourceId": 7}, 201)
    rejected = _exchange("PATCH", "/orders/7", status=403)
    read = _exchange("GET", "/orders/7", result={"colour": "red"})
    a = _record("a", on("a.example", create), on("a.example", rejected), on("a.example", read))
    b = _record("b", on("a.example", create), on("b.example", read))
    assert _traffic([a, b], "readbacks").witnesses == {"readbacks": {("/orders", "colour"): {a.test}}}


def test_parameter_values_and_pairs_keep_locations_and_test_boundaries() -> None:
    """Requirement: Parameter values keep their location and consecutive operations belong to one test."""
    first = _exchange("GET", "/api/orders/a%20b?id=a+b&id=")
    second = _exchange("PUT", "/api/orders/new")
    a, b = _record("a", first, second), _record("b", second)
    families = _traffic([a, b], "parameter_values", "operation_pairs").witnesses
    op = "GET /orders/{id}"
    assert families["parameter_values"] == {
        (op, "query", "id", "a b"): {a.test},
        (op, "query", "id", ""): {a.test},
        (op, "path", "id", "a b"): {a.test},
    }
    assert families["operation_pairs"] == {(op, "PUT /orders/new"): {a.test}}


def test_response_values_bound_short_scalars_within_each_status() -> None:
    """Requirement: Only response fields with two through twelve short scalar values keep their values."""
    rows = []
    for i in range(13):
        body = {"many": i, "edge": i % 12, "long": "a" * (63 + i), "rows": [{"v": True if i else 1}]}
        rows.append(_record(str(i), _exchange("POST", "/api/orders/new", body, body)))
    rows.append(_record("error", _exchange("POST", "/api/orders/new", result={"edge": 0}, status=400)))
    families = _traffic(rows, "request_values", "response_fields", "response_values").witnesses
    op = "POST /orders/new"
    assert set(families["response_values"]) == {
        *((op, "200", ".edge", str(i)) for i in range(12)),
        (op, "200", ".rows[].v", "1"),
        (op, "200", ".rows[].v", "true"),
    }
    assert families["response_fields"][op, "400", ".edge"] == {rows[-1].test}
    assert families["request_values"][op, ".many", "12"] == {rows[12].test}
