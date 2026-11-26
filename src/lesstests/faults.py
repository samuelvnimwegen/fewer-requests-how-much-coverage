"""Describe the single edits a recorded response can receive."""

import gzip
import json
import re
import threading
import zlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from http import HTTPStatus
from typing import Any, Self
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

    @property
    def kind(self) -> str:
        """Group scalar variants for the report.

        Returns:
            The edit kind across scalar types.
        """
        return "wrong value" if self.change in WrongValue.VARIANTS.values() else self.change

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Self:
        """Read one family from the saved inventory.

        Returns:
            The fault to arm at the proxy.
        """
        return cls(row["operation"], row["command"], row["status"], tuple(row["path"]), row["change"])


@dataclass(slots=True)
class Location:
    """A value, its concrete path and its spans in the decoded JSON text."""

    value: Any
    path: tuple[str | int, ...]
    start: int
    end: int
    member_start: int
    member_end: int

    @property
    def identifier(self) -> bool:
        """Check the field name and type.

        Returns:
            Whether this field takes an identifier edit.
        """
        return (
            bool(self.path)
            and isinstance(self.path[-1], str)
            and (self.path[-1].lower() == "id" or self.path[-1].endswith("Id"))
            and type(self.value) in (int, str)
        )


@dataclass(frozen=True, slots=True)
class Edit:
    """The concrete path and exact JSON tokens, with None for a removed member."""

    path: tuple[str | int, ...]
    original: str
    replacement: str | None


@dataclass(frozen=True, slots=True)
class FaultHit:
    """The upstream exchange and all edits made to its response."""

    exchange: Exchange
    edits: tuple[Edit, ...]


class BodyChange(ABC):
    """Recognise eligible values and choose their replacement."""

    @abstractmethod
    def kind(self, location: Location) -> str | None:
        """Check eligibility.

        Returns:
            The family name if the value is eligible.
        """

    @abstractmethod
    def edit(self, location: Location) -> Any:
        """Choose the new value.

        Returns:
            The replacement or None to remove the named member.
        """


class WrongValue(BodyChange):
    """Change a scalar while keeping its JSON type."""

    VARIANTS = {
        bool: "wrong value.boolean",
        str: "wrong value.string",
        int: "wrong value.number",
        float: "wrong value.number",
    }

    def kind(self, location: Location) -> str | None:
        """Choose the edit for this scalar.

        Returns:
            The scalar kind if the replacement differs.
        """
        if location.identifier:
            return None
        value = location.value
        if type(value) in (int, float) and value + 1 == value:
            return None
        return self.VARIANTS.get(type(value))

    def edit(self, location: Location) -> Any:
        """Choose a replacement for this scalar.

        Returns:
            A different scalar of the same type.
        """
        value = location.value
        if isinstance(value, bool):
            changed = not value
        elif isinstance(value, str):
            changed = "changed" if value != "changed" else "other"
        else:
            changed = value + 1
        return changed


class WrongId(BodyChange):
    """Change an identifier while preserving the shape of common string ids."""

    def kind(self, location: Location) -> str | None:
        """Recognise identifier fields.

        Returns:
            The identifier kind for integer and string id fields.
        """
        return "wrong id" if location.identifier else None

    def edit(self, location: Location) -> Any:
        """Change one character of a string id or increment an integer id.

        Returns:
            The changed identifier.
        """
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
        return value


class MissingField(BodyChange):
    """Remove a named member, including one whose value is null."""

    def kind(self, location: Location) -> str | None:
        """Recognise named members.

        Returns:
            The missing field kind for object members.
        """
        return "missing field" if location.path and isinstance(location.path[-1], str) else None

    def edit(self, location: Location) -> Any:
        """Remove the member from its object.

        Returns:
            None to remove this member.
        """
        return None


class EmptyList(BodyChange):
    """Empty a list that has at least one item."""

    def kind(self, location: Location) -> str | None:
        """Recognise populated arrays.

        Returns:
            The empty list kind for a populated array.
        """
        return "empty list" if isinstance(location.value, list) and location.value else None

    def edit(self, location: Location) -> Any:
        """Remove the list's items.

        Returns:
            An empty list.
        """
        return []


@dataclass(slots=True)
class JsonDocument:
    """The original text and its values in document order."""

    text: str
    encoding: str
    locations: list[Location]

    @staticmethod
    def _number(token: str) -> float:
        """Read a JSON decimal.

        Returns:
            A number that survives JSON serialisation.

        Raises:
            ValueError: If the token has no exact decimal round trip through float.
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
    def read(cls, exchange: Exchange) -> Self | None:
        """Validate the whole document before offering a body edit.

        Returns:
            The indexed document if the response supports body edits.
        """
        response = exchange.response
        encoding = (response.headers.get("content-encoding") or "identity").lower().strip()
        if exchange.request.method == "HEAD" or encoding not in ("gzip", "identity"):
            return None
        try:
            data = gzip.decompress(response.body.data) if encoding == "gzip" else response.body.data
            charset = json.detect_encoding(data)

            # Keep the byte order mark in the text so writing it preserves the original byte order.
            if charset in ("utf-16", "utf-32"):
                charset += "-le" if data.startswith(b"\xff\xfe") else "-be"
            elif charset == "utf-8-sig":
                charset = "utf-8"
            text = data.decode(charset, errors="surrogatepass")
            value = json.loads(
                text.removeprefix("\ufeff"),
                parse_float=cls._number,
                parse_constant=cls._number,
                object_pairs_hook=cls._object,
            )
            document = cls(text, charset, [])
            tokens = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\],:]|[^\s{}\[\],:"]+').finditer(
                text, int(text.startswith("\ufeff"))
            )
            document._walk(value, (), tokens)
            return document
        except (OSError, EOFError, zlib.error, ValueError, RecursionError):
            return None

    def _walk(self, value: Any, path: tuple[str | int, ...], tokens: Iterator[re.Match[str]]) -> Location:
        """Pair validated values with the tokens that spell them.

        Returns:
            The indexed location including its closing delimiter.
        """
        token = next(tokens)
        node = Location(value, path, token.start(), token.end(), token.start(), token.end())
        self.locations.append(node)
        if isinstance(value, (dict, list)):
            comma = None
            for key, child in value.items() if isinstance(value, dict) else enumerate(value):
                member = next(tokens).start() if isinstance(key, str) else None
                if member is not None:
                    next(tokens)
                item = self._walk(child, (*path, key), tokens)
                delimiter = next(tokens)
                if member is not None:
                    item.member_start = comma if comma is not None else member
                    item.member_end = item.end if comma is not None or delimiter[0] == "}" else delimiter.end()
                comma = delimiter.start()
                node.end = delimiter.end()
            if not value:
                node.end = next(tokens).end()
        return node

    def rewrite(self, response: HttpResponse, matches: list["Candidate"]) -> tuple[HttpResponse, tuple[Edit, ...]]:
        """Copy the original text between the matching locations.

        Returns:
            The changed response and each edit in document order.
        """
        parts: list[str] = []
        edits: list[Edit] = []
        cursor = 0
        for candidate in matches:
            location = candidate.location
            replacement = candidate.change.edit(location)
            token = json.dumps(replacement) if replacement is not None else None
            edits.append(Edit(location.path, self.text[location.start : location.end], token))
            start, end = (
                (location.start, location.end) if token is not None else (location.member_start, location.member_end)
            )
            parts.extend((self.text[cursor:start], token or ""))
            cursor = end
        parts.append(self.text[cursor:])
        headers = response.headers.without(
            "content-encoding", "content-length", "etag", "content-md5", "digest", "repr-digest", "content-digest"
        )
        return replace(
            response, body=Body("".join(parts).encode(self.encoding, errors="surrogatepass")), headers=headers
        ), tuple(edits)


@dataclass(slots=True)
class Candidate:
    """A body edit at one concrete location."""

    family: FaultFamily
    location: Location
    change: BodyChange


class ResponseFaults:
    """Find eligible edits using the same rules offline and at the proxy."""

    BODY_CHANGES = (MissingField(), EmptyList(), WrongId(), WrongValue())

    def __init__(self, operation: str, exchange: Exchange) -> None:
        """Keep the request context shared by the status and body families."""
        query = parse_qs(urlsplit(exchange.request.target).query, keep_blank_values=True)
        self.status = FaultFamily(operation, query.get("command", [None])[0], exchange.response.status, (), "status")
        self.exchange = exchange

    def candidates(self, document: JsonDocument) -> Iterator[Candidate]:
        """Group concrete locations by operation and folded path.

        Yields:
            Each eligible body edit.
        """
        for location in document.locations:
            path = tuple(None if isinstance(key, int) else key for key in location.path)
            for change in self.BODY_CHANGES:
                kind = change.kind(location)
                if kind:
                    yield Candidate(replace(self.status, path=path, change=kind), location, change)

    def families(self) -> Iterator[FaultFamily]:
        """List the status and body families this response exposes.

        Yields:
            Each family in document order.
        """
        yield self.status
        if document := JsonDocument.read(self.exchange):
            for candidate in self.candidates(document):
                yield candidate.family


class ProxyFault:
    """Apply one family once across the proxy's concurrent handlers."""

    def __init__(self, api: ApiDescription, family: FaultFamily) -> None:
        """Arm the proxy with a family from the inventory."""
        self._api = api
        self._family = family
        self._hit: FaultHit | None = None
        self._lock = threading.Lock()

    @property
    def hit(self) -> FaultHit | None:
        """Read the recorded hit.

        Returns:
            The original exchange and its changes after a match.
        """
        return self._hit

    @property
    def applied(self) -> bool:
        """Check the fault state.

        Returns:
            Whether a response received the armed fault.
        """
        return self._hit is not None

    def apply(self, exchange: Exchange) -> HttpResponse:
        """Try the armed family.

        Returns:
            The changed response on the first match or the upstream response.
        """
        response = exchange.response
        with self._lock:
            if self.applied:
                return response
            operation = self._api.match(exchange.request.method, exchange.request.target)
            if operation is None or operation.name != self._family.operation:
                return response
            faults = ResponseFaults(operation.name, exchange)
            if faults.status == self._family:
                status = HTTPStatus.BAD_REQUEST if 200 <= response.status < 300 else HTTPStatus.OK
                self._hit = FaultHit(exchange, (Edit((), str(response.status), str(status.value)),))
                return replace(response, status=status.value, reason=status.phrase)
            document = JsonDocument.read(exchange)
            if document and (matches := [c for c in faults.candidates(document) if c.family == self._family]):
                changed, edits = document.rewrite(response, matches)
                self._hit = FaultHit(exchange, edits)
                return changed
        return response
