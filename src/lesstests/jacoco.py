"""Server code coverage with JaCoCo: the agent in the server container, its dumps, and counting groups of dumps."""

import fnmatch
import subprocess
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Protocol, Self

from lesstests.service import ContainerSpec


@dataclass(frozen=True, slots=True)
class JaCoCo:
    """The `lib` folder of a JaCoCo release, with `jacocoagent.jar` and `jacococli.jar`."""

    folder: Path

    HELPER: ClassVar[Path] = Path(__file__).parent / "support" / "LesstestsCoverage.java"

    @classmethod
    def at(cls, folder: Path) -> Self:
        """Point at the folder.

        Returns:
            JaCoCo in that folder.

        Raises:
            ValueError: If a jar is missing.
        """
        jacoco = cls(folder.resolve())
        missing = [path.name for path in (jacoco.agent, jacoco.cli) if not path.is_file()]
        if missing:
            raise ValueError(f"{folder} has no {' or '.join(missing)}. Point at the lib folder of a JaCoCo release")
        return jacoco

    @property
    def agent(self) -> Path:
        """The agent the server JVM loads."""
        return self.folder / "jacocoagent.jar"

    @property
    def cli(self) -> Path:
        """The command line tool, which also holds the JaCoCo classes the helper uses."""
        return self.folder / "jacococli.jar"

    def dump(self, port: int, path: Path) -> None:
        """Save the agent's counters on this machine's `port` to `path`, and set them back to zero.

        `jacococli` adds a session to a file that already exists, so an older dump of the same test goes first.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        address = ["--address", "127.0.0.1", "--port", str(port)]
        self._cli("dump", *address, "--destfile", str(path), "--reset", "--retry", "5", "--quiet")

    def report(self, files: list[Path], classes: list[Path], xml: Path) -> ElementTree.Element:
        """Merge the files and let JaCoCo's own report count them, the reference for the helper.

        Returns:
            The root of the XML report.
        """
        merged = xml.with_suffix(".exec")
        merged.unlink(missing_ok=True)
        self._cli("merge", *map(str, files), "--destfile", str(merged), "--quiet")
        locations = [arg for location in classes for arg in ("--classfiles", str(location))]
        self._cli("report", str(merged), *locations, "--xml", str(xml), "--quiet")
        return ElementTree.parse(xml).getroot()

    def count(self, job: Path, out: Path) -> None:
        """Run the helper on a job file.

        Raises:
            RuntimeError: If the helper fails.
        """
        command = ["java", "-cp", str(self.cli), str(self.HELPER), "count", str(job), str(out)]
        result = subprocess.run(command, capture_output=True, text=True, errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"The coverage helper failed on {job}:\n{result.stderr[-4000:]}")

    def _cli(self, *args: str) -> None:
        result = subprocess.run(["java", "-jar", str(self.cli), *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"jacococli {args[0]} failed:\n{result.stdout[-2000:]}{result.stderr[-2000:]}")


class ServerCoverage(Protocol):
    """The code coverage counters of the running server, or nothing for a run without them."""

    @property
    def on(self) -> bool:
        """Whether the server runs with the agent."""
        ...

    def clear(self) -> None:
        """Set the counters back to zero, such as after the server started."""
        ...

    def dump(self, path: Path) -> None:
        """Save the counters to `path` and set them back to zero."""
        ...

    def idle(self, path: Path) -> None:
        """Save what the server covers on its own, with no test running, when the subject needs that."""
        ...

    def classes(self, target: Path) -> None:
        """Copy the server's class files out of its image, once."""
        ...

    def facts(self) -> dict[str, bool]:
        """Say in the recording's facts that the server ran with the agent, so a resume can't mix the two.

        Returns:
            The facts, empty without the agent, so older recordings still resume.
        """
        ...


class NoServerCoverage:
    """A server without the agent. Every call does nothing."""

    @property
    def on(self) -> bool:
        """No agent."""
        return False

    def clear(self) -> None:
        """Do nothing."""

    def dump(self, path: Path) -> None:
        """Do nothing."""

    def idle(self, path: Path) -> None:
        """Do nothing."""

    def classes(self, target: Path) -> None:
        """Do nothing."""

    def facts(self) -> dict[str, bool]:
        """No agent, so nothing to store.

        Returns:
            No facts.
        """
        return {}


@dataclass(frozen=True, slots=True)
class ServerCode:
    """What JaCoCo measures of a subject's server: its package, where its classes are, and how to reach the agent.

    `classes` are paths inside the image, folders or jars, and may hold a `*`. `idle_seconds` is how long a server
    runs alone so the code its scheduled jobs run can be left out.
    """

    package: str
    classes: tuple[str, ...]
    port: int
    idle_seconds: float = 0.0

    AGENT_PATH: ClassVar[str] = "/lesstests/jacocoagent.jar"
    AGENT_PORT: ClassVar[int] = 6300

    @property
    def prefix(self) -> str:
        """The package as the start of a class file name, like `org/apache/fineract/`."""
        return self.package.replace(".", "/") + "/"

    def container(self, spec: ContainerSpec, jacoco: JaCoCo) -> ContainerSpec:
        """Add the agent to a container without changing its image.

        The flag joins any `JAVA_TOOL_OPTIONS` the container already sets, like Fineract's JVM flags.

        Returns:
            The container with the agent.
        """
        agent = (
            f"-javaagent:{self.AGENT_PATH}=output=tcpserver,address=*,port={self.AGENT_PORT},includes={self.package}.*"
        )
        options = " ".join(filter(None, [spec.env.get("JAVA_TOOL_OPTIONS"), agent]))
        mount = ("-v", f"{jacoco.agent}:{self.AGENT_PATH}:ro", "-p", f"127.0.0.1:{self.port}:{self.AGENT_PORT}")
        return replace(spec, env={**spec.env, "JAVA_TOOL_OPTIONS": options}, docker_args=(*spec.docker_args, *mount))

    def agent(self, jacoco: JaCoCo, image: str) -> "AgentCoverage":
        """Reach the agent of a server that runs `image`.

        Returns:
            The counters of that server.
        """
        return AgentCoverage(self, jacoco, image)

    def locations(self, target: Path) -> list[Path]:
        """Find the class folders and jars that `ServerClasses` copied into `target`.

        Returns:
            The paths, in a fixed order.
        """
        return sorted(path for pattern in self.classes for path in target.glob(pattern.lstrip("/")))

    def wanted(self, member: str) -> bool:
        """Say whether a path of the image is a class location or sits inside one.

        Returns:
            True when it does.
        """
        return any(
            fnmatch.fnmatchcase(member, pattern) or member.startswith(pattern + "/")
            for pattern in (pattern.lstrip("/") for pattern in self.classes)
        )


@dataclass(slots=True)
class AgentCoverage:
    """The agent of one running server, reached over its TCP port."""

    code: ServerCode
    jacoco: JaCoCo
    image: str
    sleep: Callable[[float], None] = field(default=time.sleep)

    @property
    def on(self) -> bool:
        """The agent runs."""
        return True

    def clear(self) -> None:
        """Dump to a file that gets thrown away, since a dump is the only way the agent resets."""
        with tempfile.TemporaryDirectory() as folder:
            self.jacoco.dump(self.code.port, Path(folder) / "cleared.exec")

    def dump(self, path: Path) -> None:
        """Save the counters and set them back to zero."""
        self.jacoco.dump(self.code.port, path)

    def idle(self, path: Path) -> None:
        """Let the server run alone for the subject's idle time and save what it covered. Skip it without one."""
        if self.code.idle_seconds <= 0:
            return
        print(f"Letting the server run alone for {self.code.idle_seconds:g} s ...", flush=True)
        self.clear()
        self.sleep(self.code.idle_seconds)
        self.dump(path)

    def classes(self, target: Path) -> None:
        """Copy the class locations out of the image, unless an earlier run already did."""
        if not target.exists():
            ServerClasses(self.code, self.image).copy(target)

    def facts(self) -> dict[str, bool]:
        """Store that the agent ran.

        Returns:
            The facts.
        """
        return {"code_coverage": True}


@dataclass(frozen=True, slots=True)
class ServerClasses:
    """Copies the class files of a server out of its image, since JaCoCo matches a class by the exact bytes it ran."""

    code: ServerCode
    image: str

    def copy(self, target: Path) -> None:
        """Stream the image's files through `docker export` and keep the class locations.

        Raises:
            RuntimeError: If docker fails, or the image holds none of the locations.
        """
        created = subprocess.run(["docker", "create", self.image], capture_output=True, text=True)
        if created.returncode != 0:
            raise RuntimeError(f"docker create {self.image} failed:\n{created.stderr[-2000:]}")
        container = created.stdout.strip()
        partial = target.with_name(target.name + ".partial")
        try:
            with subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE) as export:
                assert export.stdout is not None
                self.extract(export.stdout, partial)
            if export.returncode != 0:
                raise RuntimeError(f"docker export of {self.image} failed")
        finally:
            subprocess.run(["docker", "rm", container], capture_output=True, check=False)
        if not self.code.locations(partial):
            raise RuntimeError(f"{self.image} has none of {', '.join(self.code.classes)}")
        partial.rename(target)

    def extract(self, stream: Any, target: Path) -> None:
        """Keep the wanted members of a tar stream."""
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                if self.code.wanted(member.name) and (member.isfile() or member.isdir()):
                    archive.extract(member, target, filter="data")


@dataclass(frozen=True, slots=True)
class CodeCounts:
    """Covered lines, branches and methods of the server's package."""

    lines: int = 0
    branches: int = 0
    methods: int = 0

    def to_dict(self) -> dict[str, int]:
        """Turn the counts into JSON friendly data.

        Returns:
            The counts.
        """
        return {"lines": self.lines, "branches": self.branches, "methods": self.methods}

    def of(self, kind: str) -> int:
        """Read one kind, `lines`, `branches` or `methods`.

        Returns:
            The count.
        """
        return self.to_dict()[kind]

    @classmethod
    def from_dict(cls, raw: dict[str, int]) -> Self:
        """Read counts saved by `to_dict`.

        Returns:
            The counts.
        """
        return cls(raw["lines"], raw["branches"], raw["methods"])

    def __add__(self, other: "CodeCounts") -> "CodeCounts":
        """Add per kind.

        Returns:
            The sum.
        """
        return CodeCounts(self.lines + other.lines, self.branches + other.branches, self.methods + other.methods)

    def __sub__(self, other: "CodeCounts") -> "CodeCounts":
        """Subtract per kind.

        Returns:
            The difference.
        """
        return CodeCounts(self.lines - other.lines, self.branches - other.branches, self.methods - other.methods)


@dataclass(frozen=True, slots=True)
class CodeKept:
    """What a version covered of the code its reference covered.

    `lost_methods` are the methods the reference reached and the version didn't. `lost_classes` are the classes where
    the version reached none of them. Both are only filled when the methods got listed.
    """

    kept: CodeCounts
    reference: CodeCounts
    lost_methods: tuple[str, ...] = ()
    lost_classes: tuple[str, ...] = ()

    KINDS: ClassVar[tuple[str, ...]] = ("lines", "branches", "methods")

    def share(self, kind: str) -> float:
        """The part of the reference's lines, branches or methods this version kept.

        Returns:
            A number from 0 to 1, or 1 when the reference covered none.
        """
        total = self.reference.of(kind)
        return self.kept.of(kind) / total if total else 1.0

    def to_dict(self) -> dict[str, Any]:
        """Turn it into JSON friendly data.

        Returns:
            The kept and reference counts, and what got lost.
        """
        return {
            "kept": self.kept.to_dict(),
            "of": self.reference.to_dict(),
            "lost_methods": list(self.lost_methods),
            "lost_classes": list(self.lost_classes),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read it back.

        Returns:
            The kept coverage.
        """
        return cls(
            CodeCounts.from_dict(raw["kept"]),
            CodeCounts.from_dict(raw["of"]),
            tuple(raw.get("lost_methods", ())),
            tuple(raw.get("lost_classes", ())),
        )


class MethodName:
    """Method names as the helper writes them, like `org.example.Pet.name()Ljava/lang/String;`."""

    @staticmethod
    def owner(method: str) -> str:
        """The class of a method. A descriptor has no dots, so the last dot before it ends the class name.

        Returns:
            The class name.
        """
        return method[: method.index("(")].rsplit(".", 1)[0]


@dataclass(frozen=True, slots=True)
class KeptPair:
    """A version's dump files and those of the reference it gets compared with. `name` is the key of the result."""

    name: Hashable
    files: tuple[Path, ...]
    reference: tuple[Path, ...]
    methods: bool = False


@dataclass(slots=True)
class GroupCount:
    """What the helper counted for one group of files.

    `missing` names classes that ran but have no class file, like a module jar the subject's paths don't name.
    """

    covered: CodeCounts
    totals: CodeCounts
    methods: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class CodeCheck:
    """The helper's count of all dumps, which JaCoCo's own report agreed with, and the classes without a file."""

    covered: CodeCounts
    missing: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Turn it into JSON friendly data.

        Returns:
            The counts and the missing classes.
        """
        return {**self.covered.to_dict(), "missing": list(self.missing)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read it back.

        Returns:
            The check.
        """
        return cls(CodeCounts.from_dict(raw), tuple(raw.get("missing", ())))


class CodeCounter:
    """Counts the server code that groups of dump files covered, through the helper.

    Covered in both a version V and its reference R is V plus R minus V and R joined. That holds because JaCoCo
    builds the covered lines, branches and methods of joined files by joining what each probe covers. When V only
    holds files of R, the join is R itself.
    """

    def __init__(self, jacoco: JaCoCo, locations: list[Path], prefix: str, idle: Path | None) -> None:
        """Keep the class locations, the package prefix and the idle file whose probes get left out."""
        self._jacoco = jacoco
        self._locations = locations
        self._prefix = prefix
        self._idle = idle

    def kept(self, pairs: Iterable[KeptPair]) -> dict[Hashable, CodeKept]:
        """Count what every version kept of its reference, in one helper run.

        Returns:
            The kept coverage by version name.
        """
        pairs = list(pairs)
        groups: dict[frozenset[Path], bool] = {}
        for pair in pairs:
            files, reference = frozenset(pair.files), frozenset(pair.reference)
            groups[files] = groups.get(files, False) or pair.methods
            groups[reference] = groups.get(reference, False) or pair.methods
            groups.setdefault(files | reference, False)
        counted = self.count(groups)
        result = {}
        for pair in pairs:
            files, reference = frozenset(pair.files), frozenset(pair.reference)
            version, base = counted[files], counted[reference]
            both = version.covered + base.covered - counted[files | reference].covered
            lost: tuple[str, ...] = ()
            gone: tuple[str, ...] = ()

            # The reference may list its methods for another pair, so only a listed version gets what it lost.
            if pair.methods:
                lost = tuple(sorted(base.methods - version.methods))
                classes = {MethodName.owner(method) for method in base.methods}
                gone = tuple(sorted(classes - {MethodName.owner(method) for method in version.methods}))
            result[pair.name] = CodeKept(both, base.covered, lost, gone)
        return result

    def count(self, groups: dict[frozenset[Path], bool], masked: bool = True) -> dict[frozenset[Path], GroupCount]:
        """Count groups of files. A group maps to whether its covered methods get listed too.

        Returns:
            The counts by group.
        """
        names = {files: f"g{number}" for number, files in enumerate(groups)}
        with tempfile.TemporaryDirectory() as folder:
            job, out = Path(folder) / "job.tsv", Path(folder) / "out.tsv"
            lines = [f"classes\t{location}" for location in self._locations] + [f"package\t{self._prefix}"]
            if masked and self._idle is not None:
                lines.append(f"idle\t{self._idle}")
            for files, methods in groups.items():
                lines.append(f"group\t{names[files]}\t{int(masked)}\t{int(methods)}")
                lines += [f"exec\t{path}" for path in sorted(files)]
            job.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._jacoco.count(job, out)
            by_name = self._read(out)
        return {files: by_name[name] for files, name in names.items()}

    @staticmethod
    def _read(out: Path) -> dict[str, GroupCount]:
        """Read the helper's lines.

        Returns:
            The count of each group.

        Raises:
            ValueError: If a class file differs from the one the server ran, since its coverage would count as zero.
        """
        counts: dict[str, GroupCount] = {}
        unmatched: set[str] = set()
        for line in out.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if parts[0] == "group":
                numbers = [int(part) for part in parts[2:8]]
                counts[parts[1]] = GroupCount(CodeCounts(*numbers[0::2]), CodeCounts(*numbers[1::2]))
            elif parts[0] == "method":
                counts[parts[1]].methods.add(parts[2])
            elif parts[0] == "missing":
                counts[parts[2]].missing.add(parts[1])
            elif parts[0] == "nomatch":
                unmatched.add(parts[1])
        if unmatched:
            raise ValueError(
                f"{len(unmatched)} class files differ from what the server ran, such as {min(unmatched)}. "
                "Was the image rebuilt under the same tag?"
            )
        return counts

    def check(self, files: list[Path], xml: Path) -> CodeCheck:
        """Make sure the helper counts all files the same way as JaCoCo's own report, without the idle mask.

        Returns:
            The covered counts both agree on, and the classes that ran without a class file.

        Raises:
            ValueError: If they differ.
        """
        group = frozenset(files)
        ours = self.count({group: False}, masked=False)[group]
        root = self._jacoco.report(sorted(group), self._locations, xml)
        theirs, totals = CodeCounts(), CodeCounts()
        for package in root.iter("package"):
            if (package.get("name", "") + "/").startswith(self._prefix):
                covered, missed = ReportCounters.of(package)
                theirs, totals = theirs + covered, totals + covered + missed
        if (ours.covered, ours.totals) != (theirs, totals):
            raise ValueError(f"The helper counts {ours.covered} of {ours.totals}, JaCoCo's report {theirs} of {totals}")
        return CodeCheck(ours.covered, tuple(sorted(ours.missing)))


class ReportCounters:
    """Reads the counters of one element of JaCoCo's XML report."""

    _KINDS: ClassVar[dict[str, str]] = {"LINE": "lines", "BRANCH": "branches", "METHOD": "methods"}

    @classmethod
    def of(cls, element: ElementTree.Element) -> tuple[CodeCounts, CodeCounts]:
        """Read the direct counters of an element.

        Returns:
            The covered and the missed counts.
        """
        covered: dict[str, int] = dict.fromkeys(cls._KINDS.values(), 0)
        missed = dict(covered)
        for counter in element.findall("counter"):
            kind = cls._KINDS.get(counter.get("type", ""))
            if kind is not None:
                covered[kind] = int(counter.get("covered", "0"))
                missed[kind] = int(counter.get("missed", "0"))
        return CodeCounts(**covered), CodeCounts(**missed)
