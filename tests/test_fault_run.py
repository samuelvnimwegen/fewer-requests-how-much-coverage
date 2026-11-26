import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from lesstests.api import ApiDescription, Operation
from lesstests.cli import main
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.fault_run import FaultPlan, FaultRun, FaultSession, FaultTrial
from lesstests.faults import FaultFamily
from lesstests.java import JavaTest, JvmSettings, Outcome, RunResult
from lesstests.recording import RecordedTest, Recording

FAMILY = FaultFamily("GET /pets", None, 200, (), "status")
EXCHANGE = Exchange(HttpRequest("GET", "/pets", Headers(), Body()), HttpResponse(200, "", Headers(), Body(b"[1]")))


def _plan(tmp_path: Path, count: int) -> FaultPlan:
    trials = [
        FaultTrial(FaultFamily("GET /pets", str(i), 200, (), "status"), ["pets.a", "pets.b"]) for i in range(count)
    ]
    return FaultPlan(
        str(tmp_path / "rec"),
        25,
        3,
        {"status": count},
        trials,
        {"status": count},
        3,
        {"pets.a": 1.0, "pets.b": 2.0},
    )


def test_plan_and_consent(tmp_path: Path) -> None:
    """Requirement: Only families with a passing receiver enter the plan and live runs need consent."""
    recording = Recording(tmp_path / "rec")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"openapi": "3.0.0", "paths": {"/pets": {"get": {}}}}))
    recording.write_meta({"spec": str(spec)})
    for method, outcome in (("a", Outcome.PASSED), ("b", Outcome.FAILED)):
        recording.save(RecordedTest(JavaTest("pets", method), outcome, 1, [EXCHANGE]))
    failed = Exchange(EXCHANGE.request, HttpResponse(404, "", Headers(), Body()))
    recording.save(RecordedTest(JavaTest("pets", "c"), Outcome.FAILED, 2, [failed]))
    path = tmp_path / "plan.json"
    main(["fault-plan", "--recording", str(recording.folder), "--size", "100", "--out", str(path)])
    plan = FaultPlan.load(path)
    assert plan.test_limit == 3
    assert plan.report()["excluded_without_passing_error_free_receiver"]["status"] == 1
    assert len(plan.trials) == len(plan.population) == 3
    assert all(t.tests == ["pets.a"] and t.family.status == 200 for t in plan.trials)
    limited = FaultPlan.build(recording, 3, 25, 1)
    assert all(t.tests == ["pets.a"] for t in limited.trials)
    assert limited == FaultPlan.build(recording, 3, 25, 1)
    folder = tmp_path / "out"
    with pytest.raises(SystemExit, match="Pass --live"):
        main(["fault-run", "--plan", str(path), "--out", str(folder)])
    with pytest.raises(SystemExit) as rejected:
        main(["fault-run", "--plan", str(path), "--out", str(folder), "--liv"])
    assert rejected.value.code == 2
    assert not folder.exists()


@pytest.mark.parametrize(
    ("sent", "events", "outcome", "code", "verdict"),
    [
        (False, ["receipt"], Outcome.PASSED, 0, "not applied"),
        (True, [], Outcome.PASSED, 0, "not applied"),
        (True, ["echo"], Outcome.PASSED, 0, "not applied"),
        (True, ["receipt"], Outcome.PASSED, 0, "passed"),
        (True, ["receipt", "assertion failure"], Outcome.FAILED, 1, "assertion failure"),
        (True, ["receipt", "client error"], Outcome.FAILED, 1, "client error"),
        (True, ["receipt", "client error", "assertion failure"], Outcome.FAILED, 1, "unresolved"),
        (True, ["receipt", "outside selected method"], Outcome.FAILED, 1, "unresolved"),
        (True, ["client error", "receipt"], Outcome.FAILED, 1, "unresolved"),
        (True, ["receipt", "client error"], Outcome.FAILED, 9, "unresolved"),
        (True, ["receipt"], Outcome.ERROR, None, "unresolved"),
    ],
)
def test_execution_receipt(
    tmp_path: Path, sent: bool, events: list[str], outcome: Outcome, code: int | None, verdict: str
) -> None:
    """Requirement: Only a completed edit and a matching in method receipt allow scoring."""
    connection = MagicMock()
    connection.settings = JvmSettings()
    connection.proxy.log.take.return_value = ([], [])
    runner = Mock()

    def run(test: JavaTest) -> RunResult:
        assert test.id == "pets.a"
        fault = connection.proxy.arm.call_args.args[0]
        request = replace(EXCHANGE.request, headers=Headers((("X-Lesstests-Execution", fault.execution),)))
        fault.apply(replace(EXCHANGE, request=request))
        fault.sent = sent
        destination = Path(connection.settings.properties["lesstests.evidence"])
        proof = {"receipt": fault.receipt, "echo": fault.execution}
        destination.write_text("\n".join(proof.get(e, f"{e}\tThrowable\tmessage") for e in events))
        with destination.open("a") as stream:
            stream.write(("\n" if events else "") + "finished\n")
        return RunResult(outcome, 1, code)

    runner.run.side_effect = run
    session = FaultSession(
        connection, runner, ApiDescription([Operation("GET", "/pets", frozenset())], []), tmp_path, True
    )
    row = session.run(FAMILY, "pets.a", False)
    assert row["verdict"] == verdict
    assert row["failures"] == [[e, "Throwable", "message"] for e in events if e not in {"receipt", "echo"}]
    connection.proxy.arm.assert_called_with(None)
    runner.run.side_effect = RuntimeError("JVM failed to start")
    with pytest.raises(RuntimeError, match="JVM failed"):
        session.run(FAMILY, "pets.a", True)
    connection.proxy.arm.assert_called_with(None)


@pytest.mark.parametrize(
    ("sequence", "verdict"),
    [
        (["not applied"], "not applied"),
        (["client error"], "not applied"),
        (["passed", "passed"], "passed"),
        (["passed", "assertion failure", "passed", "assertion failure"], "assertion failure"),
        (["passed", "assertion failure", "passed", "passed"], "unresolved"),
        (["passed", "assertion failure", "not applied"], "unresolved"),
    ],
)
def test_controls(sequence: list[str], verdict: str) -> None:
    """Requirement: Every catch repeats with two passing controls that received the family."""
    session = Mock(spec=FaultSession)
    session.run.side_effect = [
        {"verdict": s, "receipt": s != "not applied", "reason": "", "failures": [s]} for s in sequence
    ]
    session.pair.side_effect = lambda family, test, confirm: FaultSession.pair(session, family, test, confirm)
    row = FaultSession.pair(session, FAMILY, "pets.a")
    assert row["verdict"] == verdict
    assert [call.args[-1] for call in session.run.call_args_list] == [True, False, True, False][: len(sequence)]
    assert row["control"]["verdict"] == sequence[0]


def test_resume(tmp_path: Path) -> None:
    """Requirement: Resume rebuilds stale summaries and reruns only missing pairs."""
    session = Mock(spec=FaultSession)
    session.pair.side_effect = [{"verdict": "passed"}, InterruptedError()]
    run = FaultRun(_plan(tmp_path, 2), tmp_path)
    with pytest.raises(InterruptedError):
        run.run(lambda: nullcontext(session), False, "original")
    session.pair.side_effect = None
    session.pair.return_value = {"verdict": "passed"}
    run.run(lambda: nullcontext(session), False, "original")
    assert session.pair.call_count == 5
    (tmp_path / "kills.json").write_text("broken")
    run.run(Mock(side_effect=AssertionError), False, "original")
    assert len(json.loads((tmp_path / "kills.json").read_text())["families"]) == 2
    (tmp_path / "family-1.json").unlink()
    session.pair.return_value = {"verdict": "assertion failure"}
    run.run(lambda: nullcontext(session), False, "original")
    rows = json.loads((tmp_path / "kills.json").read_text())["families"]
    assert [r["verdict"] for r in rows] == ["assertion failure", "not caught by the sampled tests"]
    with pytest.raises(ValueError, match="changed"):
        run.run(Mock(side_effect=AssertionError), False, "different")


@pytest.mark.parametrize("verdict", ["client error", "not applied"])
def test_gate(tmp_path: Path, verdict: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Requirement: The gate stops on a client error majority of 18 or on zero usable families."""
    session = Mock(spec=FaultSession)
    session.pair.return_value = {"verdict": verdict}
    FaultRun(_plan(tmp_path, 21), tmp_path).run(lambda: nullcontext(session), True, "original")
    assert session.pair.call_count == 40
    assert "First 20 families" in capsys.readouterr().out


def test_identity(tmp_path: Path) -> None:
    """Requirement: Resume detects changed recordings, plans, source, spec, runtime and tool code."""
    run = FaultRun(_plan(tmp_path, 1), tmp_path)
    rec = Recording(Path(run.plan.recording))
    spec = tmp_path / "spec"
    spec.write_text("{}")
    rec.write_meta({"subject": "petstore", "spec": str(spec)})
    rec.project_folder.mkdir()
    support = tmp_path / "package" / "support"
    support.mkdir(parents=True)
    with (
        patch("lesstests.fault_run.subprocess.check_output", return_value=b"runtime") as command,
        patch("lesstests.fault_run.SUPPORT", support),
    ):
        keys = [run.identity({})]
        for f in (rec.project_folder / "A.java", spec, support / "helper.java"):
            f.write_text("changed")
            keys.append(run.identity({}))
        rec.save(RecordedTest(JavaTest("pets", "a"), Outcome.PASSED, 1, []))
        keys.append(run.identity({}))
        with patch("lesstests.fault_run.version", return_value="another version"):
            keys.append(run.identity({}))
        run.plan.seed += 1
        keys.append(run.identity({}))
        command.return_value = b"other runtime"
        keys.append(run.identity({}))
        assert len(set(keys)) == len(keys)


def test_seeded_receivers(tmp_path: Path) -> None:
    """Requirement: The default draw uses three distinct passing receivers without recording errors."""
    rec = Recording(tmp_path / "recording")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"openapi": "3.0.0", "paths": {"/pets": {"get": {}}}}))
    rec.write_meta({"spec": str(spec)})
    for index in range(12):
        errors = ["proxy failure"] if index == 0 else []
        rec.save(RecordedTest(JavaTest("pets", str(index)), Outcome.PASSED, 1, [EXCHANGE], errors))
    first = FaultPlan.build(rec, 100, 25)
    assert first == FaultPlan.build(rec, 100, 25)
    assert first.trials != FaultPlan.build(rec, 100, 26).trials
    assert all(len(set(t.tests)) == 3 and "pets.0" not in t.tests for t in first.trials)
    assert first.report()["healthy_controls"] == 3 * len(first.trials)
