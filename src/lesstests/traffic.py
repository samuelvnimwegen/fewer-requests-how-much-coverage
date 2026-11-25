"""Read the behaviours a test shows in its recorded HTTP traffic."""

import gzip
import json
import re
import zlib
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cached_property
from itertools import pairwise
from typing import Any, Protocol, Self
from urllib.parse import parse_qs, parse_qsl, unquote, urlsplit

from lesstests.api import ApiDescription
from lesstests.coverage import CoverageSet, Criterion
from lesstests.exchange import Body, Exchange, Headers
from lesstests.java import JavaTest
from lesstests.recording import RecordedTest

type Item = tuple[str, ...]


class Request:
    """One recorded exchange with its operation and lazily decoded request fields."""

    _escape = str.maketrans({char: "\\" + char for char in ".[]\\"})

    def __init__(self, exchange: Exchange, operation: str, path_values: tuple[tuple[str, str], ...] = ()) -> None:
        """Keep the exchange and its operation name."""
        self.exchange = exchange
        self.operation = operation
        self.path_values = path_values

    @staticmethod
    def body(body: Body, headers: Headers) -> Any:
        """Read a recorded JSON body. The recording keeps a gzip body compressed.

        Returns:
            The decoded value or None for an absent, invalid or too deeply nested body.
        """
        data = body.data
        try:
            if "gzip" in (headers.get("Content-Encoding") or "").lower():
                data = gzip.decompress(data)
            return json.loads(data)
        except (ValueError, OSError, EOFError, zlib.error, RecursionError):
            return None

    @cached_property
    def fields(self) -> list[tuple[str, Any]]:
        """Read leaf values with array positions folded.

        Returns:
            The field paths and their values.
        """
        request = self.exchange.request
        return list(self._leaves(self.body(request.body, request.headers), ""))

    @cached_property
    def response_fields(self) -> list[tuple[str, Any]]:
        """Read the response with the same path rules as the request.

        Returns:
            Field paths and values.
        """
        response = self.exchange.response
        return list(self._leaves(self.body(response.body, response.headers), ""))

    @classmethod
    def _leaves(cls, value: Any, path: str) -> Iterator[tuple[str, Any]]:
        """Walk nonempty containers and keep empty containers as leaves.

        Yields:
            A field path and its recorded value.
        """
        # A stack in place of recursion, so a deeply nested body cannot hit the recursion limit.
        stack = [(value, path)]
        while stack:
            value, path = stack.pop()
            if isinstance(value, dict) and value:
                # Escape path separators inside a key so literal dots cannot look like nested fields.
                stack.extend((child, path + "." + key.translate(cls._escape)) for key, child in reversed(value.items()))
            elif isinstance(value, list) and value:
                stack.extend((child, path + "[]") for child in reversed(value))
            elif path:
                yield path, value


class Family(Protocol):
    """Collect one kind of behaviour within a test."""

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Read the test in recording order.

        Yields:
            Items witnessed by the test.
        """
        ...


class Commands:
    """Dispatch values sent in the command query parameter."""

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Read dispatch values.

        Yields:
            An operation and command value.
        """
        for request in requests:
            query = urlsplit(request.exchange.request.target).query
            for value in parse_qs(query).get("command", []):
                yield request.operation, value


class Fields:
    """Request body field paths per operation."""

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Read the fields that a request sent.

        Yields:
            An operation and field path.
        """
        for request in requests:
            for path, _ in request.fields:
                yield request.operation, path


class Modes:
    """Boolean switches and short sets of named choices."""

    suffix = re.compile(r"(type|method|strategy(code)?|rule|frequency|calculation|enabled)(\[\])*$", re.I)

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Read possible mode values before applying the suite wide limit.

        Yields:
            An operation, field path and typed JSON value.
        """
        for request in requests:
            for path, value in request.fields:
                if (
                    (isinstance(value, bool) or self.suffix.search(path))
                    and isinstance(value, (int, bool, str))
                    and len(str(value)) < 64
                ):
                    yield request.operation, path, json.dumps(value)

    @staticmethod
    def bounded(items: Iterable[Item]) -> set[Item]:
        """Keep fields with two through twelve different values across the full suite.

        Returns:
            The accepted mode items.
        """
        values: dict[Item, set[Item]] = defaultdict(set)
        for item in items:
            values[item[:-1]].add(item)
        return {item for group in values.values() if 2 <= len(group) <= 12 for item in group}


class Readbacks:
    """Fields written on creation and returned by a later read of that resource."""

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Follow integer resourceId values returned by successful creates in this test.

        Yields:
            A collection path and top level field read back with its original scalar value.
        """
        pending: dict[tuple[str, str, str], dict[str, Any]] = {}
        seen: set[tuple[str, str, str]] = set()
        for request in requests:
            req, res = request.exchange.request, request.exchange.response
            url = urlsplit(req.target)
            path = url.path.rstrip("/")

            # The recorder keeps the path only, so the host has to come from its header.
            host = url.netloc or req.headers.get("Host") or ""
            location = (url.scheme, host, path)
            parent, _, tail = path.rpartition("/")
            if not 200 <= res.status < 300:
                continue
            if req.method in {"PUT", "PATCH", "DELETE"} or (req.method == "POST" and tail.isdigit()):
                pending.pop(location, None)
            if req.method == "POST" and not tail.isdigit():
                created = Request.body(res.body, res.headers)
                resource = created.get("resourceId") if isinstance(created, dict) else None
                if type(resource) is int:
                    target = (url.scheme, host, f"{path}/{resource}")
                    written = Request.body(req.body, req.headers)

                    # Repeated create identifiers make the source of a later value ambiguous.
                    if target not in seen and isinstance(written, dict):
                        pending[target] = written
                    else:
                        pending.pop(target, None)
                    seen.add(target)
            if req.method == "GET" and location in pending:
                returned = Request.body(res.body, res.headers)
                if isinstance(returned, dict):
                    collection = "/".join("{}" if part.isdigit() else part for part in parent.split("/"))
                    for key, value in pending[location].items():
                        if (
                            type(value) in {int, bool, float, str}
                            and type(returned.get(key)) is type(value)
                            and returned[key] == value
                        ):
                            yield collection, key


class ParameterValues:
    """Query values and values captured by path placeholders."""

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Read actual parameter values, including blank query values.

        Yields:
            Operation, location, name and value.
        """
        for request in requests:
            query = urlsplit(request.exchange.request.target).query
            for name, value in parse_qsl(query, keep_blank_values=True):
                yield request.operation, "query", name, value
            for name, value in request.path_values:
                yield request.operation, "path", name, unquote(value)


class RequestValues:
    """Typed scalar values of request body fields."""

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Read values at request leaves.

        Yields:
            Operation, path and JSON value.
        """
        for request in requests:
            for path, value in request.fields:
                if isinstance(value, (bool, int, float, str)):
                    yield request.operation, path, json.dumps(value)


class ResponseFields:
    """Response leaf paths per operation and status."""

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Read the fields present in each response.

        Yields:
            Operation, status and path.
        """
        for request in requests:
            for path, _ in request.response_fields:
                yield request.operation, str(request.exchange.response.status), path


class ResponseValues:
    """Short response values grouped by operation, status and field."""

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Read short scalar values before checking their suite wide counts.

        Yields:
            Operation, status, path and typed JSON value.
        """
        for request in requests:
            for path, value in request.response_fields:
                if isinstance(value, (bool, int, float, str)) and len(str(value)) < 64:
                    yield request.operation, str(request.exchange.response.status), path, json.dumps(value)


class OperationPairs:
    """Consecutive operations within a test."""

    def items(self, requests: list[Request]) -> Iterator[Item]:
        """Read operations in recording order.

        Yields:
            Previous and current operation.
        """
        for previous, current in pairwise(requests):
            yield previous.operation, current.operation


FAMILIES: dict[str, Family] = {
    "commands": Commands(),
    "modes": Modes(),
    "fields": Fields(),
    "readbacks": Readbacks(),
    "parameter_values": ParameterValues(),
    "request_values": RequestValues(),
    "response_fields": ResponseFields(),
    "response_values": ResponseValues(),
    "operation_pairs": OperationPairs(),
}


@dataclass(slots=True)
class TrafficCoverage:
    """Distinct items and the tests that witness each one."""

    witnesses: dict[str, dict[Item, set[JavaTest]]]

    @classmethod
    def collect(
        cls, records: list[RecordedTest], api: ApiDescription, full: CoverageSet, names: tuple[str, ...]
    ) -> Self:
        """Match requests to observed RESTCov paths with literal paths tried first.

        A path that some operation of the request's own method covers wins over a path of another method.

        Returns:
            The witnesses of each enabled family.
        """
        if not names:
            return cls({})
        paths = sorted((i.key for i in full.items if i.criterion == Criterion.PATH), key=lambda p: (p.count("{"), p))
        patterns = [(re.compile(re.sub(r"\\\{[^}]+\\\}", "([^/]+)", re.escape(path))), path) for path in paths]
        operations = {
            (method.upper(), path)
            for method, _, path in (i.key.partition(" ") for i in full.items if i.criterion == Criterion.OPERATION)
        }
        matched: dict[tuple[str, str], tuple[str, tuple[tuple[str, str], ...]]] = {}
        witnesses: dict[str, dict[Item, set[JavaTest]]] = {name: defaultdict(set) for name in names}
        for record in records:
            requests = []
            for exchange in record.exchanges:
                method = exchange.request.method
                path = api.path_of(exchange.request.target).rstrip("/") or "/"
                if (method, path) not in matched:
                    fitting = [(p, m) for pattern, p in patterns if (m := pattern.fullmatch(path))]
                    own = [entry for entry in fitting if (method.upper(), entry[0]) in operations]
                    template, values = "<unmatched>", ()
                    if fitting:
                        template, match = (own or fitting)[0]
                        values = tuple(zip(re.findall(r"\{([^}]+)\}", template), match.groups(), strict=True))
                    matched[method, path] = (f"{method} {template}", values)
                requests.append(Request(exchange, *matched[method, path]))
            for name in names:
                for item in FAMILIES[name].items(requests):
                    witnesses[name][item].add(record.test)
        for name in ("modes", "response_values"):
            if name in witnesses:
                family = witnesses[name]
                witnesses[name] = {item: family[item] for item in Modes.bounded(family)}
        return cls(witnesses)

    def counts(self, tests: list[JavaTest]) -> dict[str, int]:
        """Count distinct covered items in each enabled family.

        Returns:
            Item counts by family.
        """
        selected = set(tests)
        return {
            name: sum(not selected.isdisjoint(group) for group in items.values())
            for name, items in self.witnesses.items()
        }
