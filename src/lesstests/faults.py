"""Describe the single edits a recorded response can receive."""

import gzip
import json
import re
import threading
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from http import HTTPStatus
from typing import Any, Protocol, Self
from urllib.parse import parse_qs, urlsplit

from lesstests.api import ApiDescription
from lesstests.exchange import Body, Exchange, HttpResponse

type FieldPath = tuple[str | None, ...]


@dataclass(frozen=True, slots=True)
class FaultFamily:
    """A response location shared by tests calling the same operation."""

    operation: str
    command: str | None
    status: int
    path: FieldPath
    change: str

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Self:
        """Read one family from the saved inventory.

        Returns:
            The fault to arm at the proxy.
        """
        return cls(row["operation"], row["command"], row["status"], tuple(row["path"]), row["change"])


@dataclass(slots=True)
class Location:
    """One value and the concrete path to it inside a JSON document."""

    value: Any
    path: FieldPath
    parent: Any
    key: int | str

    def walk(self) -> Iterator["Location"]:
        """Visit the parent before its children.

        Yields:
            Each location in document order.
        """
        yield self
        if isinstance(self.value, dict):
            for key, child in self.value.items():
                yield from Location(child, (*self.path, key), self.value, key).walk()
        elif isinstance(self.value, list):
            for index, child in enumerate(self.value):
                yield from Location(child, (*self.path, None), self.value, index).walk()

    @property
    def identifier(self) -> bool:
        """Check the field name and type.

        Returns:
            Whether this field takes an identifier edit.
        """
        return (
            isinstance(self.key, str)
            and (self.key.lower() == "id" or self.key.endswith("Id"))
            and type(self.value) in (int, str)
        )


class ResponseChange(Protocol):
    """A change that recognises its locations and edits one response."""

    def kind(self, location: Location | None) -> str | None:
        """Identify the edit at this location.

        Returns:
            The family kind when eligible.
        """
        ...

    def apply(self, response: HttpResponse, location: Location | None, root: list[Any]) -> HttpResponse:
        """Apply one eligible edit.

        Returns:
            The changed response.
        """
        ...


class StatusChange:
    """Change just the status line, including for empty or binary bodies."""

    def kind(self, location: Location | None) -> str | None:
        """Check for the response itself.

        Returns:
            The status kind for the response itself.
        """
        return "status" if location is None else None

    def apply(self, response: HttpResponse, location: Location | None, root: list[Any]) -> HttpResponse:
        """Keep the body bytes as received.

        Returns:
            The response with a different status line.
        """
        status = HTTPStatus.BAD_REQUEST if 200 <= response.status < 300 else HTTPStatus.OK
        return replace(response, status=status.value, reason=status.phrase)


class BodyChange:
    """Write the changed JSON without stale lengths, encodings or validators."""

    def apply(self, response: HttpResponse, location: Location | None, root: list[Any]) -> HttpResponse:
        """Write the edited document.

        Returns:
            The edited JSON as UTF8 bytes.
        """
        assert location is not None
        self.edit(location)
        data = json.dumps(root[0], separators=(",", ":"), allow_nan=False).encode()
        headers = response.headers.without(
            "content-encoding", "content-length", "etag", "content-md5", "digest", "repr-digest", "content-digest"
        )
        return replace(response, body=Body(data), headers=headers)

    def edit(self, location: Location) -> None:
        """Edit one location in place.

        Raises:
            NotImplementedError: When the kind has no edit.
        """
        raise NotImplementedError


class WrongValue(BodyChange):
    """Change a scalar while keeping its JSON type."""

    def kind(self, location: Location | None) -> str | None:
        """Choose the edit for this scalar.

        Returns:
            The scalar kind if the replacement differs.
        """
        if location is None or location.identifier:
            return None
        value = location.value
        if isinstance(value, bool):
            return "wrong value.boolean"
        if isinstance(value, str):
            return "wrong value.string"
        if isinstance(value, (float, int)) and value + 1 != value:
            return "wrong value.number"
        return None

    def edit(self, location: Location) -> None:
        """Replace the scalar in its parent."""
        value = location.value
        if isinstance(value, bool):
            changed = not value
        elif isinstance(value, str):
            changed = "changed" if value != "changed" else "other"
        else:
            changed = value + 1
        location.parent[location.key] = changed


class WrongId(BodyChange):
    """Change an identifier while preserving the shape of common string ids."""

    def kind(self, location: Location | None) -> str | None:
        """Recognise identifier fields.

        Returns:
            The identifier kind for integer and string id fields.
        """
        return "wrong id" if location is not None and location.identifier else None

    def edit(self, location: Location) -> None:
        """Change one character of a string id or increment an integer id."""
        value = location.value
        if isinstance(value, str):
            match = re.search(r"[0-9a-zA-Z]", value[::-1])
            if match:
                index = len(value) - match.start() - 1
                char = value[index]
                alphabet = "01" if char.isdigit() else "AB" if char.isupper() else "ab"
                replacement = alphabet[1] if char == alphabet[0] else alphabet[0]
                value = value[:index] + replacement + value[index + 1 :]
            else:
                value += "1"
        else:
            value += 1
        location.parent[location.key] = value


class MissingField(BodyChange):
    """Remove a named member, including one whose value is null."""

    def kind(self, location: Location | None) -> str | None:
        """Recognise named members.

        Returns:
            The missing field kind for object members.
        """
        return "missing field" if location is not None and isinstance(location.key, str) else None

    def edit(self, location: Location) -> None:
        """Remove the member from its object."""
        del location.parent[location.key]


class EmptyList(BodyChange):
    """Empty a list that has at least one item."""

    def kind(self, location: Location | None) -> str | None:
        """Recognise populated arrays.

        Returns:
            The empty list kind for a populated array.
        """
        return "empty list" if location is not None and isinstance(location.value, list) and location.value else None

    def edit(self, location: Location) -> None:
        """Remove the list's items."""
        location.value.clear()


class ResponseFaults:
    """Find eligible edits using the same rules offline and at the proxy."""

    BODY_CHANGES = (MissingField(), EmptyList(), WrongId(), WrongValue())

    @staticmethod
    def _number(token: str) -> float:
        """Read a JSON decimal.

        Returns:
            A number that survives JSON serialisation.

        Raises:
            ValueError: If rewriting it would change another field.
        """
        value = float(token)
        if not Decimal(str(value)).is_finite() or Decimal(str(value)) != Decimal(token):
            raise ValueError("Cannot preserve this number")
        return value

    @staticmethod
    def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Keep each named member.

        Returns:
            An object that can be written back with every field.

        Raises:
            ValueError: If two members have the same name.
        """
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("Repeated JSON member")
        return value

    @classmethod
    def locations(cls, exchange: Exchange) -> Iterator[tuple[ResponseChange, Location | None, list[Any]]]:
        """Find the status and each editable body location.

        Yields:
            The edit, its location and the parsed root.
        """
        yield StatusChange(), None, []
        response = exchange.response
        encoding = (response.headers.get("content-encoding") or "identity").lower().strip()
        if exchange.request.method == "HEAD" or encoding not in ("gzip", "identity"):
            return
        try:
            data = gzip.decompress(response.body.data) if encoding == "gzip" else response.body.data
            value = json.loads(data, parse_float=cls._number, parse_constant=cls._number, object_pairs_hook=cls._object)
        except (OSError, EOFError, zlib.error, ValueError, RecursionError):
            return
        root = [value]
        for location in Location(value, (), root, 0).walk():
            for change in cls.BODY_CHANGES:
                yield change, location, root

    @classmethod
    def candidates(
        cls, operation: str, exchange: Exchange
    ) -> Iterator[tuple[FaultFamily, ResponseChange, Location | None, list[Any]]]:
        """Group concrete locations by operation and folded path.

        Yields:
            Families with the edits that produce them.
        """
        query = parse_qs(urlsplit(exchange.request.target).query, keep_blank_values=True)
        command = query.get("command", [None])[0]
        for change, location, root in cls.locations(exchange):
            kind = change.kind(location)
            if kind is not None:
                family = FaultFamily(
                    operation, command, exchange.response.status, location.path if location else (), kind
                )
                yield family, change, location, root


class ProxyFault:
    """Apply one family once across the proxy's concurrent handlers."""

    def __init__(self, api: ApiDescription, family: FaultFamily) -> None:
        """Arm the proxy with a family from the inventory."""
        self.api = api
        self.family = family
        self.applied = False
        self._lock = threading.Lock()

    def apply(self, exchange: Exchange) -> HttpResponse:
        """Try the armed family.

        Returns:
            The changed response on the first match or the upstream response.
        """
        with self._lock:
            if not self.applied:
                operation = self.api.match(exchange.request.method, exchange.request.target)
                if operation is not None and operation.name == self.family.operation:
                    for family, change, location, root in ResponseFaults.candidates(operation.name, exchange):
                        if family == self.family:
                            changed = change.apply(exchange.response, location, root)
                            if changed.status != exchange.response.status or changed.body != exchange.response.body:
                                self.applied = True
                                return changed
        return exchange.response
