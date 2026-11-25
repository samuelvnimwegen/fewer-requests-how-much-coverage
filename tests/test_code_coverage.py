import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fake_restcov import FakeRestCov
from test_jacoco import FakeJaCoCo

from lesstests import merge_study
from lesstests.code_coverage import BenchCode, RecordingCode
from lesstests.coverage import CoverageSet, SuiteCoverage
from lesstests.jacoco import CodeCounts, CodeKept, ServerCode
from lesstests.java import JavaTest, Outcome
from lesstests.merge import MergeOutcome
from lesstests.merge_study import MergeStudy, PlanStudy
from lesstests.recording import RecordedTest, Recording
from lesstests.subjects import FINERACT, FINERACT_JVM_FLAGS, GOOGLE_DRIVE, PETSTORE, SUBJECTS
from lesstests.versions import SuiteVersion, VersionPlan

CODE = ServerCode("a", ("classes",), 8083)
REPORT = '<report><package name="a">{counters}</package></report>'


def _recording(tmp_path: Path, dumps: dict[str, str]) -> Recording:
    """Every test has a dump with one covered item per word, which `FakeJaCoCo` counts."""
    recording = Recording(tmp_path / "rec")
    recording.write_meta({"subject": "petstore", "code_coverage": True})
    (recording.classes_folder / "classes").mkdir(parents=True)
    for method, items in dumps.items():
        test = JavaTest("s.Api", method)
        recording.save(RecordedTest(test, Outcome.PASSED, 1.0))
        recording.exec_of(test).parent.mkdir(parents=True, exist_ok=True)
        recording.exec_of(test).write_text(items)
    return recording


def _counters(lines: tuple[int, int]) -> str:
    kinds = {"LINE": lines, "BRANCH": (100, 0), "METHOD": (100, 0)}
    return "".join(f'<counter type="{kind}" missed="{m}" covered="{c}"/>' for kind, (m, c) in kinds.items())


def test_a_recording_without_code_coverage_has_none(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "rec")
    recording.write_meta({"subject": "petstore"})
    assert RecordingCode.of(recording, None, CODE) is None


def test_a_recording_with_code_coverage_needs_jacoco(tmp_path: Path) -> None:
    recording = _recording(tmp_path, {"a": "line:1"})
    with pytest.raises(ValueError, match="LESSTESTS_JACOCO"):
        RecordingCode.of(recording, None, CODE)


def test_the_check_against_jacocos_report_runs_once_per_recording(tmp_path: Path) -> None:
    recording = _recording(tmp_path, {"a": "line:1", "b": "line:2"})
    jacoco = FakeJaCoCo(tmp_path, REPORT.format(counters=_counters((98, 2))))
    code = RecordingCode.of(recording, jacoco, CODE)
    assert code is not None
    assert code.check().covered == CodeCounts(2, 0, 0)

    # A wrong report would fail the check, so a second call that passes read the saved result.
    object.__setattr__(jacoco, "report_xml", REPORT.format(counters=_counters((90, 10))))
    assert code.check().covered == CodeCounts(2, 0, 0)


def test_a_version_with_a_merged_copy_finds_its_dump_in_the_merged_recording(tmp_path: Path) -> None:
    recording = _recording(tmp_path, {"a": "line:1 line:2", "b": "line:3"})
    merged = Recording(tmp_path / "merged")
    copy = JavaTest("s.Api", "merged1")
    merged.exec_of(copy).parent.mkdir(parents=True)
    merged.exec_of(copy).write_text("line:1 line:3")
    code = RecordingCode([recording], FakeJaCoCo(tmp_path), CODE).with_merged(merged)
    tests = [JavaTest("s.Api", "a"), JavaTest("s.Api", "b")]

    def version(name: str, chosen: list[JavaTest]) -> SuiteVersion:
        return SuiteVersion(name, chosen, CoverageSet(), frozenset(), len(chosen))

    plan = VersionPlan([version("Original", tests), version("Select + Merge", [copy])])
    code.annotate(plan)
    assert plan.versions[1].code == CodeKept(CodeCounts(2, 0, 0), CodeCounts(3, 0, 0))
    assert "| 2 of 3 (66.7%) | 0 of 0 (100.0%) | 0 of 0 (100.0%) |" in plan.markdown()

    saved = SuiteVersion.from_dict(json.loads(json.dumps(plan.versions[1].to_dict())))
    assert saved.code == plan.versions[1].code


def test_a_plan_without_code_coverage_keeps_its_old_table(tmp_path: Path) -> None:
    plan = VersionPlan([SuiteVersion("Original", [], CoverageSet(), frozenset(), 0)])
    assert "Lines kept" not in plan.markdown()
    assert "code" not in plan.original.to_dict()


def test_bench_compares_each_run_with_its_rounds_original_and_original_with_the_recording(tmp_path: Path) -> None:
    recording = _recording(tmp_path, {"a": "line:1 line:2", "b": "line:3"})
    code = RecordingCode([recording], FakeJaCoCo(tmp_path), CODE)
    runs = tmp_path / "runs"
    runs.mkdir()
    dumps = {}
    for (version, number), items in {
        ("Original", 1): "line:1 line:2 line:3",
        ("Select", 1): "line:1 line:9",
        ("Original", 2): "line:1 line:2",
        ("Select", 2): "line:1 line:2",
    }.items():
        dumps[version, number] = runs / f"{version}-{number}.exec"
        dumps[version, number].write_text(items)
    kept = BenchCode.count(code, dumps, "Original")
    assert kept.runs["Select", 1].kept.lines == 1
    assert kept.runs["Select", 1].reference.lines == 3
    assert kept.runs["Select", 2].share("lines") == 1.0
    assert kept.recording[2] == CodeKept(CodeCounts(2, 0, 0), CodeCounts(3, 0, 0))
    assert BenchCode.from_dict(json.loads(json.dumps(kept.to_dict()))) == kept


def test_a_kept_merge_brings_its_dump_from_the_round_that_recorded_it(tmp_path: Path) -> None:
    rounds = tmp_path / "rounds"
    test = JavaTest("s.Api", "merged2")
    source = Recording(rounds / "round-2" / "recording").exec_of(test)
    source.parent.mkdir(parents=True)
    source.write_text("dump")
    merged = Recording(tmp_path / "merged")
    MergeStudy._copy_dump(rounds, merged, test)
    assert merged.exec_of(test).read_text() == "dump"
    with pytest.raises(FileNotFoundError, match="found 0"):
        MergeStudy._copy_dump(rounds, merged, JavaTest("s.Api", "merged3"))


def test_only_the_subjects_with_a_server_the_tool_runs_measure_code(tmp_path: Path) -> None:
    jacoco = FakeJaCoCo(tmp_path)
    petstore = PETSTORE.connect({}, jacoco)
    assert petstore.code.on
    assert not PETSTORE.connect({}).code.on
    fineract = FINERACT.connect({}, jacoco)
    assert fineract.code.on
    with pytest.raises(ValueError, match="no code coverage"):
        GOOGLE_DRIVE.connect(
            {"GOOGLE_CLIENT_ID": "x", "GOOGLE_CLIENT_SECRET": "y", "GOOGLE_REFRESH_TOKEN": "z"}, jacoco
        )


def test_a_recording_whose_class_files_never_got_copied_says_so(tmp_path: Path) -> None:
    recording = _recording(tmp_path, {"a": "line:1"})
    shutil.rmtree(recording.classes_folder)
    with pytest.raises(ValueError, match="has no class files yet. Run record again"):
        RecordingCode([recording], FakeJaCoCo(tmp_path), CODE)


def test_the_check_runs_again_once_a_dump_is_newer(tmp_path: Path) -> None:
    recording = _recording(tmp_path, {"a": "line:1", "b": "line:2"})
    jacoco = FakeJaCoCo(tmp_path, REPORT.format(counters=_counters((98, 2))))
    code = RecordingCode([recording], jacoco, CODE)
    code.check()
    object.__setattr__(jacoco, "report_xml", REPORT.format(counters=_counters((90, 10))))
    dump = recording.exec_of(JavaTest("s.Api", "a"))
    later = (recording.code_folder / "check.json").stat().st_mtime + 10
    os.utime(dump, (later, later))
    with pytest.raises(ValueError, match="report"):
        code.check()


class FakeRoundRunner:
    made: list["FakeRoundRunner"] = []

    def __init__(self, subject: Any, meta: Any, env: Any, progress: Any, jacoco: Any = None) -> None:
        self.jacoco = jacoco
        FakeRoundRunner.made.append(self)

    def project(self, edited: Any, folder: Path) -> None:
        pass


@dataclass
class FakeMerged:
    test: JavaTest

    def to_dict(self) -> dict[str, Any]:
        return {"test": self.test.id}


def test_merge_runs_its_rounds_with_the_agent_and_brings_the_kept_dumps_into_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = _recording(tmp_path, {"a": "line:1 line:2", "b": "line:3"})
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"openapi": "3.0.0", "paths": {}}))
    recording.write_meta({"subject": "petstore", "code_coverage": True, "spec": str(spec), "project": str(tmp_path)})
    copy = JavaTest("s.Api", "merged1")

    class FakeMerger:
        def __init__(self, planner: Any, runner: Any, api: Any, rounds: int) -> None:
            pass

        def run(self, folder: Path) -> MergeOutcome:
            dump = Recording(folder / "round-1" / "recording").exec_of(copy)
            dump.parent.mkdir(parents=True)
            dump.write_text("line:9")
            record = RecordedTest(copy, Outcome.PASSED, 1.0)
            return MergeOutcome([FakeMerged(copy)], {copy: record}, [])  # type: ignore[list-item]

    monkeypatch.setattr(merge_study, "Merger", FakeMerger)
    monkeypatch.setattr(merge_study, "MergeRoundRunner", FakeRoundRunner)
    monkeypatch.setattr(merge_study.MergePlanner, "edited_files", lambda self, merged: {})
    FakeRoundRunner.made.clear()
    restcov = FakeRestCov()
    coverage = SuiteCoverage.measure(recording, restcov, tmp_path / "coverage")
    code = RecordingCode([recording], FakeJaCoCo(tmp_path), CODE)
    MergeStudy(recording, restcov, coverage, {}, 1, code=code).run(tmp_path / "merge")

    assert FakeRoundRunner.made[0].jacoco is code.jacoco
    assert Recording(tmp_path / "merge" / "merged").exec_of(copy).read_text() == "line:9"
    versions = json.loads((tmp_path / "merge" / "plan.json").read_text())["versions"]
    assert all("code" in version for version in versions)


def test_plan_gives_every_version_its_code(tmp_path: Path) -> None:
    recording = _recording(tmp_path, {"a": "line:1 line:2", "b": "line:3"})
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"openapi": "3.0.0", "paths": {}}))
    recording.write_meta({"subject": "fineract", "code_coverage": True, "spec": str(spec)})
    coverage = SuiteCoverage.measure(recording, FakeRestCov(), tmp_path / "coverage")
    code = RecordingCode([recording], FakeJaCoCo(tmp_path), CODE)
    PlanStudy(recording, coverage, code).run(tmp_path / "plan")
    versions = json.loads((tmp_path / "plan" / "plan.json").read_text())["versions"]
    assert versions[0]["code"]["of"] == {"lines": 3, "branches": 0, "methods": 0}
    assert "code" in versions[1]


def test_a_copied_dump_found_in_two_rounds_stops(tmp_path: Path) -> None:
    rounds = tmp_path / "rounds"
    test = JavaTest("s.Api", "merged2")
    for name in ("round-1", "round-2"):
        source = Recording(rounds / name / "recording").exec_of(test)
        source.parent.mkdir(parents=True)
        source.write_text("dump")
    with pytest.raises(FileNotFoundError, match="found 2"):
        MergeStudy._copy_dump(rounds, Recording(tmp_path / "merged"), test)


def test_connect_puts_the_agent_into_the_container_it_starts(tmp_path: Path) -> None:
    jacoco = FakeJaCoCo(tmp_path)
    fineract = FINERACT.connect({}, jacoco).service._app  # type: ignore[attr-defined]
    options = fineract.env["JAVA_TOOL_OPTIONS"]
    assert options.startswith(FINERACT_JVM_FLAGS + " -javaagent:")
    assert "127.0.0.1:8093:6300" in fineract.docker_args
    petstore = PETSTORE.connect({}, jacoco).service._spec  # type: ignore[attr-defined]
    assert petstore.env["JAVA_TOOL_OPTIONS"].startswith("-javaagent:")
    assert "127.0.0.1:8083:6300" in petstore.docker_args
    assert "JAVA_TOOL_OPTIONS" not in PETSTORE.connect({}).service._spec.env  # type: ignore[attr-defined]


def test_the_subjects_measure_the_right_package_with_their_own_ports() -> None:
    ports = []
    for subject in SUBJECTS.values():
        ports.append(subject.listen.port)  # type: ignore[attr-defined]
        ports += [getattr(subject, "service_port", None), subject.server_code.port if subject.server_code else None]
    taken = [port for port in ports if port is not None]
    assert len(taken) == len(set(taken))
    assert PETSTORE.server_code is not None and FINERACT.server_code is not None
    assert PETSTORE.server_code.package == "io.swagger.sample"
    assert PETSTORE.server_code.idle_seconds == 0
    assert FINERACT.server_code.package == "org.apache.fineract"
    assert "/app/libs/fineract-*.jar" in FINERACT.server_code.classes

    # The slowest frequent job runs every 15 minutes, so the idle run has to see one tick of it.
    assert 15 * 60 < FINERACT.server_code.idle_seconds <= 20 * 60


def test_per_test_methods_drop_idle_probes(tmp_path: Path) -> None:
    """Requirement: Method novelty counts only methods that a test reaches after removing idle probes."""
    recording = _recording(tmp_path, {"a": "method:A method:B", "b": "method:B method:C"})
    recording.idle_file.write_text("method:B")
    code = RecordingCode([recording], FakeJaCoCo(tmp_path), CODE)
    a, b = [record.test for record in recording.records()]
    assert code.methods_by_test([a, b]) == {a: {"A"}, b: {"C"}}


def test_external_check_leaves_the_cache_with_the_recording(tmp_path: Path) -> None:
    """Requirement: An external check uses a current recording cache and only stores its report externally."""
    recording = _recording(tmp_path, {"a": "line:1"})
    jacoco = FakeJaCoCo(tmp_path, REPORT.format(counters=_counters((99, 1))))
    code = RecordingCode([recording], jacoco, CODE)
    out = tmp_path / "results"
    assert code.check(out).covered.lines == 1
    assert not (out / "check.json").exists()
    assert not (recording.code_folder / "check.json").exists()
    code.check()
    object.__setattr__(jacoco, "report_xml", "")
    assert code.check(out).covered.lines == 1
