import gzip
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from test_proxy import EchoHandler

from lesstests.api import ApiDescription, Operation
from lesstests.cli import main
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.faults import FaultFamily, ProxyFault
from lesstests.java import JavaTest, Outcome
from lesstests.proxy import Address, RecordingProxy, _ForwardingHandler, _LoggingServer
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
        (b'{"id":12}', ("id",), "wrong id", {"id": 13}),
        (b'{"petId":"A-001"}', ("petId",), "wrong id", {"petId": "A-000"}),
        (b'{"a.b":null}', ("a.b",), "missing field", {}),
        (b"[[],[1],[2]]", (None,), "empty list", [[], [], [2]]),
        (b"[1]", (), "empty list", []),
    ],
)
def test_body_edit(data: bytes, path: tuple[str | None, ...], kind: str, expected: Any) -> None:
    """Requirement: Each body edit changes the bytes and only its first eligible JSON location."""
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
    server.response.side_effect = lambda pair: _LoggingServer.response(server, pair)
    handler._proxy = server
    handler._keep.side_effect = lambda pair: _ForwardingHandler._keep(handler, pair)
    for body in (b'{"a":2}', data):
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
    kept.write_text("suite.a\n")
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
    assert json.loads(ProxyFault(API, FaultFamily.from_dict(row)).apply(first).body.data) == [{"x": 2}, {"x": 2}]


def test_default_proxy_keeps_echo_response() -> None:
    """Requirement: An unarmed proxy keeps the bytes and headers of the upstream response."""
    echo = Mock(spec=EchoHandler)
    echo.path = "/pets/42?command=get"
    echo.command = "GET"
    echo.headers = {}
    echo.wfile = io.BytesIO()
    EchoHandler.do_GET(echo)
    headers = Headers(tuple(call.args for call in echo.send_header.call_args_list))
    exchange = _exchange(echo.wfile.getvalue(), status=echo.send_response.call_args.args[0], headers=headers)
    server = Mock(spec=_LoggingServer)
    server.fault = None
    assert _LoggingServer.response(server, exchange) is exchange.response
