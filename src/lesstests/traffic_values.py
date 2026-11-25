import gzip
import json
import math
import re
import zlib
from collections import defaultdict
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit

from lesstests.exchange import Body, Headers, HttpRequest

type Scalar = str | int | float | bool | None
type Item = tuple[str, ...]


class Traffic:
    """Read fields and recognise changing values without knowing the API's parameter names."""

    integer = re.compile(r"-?\d+$")
    decimal = re.compile(r"-?\d+\.\d+$")
    uuid = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
    mixed = re.compile(r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_-]{6,}$")
    date = re.compile(
        r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)?"
        r"|(?i:\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b)",
    )

    @classmethod
    def operation(cls, request: HttpRequest, prefix: str) -> tuple[str, list[tuple[str, str]]]:
        """Group numeric IDs, UUIDs and long generated path segments.

        Returns:
            The operation and query fields, including empty fields.
        """
        url = urlsplit(request.target)
        parts = []
        for part in unquote(url.path).removeprefix(prefix).split("/"):
            if cls.integer.match(part):
                part = "{id}"
            elif cls.uuid.match(part):
                part = "{uuid}"
            elif len(part) >= 10 and cls.mixed.match(part):
                part = "{tok}"
            if part:
                parts.append(part)
        return f"{request.method} /{'/'.join(parts)}", sorted(parse_qsl(url.query, keep_blank_values=True))

    @classmethod
    def fields(cls, body: Body, headers: Headers) -> Iterator[tuple[str, Scalar]]:
        """Read JSON leaves from a recorded body.

        Yields:
            Field paths and scalar values.
        """
        raw = body.data
        if raw.startswith(b"\x1f\x8b") or headers.get("content-encoding") == "gzip":
            with suppress(OSError, EOFError, zlib.error):
                raw = gzip.decompress(raw)
        if raw:
            try:
                yield from cls._leaves(json.loads(raw), "")
            except (ValueError, RecursionError):
                return

    @classmethod
    def _leaves(cls, value: object, path: str) -> Iterator[tuple[str, Scalar]]:
        """Ignore array indices so repeated shapes reach the same field.

        Yields:
            Each leaf path and value.
        """
        if isinstance(value, list):
            if not value:
                yield path, "<EMPTY_LIST>"
            for entry in value:
                yield from cls._leaves(entry, path + "[]")
        elif isinstance(value, dict):
            if not value and path:
                yield path, "<EMPTY_OBJ>"
            for key, entry in value.items():
                yield from cls._leaves(entry, f"{path}/{key}")
        elif isinstance(value, (str, int, float)) or value is None:
            yield path, value

    @classmethod
    def token(cls, value: Scalar) -> str:
        """Group volatile scalar values for traffic vectors.

        Returns:
            The token value.
        """
        if isinstance(value, bool) or value is None:
            return str(value)
        if isinstance(value, str):
            value = value.strip()
            if not value or value.lower() in ("true", "false"):
                return value.lower()
            if cls.integer.fullmatch(value):
                number = float(value)
                if abs(number) <= 20:
                    return str(int(number))
                return "<NEG>" if number < 0 else "<NUM>"
            if cls.decimal.fullmatch(value):
                return "<NEG>" if float(value) < 0 else "<FLOAT>"
            if cls.uuid.fullmatch(value):
                return "<UUID>"
            if cls.date.fullmatch(value):
                return "<DATE>"
            if cls.date.search(value):
                return "<STR_DATE>"
            if cls.mixed.fullmatch(value):
                return "<RAND>"
            if len(value) <= 32 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_. -]*", value):
                return value
            return "<STR>"
        if value in (-1, 0, 1) or isinstance(value, int) and abs(value) <= 20:
            return str(value)
        return "<NEG>" if value < 0 else "<NUM>"


@dataclass(slots=True)
class FieldValues:
    """Group each distinct field value by the tests that sent it."""

    tests: set[int] = field(default_factory=set)
    numbers: dict[float, set[int]] = field(default_factory=lambda: defaultdict(set))
    strings: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))

    def observe(self, test: int, value: Scalar) -> Item:
        """Record a value for later comparison across tests.

        Returns:
            Its immediate boundary item, if present.
        """
        self.tests.add(test)
        if value is None or isinstance(value, bool):
            return ("literal", str(value))
        if isinstance(value, str):
            if value in ("", "<EMPTY_LIST>", "<EMPTY_OBJ>"):
                return ("empty", value)
            if value.lower() in ("true", "false"):
                return ("boolean string", value.lower())
            if not (Traffic.integer.fullmatch(value.strip()) or Traffic.decimal.fullmatch(value.strip())):
                self.strings[value].add(test)
                return ()
        try:
            num = float(value)
        except OverflowError:
            return ()
        if math.isfinite(num):
            self.numbers[num].add(test)
        return ()

    def boundaries(self) -> Iterator[tuple[Item, set[int]]]:
        """Find the numeric boundaries, unusual lengths and repeating modes of this field.

        Yields:
            Each item and the tests that showed it.
        """
        modes: dict[Item, set[int]] = {}
        for number, tests in self.numbers.items():
            modes["number", str(number or 0.0)] = tests
            if number <= 0:
                yield ("negative" if number < 0 else "zero",), tests
            if not number.is_integer():
                yield ("fraction",), tests
        if len(self.numbers) >= 3:
            yield ("minimum",), self.numbers[min(self.numbers)]
            yield ("maximum",), self.numbers[max(self.numbers)]

        lengths = sorted(len(value) for value, tests in self.strings.items() for _ in tests)
        compare_lengths = len(lengths) >= 5 and len(set(lengths)) >= 3
        for value, tests in self.strings.items():
            if len(value) <= 48 and not (
                Traffic.uuid.match(value) or Traffic.mixed.match(value) or Traffic.date.search(value)
            ):
                modes["string", value] = tests
            if compare_lengths:
                median = lengths[len(lengths) // 2]
                if len(value) == lengths[0] and len(value) < median / 2:
                    yield ("short",), tests
                if len(value) >= lengths[int(0.95 * (len(lengths) - 1))] and len(value) > 1.5 * median:
                    yield ("long",), tests

        count = len(modes)
        observations = sum(map(len, modes.values()))
        if 2 <= count <= 16 and observations >= 3 and count <= 0.65 * observations:
            yield from modes.items()
        elif 2 <= count <= 40 and observations >= 6 and count <= 0.5 * observations:
            yield from (
                (item, tests) for item, tests in modes.items() if len(tests) <= max(1, int(0.15 * len(self.tests)))
            )
