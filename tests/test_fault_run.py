import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from lesstests.api import ApiDescription, Operation
from lesstests.cli import main
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.fault_plan import FaultPlan, FaultTrial
from lesstests.fault_run import FaultRun, FaultSession
from lesstests.faults import FaultFamily
from lesstests.java import JavaTest, JUnitLauncher, Outcome, RunResult, TestNgLauncher
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
        count,
        None,
        {"status": count},
        trials,
        {"pets.a": "passed", "pets.b": "passed"},
        ["pets.a"],
        3,
        28,
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
    assert len(plan.trials) == len(plan.population) == 3
    assert all(t.tests == ["pets.a", "pets.b"] and t.family.status == 200 for t in plan.trials)
    limited = FaultPlan.build(recording, 3, 25, 1)
    assert all(t.tests == ["pets.a"] for t in limited.trials)
    assert limited == FaultPlan.build(recording, 3, 25, 1)
    folder = tmp_path / "out"
    with pytest.raises(SystemExit, match="Pass --live"):
        main(["fault-run", "--plan", str(path), "--out", str(folder)])
    assert not folder.exists()


@pytest.mark.parametrize(
    ("apply", "healthy", "outcome", "reason", "expected"),
    [
        (False, "passed", Outcome.PASSED, None, "not applied"),
        (False, "passed", Outcome.FAILED, "client error", "not applied"),
        (True, "failed", Outcome.FAILED, "assertion failure", "unhealthy baseline"),
        (True, "passed", Outcome.FAILED, "client error", "client error"),
        (True, "passed", Outcome.FAILED, "assertion failure", "assertion failure"),
        (True, "passed", Outcome.ERROR, None, "unresolved"),
        (True, "passed", Outcome.PASSED, None, "survived"),
    ],
)
def test_pair(apply: bool, healthy: str, outcome: Outcome, reason: str | None, expected: str) -> None:
    """Requirement: A catch needs a healthy test, a confirmed edit and a reported failure."""
    connection = MagicMock()
    runner = Mock()
    connection.proxy.log.take.return_value = ([], [])

    def run(test: JavaTest) -> RunResult:
        assert test.id == "pets.a"
        connection.service.reset.assert_called_once()
        if apply:
            fault = connection.proxy.arm.call_args.args[0]
            assert fault.apply(EXCHANGE).status == 400
        return RunResult(outcome, 1, reason)

    runner.run.side_effect = run
    session = FaultSession(connection, runner, ApiDescription([Operation("GET", "/pets", frozenset())], []))
    row = session.pair(FAMILY, "pets.a", healthy)
    assert row["verdict"] == expected and row["outcome"] == outcome and row["applied"] == apply
    assert [e["replacement"] for e in row["edits"]] == (["400"] if apply else [])
    connection.proxy.arm.assert_called_with(None)


def test_checkpoint_gate_and_score(tmp_path: Path) -> None:
    """Requirement: Resume skips saved pairs and controls, counts each family once and honours the gate."""
    plan = _plan(tmp_path, 21)
    session = Mock(spec=FaultSession)
    session.run.return_value = RunResult(Outcome.PASSED, 1), []
    error = {"healthy": "passed", "verdict": "client error"}
    survived = {"healthy": "passed", "verdict": "survived"}
    session.pair.side_effect = [error, InterruptedError()]
    experiment = FaultRun(plan, tmp_path)
    with pytest.raises(InterruptedError):
        experiment.run(lambda: nullcontext(session), True)
    assert list(json.loads((tmp_path / "family-1.json").read_text())) == ["pets.a"]
    session.pair.side_effect = lambda fault, test, healthy: error if test == "pets.a" else survived
    experiment.run(lambda: nullcontext(session), True)
    path = tmp_path / "kills.json"
    assert len(json.loads(path.read_text())["families"]) == 20
    experiment.run(lambda: nullcontext(session), False)
    session.run.assert_called_once()
    assert session.pair.call_count == 43
    report = FaultRun.score(path, {"pets.b"})
    assert report["per_kind"]["status"] == {
        "caught": 21,
        "retained": 0,
        "survived": 0,
        "unresolved": 0,
        "retained_fraction": 0.0,
    }
    assert FaultRun.score(path, {"pets.a"})["per_kind"]["status"]["retained"] == 21
    experiment.run(Mock(side_effect=AssertionError), True)
    assert FaultRun.score(path, set())["measured"] == 21
    plan.seed += 1
    with pytest.raises(ValueError, match="another fault plan"):
        experiment.run(Mock(side_effect=AssertionError), False)


def test_report_failure_categories(tmp_path: Path) -> None:
    """Requirement: The category comes from the matching failed method in each framework report."""
    test = JavaTest("pets", "a")
    for exception, reason in [
        ("java.lang.AssertionError", "assertion failure"),
        ("java.io.IOException", "client error"),
    ]:
        (tmp_path / "testng-results.xml").write_text(
            '<class name="pets"><test-method name="a" status="FAIL">'
            f'<exception class="{exception}"/></test-method></class>'
        )
        assert TestNgLauncher().failure_kind(tmp_path, test) == reason
        (tmp_path / "failures.tsv").write_text(f"pets\tb\tother\npets\ta\t{reason}\n")
        assert JUnitLauncher().failure_kind(tmp_path, test) == reason


def test_proxy_error_and_failed_clean_control_stay_unresolved(tmp_path: Path) -> None:
    """Requirement: A failed fresh control or a proxy error cannot turn an applied fault into a catch."""
    plan = _plan(tmp_path, 1)
    session = Mock(spec=FaultSession)
    session.run.return_value = RunResult(Outcome.FAILED, 1, "client error"), []
    session.pair.side_effect = lambda family, test, healthy: {
        "healthy": healthy,
        "verdict": "unhealthy baseline" if healthy != "passed" else "survived",
    }
    FaultRun(plan, tmp_path).run(lambda: nullcontext(session), False)
    assert FaultRun.score(tmp_path / "kills.json", set())["per_kind"]["status"]["unresolved"] == 1
    connection = MagicMock()
    runner = Mock()

    def fail(test: JavaTest) -> RunResult:
        connection.proxy.arm.call_args.args[0].apply(EXCHANGE)
        return RunResult(Outcome.FAILED, 1, "client error")

    runner.run.side_effect = fail
    connection.proxy.log.take.side_effect = [([], []), ([], ["connection lost"])]
    actual = FaultSession(connection, runner, ApiDescription([Operation("GET", "/pets", frozenset())], []))
    assert actual.pair(FAMILY, "pets.a", "passed")["verdict"] == "unresolved"
