"""Code coverage of recordings, plans and benchmark runs, counted from the JaCoCo dumps next to them."""

import json
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from lesstests.jacoco import CodeCheck, CodeCounter, CodeKept, JaCoCo, KeptPair, ServerCode
from lesstests.java import JavaTest
from lesstests.recording import Recording
from lesstests.versions import VersionPlan


class RecordingCode:
    """The code coverage of a recording, and of merged copies recorded next to it.

    The class files and the idle dump come from the first recording.
    """

    def __init__(self, recordings: list[Recording], jacoco: JaCoCo, code: ServerCode) -> None:
        """Keep the recordings, the first one being the original suite, which must have its class files."""
        self._recordings = recordings
        self._jacoco = jacoco
        self._code = code
        original = recordings[0]
        idle = original.idle_file if original.idle_file.exists() else None
        self._counter = CodeCounter(jacoco, self._locations(original, code), code.prefix, idle)

    @staticmethod
    def _locations(recording: Recording, code: ServerCode) -> list[Path]:
        """Find the class files a finished recording copied out of the image.

        Returns:
            The class folders and jars.

        Raises:
            ValueError: If there are none, since the copy only happens once the record run finished.
        """
        locations = code.locations(recording.classes_folder)
        if not locations:
            raise ValueError(f"{recording.folder} has no class files yet. Run record again so it finishes")
        return locations

    @classmethod
    def of(cls, recording: Recording, jacoco: JaCoCo | None, code: ServerCode | None) -> Self | None:
        """Build it for a recording made with code coverage.

        Returns:
            The code coverage, or None when the recording has none.

        Raises:
            ValueError: If the recording has code coverage but no JaCoCo was given, or no class files yet.
        """
        if not recording.has_code_coverage:
            return None
        if jacoco is None or code is None:
            raise ValueError(f"{recording.folder} has code coverage. Give --jacoco or set LESSTESTS_JACOCO")
        return cls([recording], jacoco, code)

    @property
    def jacoco(self) -> JaCoCo:
        """The JaCoCo the dumps get counted with."""
        return self._jacoco

    def with_merged(self, merged: Recording) -> Self:
        """Also look up dumps in the recording of the kept merged copies.

        Returns:
            A new code coverage over both recordings.
        """
        return type(self)([*self._recordings, merged], self._jacoco, self._code)

    def exec_of(self, test: JavaTest) -> Path:
        """Find the dump of a test in any of the recordings.

        Returns:
            The file path.

        Raises:
            FileNotFoundError: If no recording has one.
        """
        for recording in self._recordings:
            path = recording.exec_of(test)
            if path.exists():
                return path
        raise FileNotFoundError(f"No code coverage dump for {test.id}")

    def files(self, tests: Iterable[JavaTest]) -> tuple[Path, ...]:
        """Find the dumps of some tests.

        Returns:
            The file paths.
        """
        return tuple(self.exec_of(test) for test in tests)

    def kept(
        self, versions: dict[str, list[JavaTest]], reference: list[JavaTest], listed: frozenset[str] = frozenset()
    ) -> dict[str, CodeKept]:
        """Count what each version keeps of the reference. The versions in `listed` also get their lost methods.

        Returns:
            The kept coverage by version name.
        """
        base = self.files(reference)
        pairs = [KeptPair(name, self.files(tests), base, name in listed) for name, tests in versions.items()]
        return self._counter.kept(pairs)

    def check(self) -> CodeCheck:
        """Check the helper against JaCoCo's own report on all dumps of the first recording.

        The result is saved, and checked again once a dump is newer, such as after a resumed recording.

        Returns:
            The covered counts of all tests without the idle mask, and the classes that ran without a class file.
        """
        recording = self._recordings[0]
        saved = recording.code_folder / "check.json"
        dumps = sorted((recording.code_folder / "exec").glob("*.exec"))
        newest = max((path.stat().st_mtime for path in dumps), default=0.0)
        if saved.exists() and saved.stat().st_mtime >= newest:
            return CodeCheck.from_dict(json.loads(saved.read_text(encoding="utf-8")))
        checked = self._counter.check(dumps, recording.code_folder / "check" / "report.xml")
        saved.write_text(json.dumps(checked.to_dict(), indent=1), encoding="utf-8")
        return checked

    def annotate(self, plan: VersionPlan) -> None:
        """Give every version of a plan the code it keeps of the Original."""
        versions = {version.name: version.tests for version in plan.versions}
        kept = self.kept(versions, plan.original.tests)
        for version in plan.versions:
            version.code = kept[version.name]

    def recorded(self) -> tuple[Path, ...]:
        """Find the dumps of every test of the original recording, which reads every record.

        Returns:
            The file paths.
        """
        return self.files(record.test for record in self._recordings[0].records())

    def pairs[K: Hashable](self, pairs: list[KeptPair[K]]) -> dict[K, CodeKept]:
        """Count any pairs of dumps with this recording's classes and idle dump, such as benchmark runs.

        Returns:
            The kept coverage by pair name.
        """
        return self._counter.kept(pairs)


@dataclass(slots=True)
class BenchCode:
    """The code each benchmark run kept of the Original run of its round, and how far Original is from the recording.

    `runs` maps a version and a round to its kept coverage. `recording` maps a round to what its Original run
    reached of what the recording reached.
    """

    runs: dict[tuple[str, int], CodeKept]
    recording: dict[int, CodeKept]

    @classmethod
    def count(cls, code: RecordingCode, dumps: dict[tuple[str, int], Path], original: str) -> Self:
        """Count every run against the Original run of its round.

        Returns:
            The kept coverage of every run.
        """
        runs = code.pairs([KeptPair(key, (path,), (dumps[original, key[1]],)) for key, path in dumps.items()])
        recorded = code.recorded()
        originals = [
            KeptPair(number, (path,), recorded) for (version, number), path in dumps.items() if version == original
        ]
        return cls(runs, code.pairs(originals))

    def to_dict(self) -> dict[str, Any]:
        """Turn it into JSON friendly data.

        Returns:
            The kept coverage per run and per round.
        """
        return {
            "runs": [
                {"version": version, "round": number, **kept.to_dict()}
                for (version, number), kept in sorted(self.runs.items())
            ],
            "recording": {str(number): kept.to_dict() for number, kept in sorted(self.recording.items())},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read it back.

        Returns:
            The kept coverage.
        """
        runs = {(run["version"], run["round"]): CodeKept.from_dict(run) for run in raw["runs"]}
        return cls(runs, {int(number): CodeKept.from_dict(kept) for number, kept in raw["recording"].items()})
