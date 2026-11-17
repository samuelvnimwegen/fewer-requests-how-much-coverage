import io
import os
import shutil
import subprocess
import tarfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from lesstests import jacoco as jacoco_module
from lesstests.jacoco import (
    AgentCoverage,
    CodeCounter,
    CodeCounts,
    CodeKept,
    JaCoCo,
    KeptPair,
    MethodName,
    ServerClasses,
    ServerCode,
)
from lesstests.service import ContainerSpec

FINERACT_CODE = ServerCode("org.apache.fineract", ("/app/classes", "/app/libs/fineract-*.jar"), port=8093)


class FakeJaCoCo(JaCoCo):
    """Counts with sets in place of the helper. Each dump file holds one covered item per line, like `line:Pet:3`."""

    calls: list[str]
    report_xml: str

    def __init__(self, folder: Path, report: str = "") -> None:
        super().__init__(folder)
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "report_xml", report)

    def dump(self, port: int, path: Path) -> None:
        self.calls.append(f"dump {port} {path.name}")

    def report(self, files: list[Path], classes: list[Path], xml: Path) -> ElementTree.Element:
        return ElementTree.fromstring(self.report_xml)

    def count(self, job: Path, out: Path) -> None:
        idle: set[str] = set()
        groups: list[tuple[str, bool, bool, set[str]]] = []
        for line in job.read_text().splitlines():
            parts = line.split("\t")
            if parts[0] == "idle":
                idle = set(Path(parts[1]).read_text().split())
            elif parts[0] == "group":
                groups.append((parts[1], parts[2] == "1", parts[3] == "1", set()))
            elif parts[0] == "exec":
                groups[-1][3].update(Path(parts[1]).read_text().split())
        lines = []
        for name, masked, methods, items in groups:
            items = items - idle if masked else items
            counts = [sum(item.startswith(kind) for item in items) for kind in ("line", "branch", "method")]
            lines.append("\t".join(["group", name, *(f"{count}\t100" for count in counts)]))
            if methods:
                lines += [f"method\t{name}\t{item.split(':', 1)[1]}" for item in sorted(items) if item[0] == "m"]
        out.write_text("\n".join(lines) + "\n")


def _dump(tmp_path: Path, name: str, *items: str) -> Path:
    path = tmp_path / f"{name}.exec"
    path.write_text("\n".join(items))
    return path


def test_the_agent_joins_the_java_tool_options_a_container_already_sets() -> None:
    spec = ContainerSpec("fineract:1", 8080, "/health", env={"JAVA_TOOL_OPTIONS": "-Xmx2G"}, docker_args=("--x",))
    jacoco = JaCoCo(Path("/opt/jacoco"))
    with_agent = FINERACT_CODE.container(spec, jacoco)
    options = with_agent.env["JAVA_TOOL_OPTIONS"]
    assert options.startswith("-Xmx2G -javaagent:/lesstests/jacocoagent.jar=output=tcpserver,address=*,port=6300,")
    assert options.endswith("includes=org.apache.fineract.*")
    assert with_agent.docker_args == (
        "--x",
        "-v",
        "/opt/jacoco/jacocoagent.jar:/lesstests/jacocoagent.jar:ro",
        "-p",
        "127.0.0.1:8093:6300",
    )
    assert spec.env == {"JAVA_TOOL_OPTIONS": "-Xmx2G"}


def test_a_container_without_java_tool_options_gets_only_the_agent() -> None:
    spec = ContainerSpec("petstore:1", 8080, "/health")
    options = ServerCode("io.swagger.sample", ("/x",), 8083).container(spec, JaCoCo(Path("/j"))).env
    assert options["JAVA_TOOL_OPTIONS"].startswith("-javaagent:")


def test_jacoco_needs_both_jars(tmp_path: Path) -> None:
    (tmp_path / "jacocoagent.jar").write_text("")
    with pytest.raises(ValueError, match="jacococli.jar"):
        JaCoCo.at(tmp_path)


def _tar(*names: str) -> io.BytesIO:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name in names:
            data = b"x"
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    stream.seek(0)
    return stream


def test_only_the_class_locations_come_out_of_the_image(tmp_path: Path) -> None:
    stream = _tar(
        "app/classes/org/apache/fineract/A.class",
        "app/libs/fineract-core.jar",
        "app/libs/spring-core.jar",
        "app/resources/application.properties",
        "app/classes-old/B.class",
    )
    ServerClasses(FINERACT_CODE, "fineract:1").extract(stream, tmp_path)
    found = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_file())
    assert found == ["app/classes/org/apache/fineract/A.class", "app/libs/fineract-core.jar"]
    assert [path.name for path in FINERACT_CODE.locations(tmp_path)] == ["classes", "fineract-core.jar"]


def test_a_server_without_idle_time_never_waits(tmp_path: Path) -> None:
    jacoco = FakeJaCoCo(tmp_path)
    slept: list[float] = []
    AgentCoverage(ServerCode("p", (), 8083), jacoco, "image", slept.append).idle(tmp_path / "idle.exec")
    assert jacoco.calls == []
    assert slept == []


def test_the_idle_dump_starts_from_zero_and_waits_the_idle_time(tmp_path: Path) -> None:
    jacoco = FakeJaCoCo(tmp_path)
    slept: list[float] = []
    code = ServerCode("p", (), 8093, idle_seconds=960.0)
    AgentCoverage(code, jacoco, "image", slept.append).idle(tmp_path / "idle.exec")
    assert jacoco.calls == ["dump 8093 cleared.exec", "dump 8093 idle.exec"]
    assert slept == [960.0]


def test_kept_code_counts_what_both_cover_even_when_the_version_reaches_more(tmp_path: Path) -> None:
    original = _dump(tmp_path, "o", "line:A:1", "line:A:2", "line:A:3", "branch:A:1:0", "method:A.a()V")
    version = _dump(tmp_path, "v", "line:A:2", "line:A:9", "branch:A:1:1", "method:A.a()V", "method:B.b()V")
    counter = CodeCounter(FakeJaCoCo(tmp_path), [], "a/", None)
    kept = counter.kept([KeptPair("Select", (version,), (original,))])["Select"]
    assert kept.kept == CodeCounts(lines=1, branches=0, methods=1)
    assert kept.reference == CodeCounts(lines=3, branches=1, methods=1)
    assert kept.share("lines") == pytest.approx(1 / 3)


def test_a_subset_of_the_reference_needs_no_extra_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = _dump(tmp_path, "a", "line:A:1")
    b = _dump(tmp_path, "b", "line:A:2")
    groups: list[int] = []
    original_count = CodeCounter.count

    def count(self: CodeCounter, wanted: dict[frozenset[Path], bool], masked: bool = True) -> dict:
        groups.append(len(wanted))
        return original_count(self, wanted, masked)

    monkeypatch.setattr(CodeCounter, "count", count)
    kept = CodeCounter(FakeJaCoCo(tmp_path), [], "a/", None).kept([KeptPair("half", (a,), (a, b))])
    assert groups == [2]
    assert kept["half"].kept.lines == 1


def test_the_idle_dump_drops_out_of_both_sides(tmp_path: Path) -> None:
    idle = _dump(tmp_path, "idle", "line:Job:1")
    original = _dump(tmp_path, "o", "line:A:1", "line:Job:1")
    version = _dump(tmp_path, "v", "line:Job:1")
    kept = CodeCounter(FakeJaCoCo(tmp_path), [], "a/", idle).kept([KeptPair("v", (version,), (original,))])["v"]
    assert (kept.kept.lines, kept.reference.lines) == (0, 1)


def test_lost_methods_and_classes_are_listed_on_request(tmp_path: Path) -> None:
    original = _dump(tmp_path, "o", "method:a.A.x()V", "method:a.A.y(I)V", "method:a.B.z()V", "method:a.C$1.w()V")
    version = _dump(tmp_path, "v", "method:a.A.x()V")
    pairs = [KeptPair("listed", (version,), (original,), methods=True), KeptPair("quiet", (version,), (original,))]
    kept = CodeCounter(FakeJaCoCo(tmp_path), [], "a/", None).kept(pairs)
    assert kept["listed"].lost_methods == ("a.A.y(I)V", "a.B.z()V", "a.C$1.w()V")
    assert kept["listed"].lost_classes == ("a.B", "a.C$1")
    assert kept["quiet"].kept == kept["listed"].kept

    # The reference lists its methods for `listed`, which must not make `quiet` lose them all.
    assert (kept["quiet"].lost_methods, kept["quiet"].lost_classes) == ((), ())


def test_a_method_name_keeps_its_class_even_with_dots_in_the_descriptor() -> None:
    assert MethodName.owner("org.x.Pet.name(Ljava/lang/String;)Lorg/x/Pet;") == "org.x.Pet"


def test_kept_code_survives_json() -> None:
    kept = CodeKept(CodeCounts(1, 2, 3), CodeCounts(4, 5, 6), ("a.A.x()V",), ("a.A",))
    assert CodeKept.from_dict(kept.to_dict()) == kept


REPORT = """<report name="r">
<package name="org/apache/fineract/a">{a}</package>
<package name="org/apache/fineractx">{other}</package>
<package name="org/apache/fineract/b">{b}</package>
</report>"""


def _counters(lines: tuple[int, int], branches: tuple[int, int], methods: tuple[int, int]) -> str:
    kinds = {"LINE": lines, "BRANCH": branches, "METHOD": methods}
    return "".join(f'<counter type="{kind}" missed="{m}" covered="{c}"/>' for kind, (m, c) in kinds.items())


def test_the_check_sums_only_the_packages_of_the_server(tmp_path: Path) -> None:
    dump = _dump(tmp_path, "t", "line:1", "line:2", "branch:1", "method:a.A.x()V")
    report = REPORT.format(
        a=_counters((48, 2), (49, 1), (49, 1)),
        b=_counters((50, 0), (50, 0), (50, 0)),
        other=_counters((0, 50), (0, 50), (0, 50)),
    )
    counter = CodeCounter(FakeJaCoCo(tmp_path, report), [], "org/apache/fineract/", None)
    assert counter.check([dump], tmp_path / "report.xml").covered == CodeCounts(2, 1, 1)


def test_the_check_stops_when_the_helper_and_the_report_differ(tmp_path: Path) -> None:
    dump = _dump(tmp_path, "t", "line:1")
    report = REPORT.format(a=_counters((98, 2), (100, 0), (100, 0)), b="", other="")
    counter = CodeCounter(FakeJaCoCo(tmp_path, report), [], "org/apache/fineract/", None)
    with pytest.raises(ValueError, match="report"):
        counter.check([dump], tmp_path / "report.xml")


APP = """package demo;

public class App {
    static int pick(int x) {
        if (x > 0) {
            return 1;
        }
        return 2;
    }

    static void tick() {
        System.out.print("");
    }

    public static void main(String[] args) {
        if (args[0].equals("pos")) {
            pick(5);
        }
        if (args[0].equals("neg")) {
            pick(-5);
        }
        if (args[0].equals("tick")) {
            tick();
        }
    }
}
"""


@pytest.mark.skipif(not os.environ.get("LESSTESTS_JACOCO"), reason="needs LESSTESTS_JACOCO and a JDK")
def test_the_helper_agrees_with_jacocos_own_report(tmp_path: Path) -> None:
    jacoco = JaCoCo.at(Path(os.environ["LESSTESTS_JACOCO"]))
    source = tmp_path / "src" / "demo" / "App.java"
    source.parent.mkdir(parents=True)
    source.write_text(APP)
    classes = tmp_path / "classes"
    subprocess.run(["javac", "-g", "-d", str(classes), str(source)], check=True)
    dumps = {}
    for argument in ("pos", "neg", "tick"):
        dumps[argument] = tmp_path / f"{argument}.exec"
        agent = f"-javaagent:{jacoco.agent}=destfile={dumps[argument]},includes=demo.*"
        subprocess.run(["java", agent, "-cp", str(classes), "demo.App", argument], check=True)
    jar = tmp_path / "app.jar"
    shutil.make_archive(str(jar.with_suffix("")), "zip", classes)
    jar.with_suffix(".zip").rename(jar)

    counter = CodeCounter(jacoco, [jar], "demo/", None)
    both = counter.check([dumps["pos"], dumps["neg"]], tmp_path / "both" / "report.xml").covered
    alone = counter.check([dumps["pos"]], tmp_path / "alone" / "report.xml").covered
    assert both.methods == 2
    assert alone.branches < both.branches

    # Each side ran other branches of `pick`, so only the join of both reaches all of them.
    pairs = [KeptPair("pos", (dumps["pos"],), (dumps["neg"],), methods=True)]
    kept = counter.kept(pairs)["pos"]
    neg = counter.check([dumps["neg"]], tmp_path / "neg" / "report.xml").covered
    assert kept.kept == alone + neg - both
    assert kept.reference == neg

    masked = CodeCounter(jacoco, [jar], "demo/", dumps["tick"])
    tick_masked = masked.kept([KeptPair("pos", (dumps["pos"],), (dumps["pos"],))])["pos"]
    assert tick_masked.reference.lines < alone.lines


def test_a_class_file_that_differs_from_what_the_server_ran_stops_the_count(tmp_path: Path) -> None:
    out = tmp_path / "out.tsv"
    out.write_text("group\tg0\t1\t2\t0\t0\t1\t1\nnomatch\torg/x/Pet\tg0\nmissing\torg/x/Gone\tg0\n")
    with pytest.raises(ValueError, match="1 class files differ from what the server ran, such as org/x/Pet"):
        CodeCounter._read(out)


def test_a_class_without_a_file_is_named_but_still_counted(tmp_path: Path) -> None:
    out = tmp_path / "out.tsv"
    out.write_text("group\tg0\t1\t2\t0\t0\t1\t1\nmissing\torg/x/Gone\tg0\n")
    count = CodeCounter._read(out)["g0"]
    assert count.missing == {"org/x/Gone"}
    assert count.covered == CodeCounts(1, 0, 1)


def test_a_dump_replaces_an_older_one_and_resets_the_counters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    target = tmp_path / "exec" / "t.exec"
    target.parent.mkdir()
    target.write_text("old session")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert not target.exists()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(jacoco_module.subprocess, "run", run)
    JaCoCo(Path("/j")).dump(8083, target)
    command = calls[0]
    assert command[:4] == ["java", "-jar", "/j/jacococli.jar", "dump"]
    assert command[command.index("--address") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "8083"
    assert command[command.index("--destfile") + 1] == str(target)
    assert "--reset" in command
    assert 1 <= int(command[command.index("--retry") + 1]) <= 10


def test_the_report_merges_into_a_fresh_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    merged = tmp_path / "report.exec"
    merged.write_text("old sessions")
    xml = tmp_path / "report.xml"

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[3] == "merge":
            assert not merged.exists()
        else:
            xml.write_text("<report/>")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(jacoco_module.subprocess, "run", run)
    assert JaCoCo(Path("/j")).report([tmp_path / "a.exec"], [tmp_path / "classes"], xml).tag == "report"


def test_classes_copied_once_are_not_copied_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[Path] = []
    monkeypatch.setattr(ServerClasses, "copy", lambda self, target: copied.append(target))
    agent = AgentCoverage(FINERACT_CODE, FakeJaCoCo(tmp_path), "image")
    agent.classes(tmp_path / "new")
    (tmp_path / "old").mkdir()
    agent.classes(tmp_path / "old")
    assert copied == [tmp_path / "new"]


def test_an_image_without_the_class_locations_stops_the_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    removed: list[str] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "rm":
            removed.append(command[2])
        return subprocess.CompletedProcess(command, 0, "abc123\n", "")

    class Export:
        def __init__(self, command: list[str], stdout: object) -> None:
            self.stdout = _tar("app/resources/application.properties")
            self.returncode = 0

        def __enter__(self) -> "Export":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(jacoco_module.subprocess, "run", run)
    monkeypatch.setattr(jacoco_module.subprocess, "Popen", Export)
    with pytest.raises(RuntimeError, match="has none of /app/classes"):
        ServerClasses(FINERACT_CODE, "fineract:1").copy(tmp_path / "classes")
    assert removed == ["abc123"]
    assert not (tmp_path / "classes").exists()


def _compile_and_run(tmp_path: Path, jacoco: JaCoCo, source: str, name: str, argument: str) -> tuple[Path, Path]:
    """Compile App from `source` into its own folder and run it once with the agent.

    Returns:
        The class folder and the dump.
    """
    folder = tmp_path / name
    java = folder / "src" / "demo" / "App.java"
    java.parent.mkdir(parents=True)
    java.write_text(source)
    classes = folder / "classes"
    subprocess.run(["javac", "-g", "-d", str(classes), str(java)], check=True)
    dump = folder / f"{argument}.exec"
    agent = f"-javaagent:{jacoco.agent}=destfile={dump},includes=demo.*"
    subprocess.run(["java", agent, "-cp", str(classes), "demo.App", argument], check=True)
    return classes, dump


@pytest.mark.skipif(not os.environ.get("LESSTESTS_JACOCO"), reason="needs LESSTESTS_JACOCO and a JDK")
def test_a_dump_of_another_build_stops_the_count_even_next_to_one_of_this_build(tmp_path: Path) -> None:
    jacoco = JaCoCo.at(Path(os.environ["LESSTESTS_JACOCO"]))
    _, old = _compile_and_run(tmp_path, jacoco, APP, "old", "pos")
    rebuilt = APP.replace("return 2;", "return 3;")
    classes, new = _compile_and_run(tmp_path, jacoco, rebuilt, "new", "neg")
    counter = CodeCounter(jacoco, [classes], "demo/", None)
    with pytest.raises(ValueError, match="1 class files differ from what the server ran, such as demo/App"):
        counter.kept([KeptPair("both", (old, new), (new,))])


@pytest.mark.skipif(not os.environ.get("LESSTESTS_JACOCO"), reason="needs LESSTESTS_JACOCO and a JDK")
def test_an_empty_dump_stops_the_count(tmp_path: Path) -> None:
    jacoco = JaCoCo.at(Path(os.environ["LESSTESTS_JACOCO"]))
    classes, dump = _compile_and_run(tmp_path, jacoco, APP, "app", "pos")
    empty = tmp_path / "empty.exec"
    empty.write_bytes(b"")
    with pytest.raises(RuntimeError, match="is empty"):
        CodeCounter(jacoco, [classes], "demo/", None).kept([KeptPair("e", (empty,), (dump,))])


def test_a_failed_docker_create_stops_with_its_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "Error: No such image: fineract:1")

    monkeypatch.setattr(jacoco_module.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="docker create fineract:1 failed:\nError: No such image: fineract:1"):
        ServerClasses(FINERACT_CODE, "fineract:1").copy(tmp_path / "classes")


@pytest.mark.skipif(not os.environ.get("LESSTESTS_JACOCO"), reason="needs LESSTESTS_JACOCO and a JDK")
def test_an_idle_dump_of_another_build_stops_the_count(tmp_path: Path) -> None:
    jacoco = JaCoCo.at(Path(os.environ["LESSTESTS_JACOCO"]))
    _, idle = _compile_and_run(tmp_path, jacoco, APP, "old", "tick")
    rebuilt = APP.replace("return 2;", "return 3;")
    classes, dump = _compile_and_run(tmp_path, jacoco, rebuilt, "new", "pos")
    counter = CodeCounter(jacoco, [classes], "demo/", idle)
    with pytest.raises(RuntimeError, match="The idle dump ran another build for 1 classes, such as demo/App"):
        counter.kept([KeptPair("pos", (dump,), (dump,))])
