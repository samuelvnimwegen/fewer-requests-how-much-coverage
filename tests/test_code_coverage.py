import json
from pathlib import Path

import pytest
from test_jacoco import FakeJaCoCo

from lesstests.code_coverage import BenchCode, RecordingCode
from lesstests.coverage import CoverageSet
from lesstests.jacoco import CodeCounts, CodeKept, ServerCode
from lesstests.java import JavaTest, Outcome
from lesstests.merge_study import MergeStudy
from lesstests.recording import RecordedTest, Recording
from lesstests.subjects import FINERACT, GOOGLE_DRIVE, PETSTORE
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
