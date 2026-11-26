import gzip
import io
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from lesstests.api import ApiDescription, Operation
from lesstests.cli import main
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.faults import Edit, FaultFamily, ProxyFault, ResponseFaults
from lesstests.java import JavaTest, Outcome
from lesstests.proxy import (
    Address,
    ExchangeLog,
    RecordingProxy,
    Redactor,
    _ForwardingHandler,
    _LoggingServer,
    _TunnelHandler,
)
from lesstests.recording import RecordedTest, Recording

API = ApiDescription([Operation("GET", "/pets/{pet}", frozenset())], [])


def _exchange(
    data: bytes, target: str = "/pets/42?command=get", status: int = 200, headers: Headers | None = None
) -> Exchange:
    """Build a response from the fake service.

    Returns:
        Its exchange.
    """
    request = HttpRequest("GET", target, Headers(), Body())
    return Exchange(request, HttpResponse(status, "upstream", headers or Headers(), Body(data)))


@pytest.mark.parametrize(
    ("data", "path", "kind", "expected"),
    [
        (b"false", (), "wrong value.boolean", True),
        (b'{"a":2.5}', ("a",), "wrong value.number", {"a": 3.5}),
        (b'"changed"', (), "wrong value.string", "other"),
        (b'{"id":"---"}', ("id",), "wrong id", {"id": "---1"}),
        (b'{"id":12}', ("id",), "wrong id", {"id": 13}),
        (b'{"petId":"A-001"}', ("petId",), "wrong id", {"petId": "A-000"}),
        (b'{"a.b":null}', ("a.b",), "missing field", {}),
        (b"[[],[1],[2]]", (None,), "empty list", [[], [], []]),
        (b"[1]", (), "empty list", []),
    ],
)
def test_body_edit(data: bytes, path: tuple[str | None, ...], kind: str, expected: Any) -> None:
    """Requirement: Each body edit changes the bytes and every matching JSON location."""
    fault = ProxyFault(API, FaultFamily("GET /pets/{pet}", "get", 200, path, kind))
    exchange = _exchange(data)
    response = fault.apply(exchange)
    assert fault.applied
    assert response.body.data != data
    assert json.loads(response.body.data) == expected
    assert fault.apply(exchange) is exchange.response


@pytest.mark.parametrize(("original", "changed"), [(201, 400), (404, 200)])
def test_status_keeps_binary_body(original: int, changed: int) -> None:
    """Requirement: A status edit keeps every upstream body byte."""
    exchange = _exchange(b"\xff\x00", status=original)
    fault = ProxyFault(API, FaultFamily("GET /pets/{pet}", "get", original, (), "status"))
    response = fault.apply(exchange)
    assert fault.hit and fault.hit.exchange is exchange
    assert fault.hit.edits == (Edit((), str(original), str(changed)),)
    assert response.status == changed
    assert response.body is exchange.response.body
    assert response.headers is exchange.response.headers


@pytest.mark.parametrize(
    "exchange",
    [
        _exchange(b"1e200"),
        _exchange(b"null"),
        _exchange(b"bad json"),
        _exchange(b"NaN"),
        _exchange(b"123", status=201),
        _exchange(b"1", "/elsewhere"),
        _exchange(b"1", "/pets/1?command=put"),
        _exchange(b"1", headers=Headers((("Content-Encoding", "gzip"),))),
        _exchange(b"1", headers=Headers((("Content-Encoding", "br"),))),
    ],
)
def test_ineligible_response_keeps_fault_armed(exchange: Exchange) -> None:
    """Requirement: A mismatch or an unreadable body leaves the fault available."""
    fault = ProxyFault(API, FaultFamily("GET /pets/{pet}", "get", 200, (), "wrong value.number"))
    assert fault.apply(exchange) is exchange.response
    assert not fault.applied
    assert fault.apply(_exchange(b"1")).body == Body(b"2")


def test_concurrent_matches() -> None:
    """Requirement: Concurrent matches receive a single edit between them."""
    exchange = _exchange(b"false")
    fault = ProxyFault(API, FaultFamily("GET /pets/{pet}", "get", 200, (), "wrong value.boolean"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(fault.apply, [exchange] * 64))
    assert sum(r.body == Body(b"true") for r in responses) == 1


def test_forwarded_bytes_and_headers() -> None:
    """Requirement: The proxy sends the first edit with current headers and then forwards untouched bytes."""
    data = gzip.compress(b'{"a": 1}')
    headers = Headers((("Content-Encoding", "gzip"), ("ETag", "abc"), ("Content-Length", str(len(data)))))
    exchange = _exchange(data, headers=headers)
    fault = ProxyFault(API, FaultFamily("GET /pets/{pet}", "get", 200, ("a",), "wrong value.number"))
    proxy = RecordingProxy(Address("localhost", 0), Address("localhost", 0), fault=fault)
    handler = Mock(spec=_ForwardingHandler)
    handler.command = "GET"
    handler._request.return_value = exchange.request
    handler._read_body.return_value = b""
    handler._send_upstream.return_value = exchange.response
    server = Mock(spec=_LoggingServer)
    server.log = proxy.log
    with patch.object(proxy, "_make_server", return_value=server), patch("lesstests.proxy.threading.Thread"):
        proxy.start()
        proxy.stop()
    handler._proxy = server
    handler._keep.side_effect = lambda pair, active: _ForwardingHandler._keep(handler, pair, active)
    for body in (b'{"a": 2}', data):
        handler.wfile = io.BytesIO()
        handler.send_header.reset_mock()
        _ForwardingHandler._forward(handler)
        assert handler.wfile.getvalue() == body
        sent = dict(call.args for call in handler.send_header.call_args_list)
        assert sent["Content-Length"] == str(len(body))
        assert ("ETag" in sent) == (body == data)
        assert ("Content-Encoding" in sent) == (body == data)
        assert proxy.log.take()[0][0].response.body.data == body


def test_saved_inventory_and_kept_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Requirement: Fold array indexes and concrete routes while keeping commands and original statuses distinct."""
    recording = Recording(tmp_path / "recording")
    first = _exchange(b'[{"x":1},{"x":2}]')
    second = _exchange(b'[{"x":3}]', "/pets/3?command=get")
    other = _exchange(b"[]", "/pets/3?command=list")
    for name, exchanges in (("a", [first, first]), ("b", [second, other, _exchange(b"[]", status=404)])):
        recording.save(RecordedTest(JavaTest("suite", name), Outcome.PASSED, 1.0, exchanges))
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"openapi": "3.0.0", "paths": {"/pets/{pet}": {"get": {}}}}))
    recording.write_meta({"spec": str(spec)})
    kept = tmp_path / "kept.txt"
    recording.save(RecordedTest(JavaTest("suite", "empty"), Outcome.PASSED, 1.0, []))
    kept.write_text("suite.a\n  \nsuite.empty\n")
    out = tmp_path / "families.json"
    main(["faults", "--recording", str(recording.folder), "--out", str(out), "--kept", str(kept)])
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "families": 6,
        "per_change": {"empty list": 1, "missing field": 1, "status": 3, "wrong value": 1},
        "single_receiver": 2,
        "no_kept_receiver": 2,
    }
    rows = json.loads(out.read_text())
    row = next(row for row in rows if row["change"] == "wrong value.number")
    assert row["tests"] == ["suite.a", "suite.b"]
    assert row["path"] == [None, "x"]
    assert json.loads(ProxyFault(API, FaultFamily.from_dict(row)).apply(first).body.data) == [{"x": 2}, {"x": 3}]

    kept.write_text("suite.a\nsuite.typo\n")
    out.unlink()
    with pytest.raises(ValueError, match="Unknown kept test ids: suite.typo"):
        main(["faults", "--recording", str(recording.folder), "--out", str(out), "--kept", str(kept)])
    assert not out.exists()


@pytest.mark.parametrize("charset", ["utf-8", "utf-8-sig", "utf-16", "utf-16-be", "utf-32"])
def test_folded_edit_keeps_surrounding_tokens(charset: str) -> None:
    """Requirement: Edit all three matching fields and keep every other byte."""
    text = ' [ {"a":1, "€":12000.000000}, {"a":2}, {"a":3} ]\r\n'
    exchange = _exchange(text.encode(charset))
    fault = ProxyFault(API, FaultFamily("GET /pets/{pet}", "get", 200, (None, "a"), "wrong value.number"))
    expected = ' [ {"a":2, "€":12000.000000}, {"a":3}, {"a":4} ]\r\n'
    assert fault.apply(exchange).body.data == expected.encode(charset)
    assert fault.hit and fault.hit.exchange is exchange
    assert fault.hit.edits == tuple(Edit((i, "a"), str(i + 1), str(i + 2)) for i in range(3))


def test_remove_member_in_each_position() -> None:
    """Requirement: Removing a member keeps its neighbours valid in every object."""
    exchange = _exchange(b'[{"x":null, "a":1},{"b":2,"x":[],"c":3},{"d":4,"x":[2]}]')
    fault = ProxyFault(API, FaultFamily("GET /pets/{pet}", "get", 200, (None, "x"), "missing field"))
    assert fault.apply(exchange).body.data == b'[{ "a":1},{"b":2,"c":3},{"d":4}]'
    assert fault.hit and fault.hit.edits == tuple(
        Edit((i, "x"), original, None) for i, original in enumerate(("null", "[]", "[2]"))
    )


@pytest.mark.parametrize(
    ("method", "body"), [("GET", b'{"a":1,"a":2}'), ("GET", b'{"a":3.14159265358979323846}'), ("HEAD", b'{"a":1}')]
)
def test_status_only(method: str, body: bytes) -> None:
    """Requirement: HEAD, duplicate names and unrepresentable decimals expose only the status."""
    exchange = _exchange(body)
    exchange = replace(exchange, request=replace(exchange.request, method=method))
    assert [family.change for family in ResponseFaults("operation", exchange).families()] == ["status"]


def test_id_excludes_scalar_edit() -> None:
    """Requirement: An identifier can receive an id edit and cannot receive a scalar edit."""
    kinds = {f.change for f in ResponseFaults("operation", _exchange(b'{"id":"---"}')).families()}
    assert kinds == {"status", "missing field", "wrong id"}


def test_tunnel_skips_passthrough_and_redacts_edited_response() -> None:
    """Requirement: Passthrough leaves the fault armed and the tunnel logs the redacted edit."""
    handler = Mock(spec=_TunnelHandler)
    fault = ProxyFault(API, FaultFamily("GET /pets/{pet}", "get", 200, (), "wrong value.number"))
    handler._proxy.log = ExchangeLog()
    handler._intercepting.passthrough = {"skip"}
    handler._intercepting.redactor = Redactor(frozenset({"secret"}))
    exchange = _exchange(b"1", headers=Headers((("secret", "private"),)))
    handler._host = "skip"
    assert _TunnelHandler._keep(handler, exchange, fault) is exchange.response
    assert not fault.applied
    assert handler._proxy.log.take() == ([], [])
    handler._host = "included"
    response = _TunnelHandler._keep(handler, exchange, fault)
    assert response.headers.get("secret") == "private"
    assert response.body.data == b"2"
    logged = handler._proxy.log.take()[0][0].response
    assert logged.body == response.body
    assert logged.headers.get("secret") == "<redacted>"
