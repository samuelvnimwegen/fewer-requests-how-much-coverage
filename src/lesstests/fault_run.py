"""Run the planned tests and keep the evidence for each catch."""

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Self

from lesstests.api import ApiDescription
from lesstests.fault_plan import FaultPlan, FaultTrial
from lesstests.faults import FaultFamily, ProxyFault
from lesstests.java import JavaTest, JvmRunner, Outcome, RunResult
from lesstests.recording import Recording
from lesstests.subjects import SUBJECTS, Connection, Runners

CATCHES = ("assertion failure", "client error")


@dataclass(slots=True)
class FaultSession:
    """One running service shared by isolated tests."""

    connection: Connection
    runner: JvmRunner
    api: ApiDescription

    @classmethod
    @contextmanager
    def open(cls, plan: FaultPlan, output: Path, env: dict[str, str]) -> Generator[Self, None, None]:
        """Compile the recorded sources in a private copy and start the service.

        Yields:
            The session until the run finishes or stops.
        """
        recording = Recording(Path(plan.recording))
        meta = recording.meta()
        api = ApiDescription.load(Path(meta["spec"]), meta.get("restcov_base_path"))
        subject = SUBJECTS[meta["subject"]]
        connection = subject.connect(env)
        with TemporaryDirectory(dir=output) as scratch:
            project = subject.toolchain.project(recording.project_folder, Path(scratch) / "project", ())
            project.prepare()
            runner = Runners.of(subject, project, Path(scratch) / "reports", connection.settings)
            with connection.proxy:
                try:
                    connection.service.start()
                    yield cls(connection, runner, api)
                finally:
                    connection.service.stop()

    def run(self, test: str, fault: ProxyFault | None) -> tuple[RunResult, list[str]]:
        """Reset before arming the fault so startup traffic cannot take the edit.

        Returns:
            The JVM verdict and any proxy errors during this execution.
        """
        proxy = self.connection.proxy
        self.connection.service.reset()
        proxy.log.take()
        proxy.arm(fault)
        try:
            result = self.runner.run(JavaTest.from_id(test))
        finally:
            proxy.arm(None)
        _, errors = proxy.log.take()
        return result, errors

    def pair(self, family: FaultFamily, test: str, healthy: str) -> dict[str, Any]:
        """Keep the concrete edits and report category for this test.

        Returns:
            The observed result and whether it counts as a catch.
        """
        fault = ProxyFault(self.api, family)
        result, errors = self.run(test, fault)
        hit = fault.hit
        verdict = "unresolved"
        if hit is None:
            verdict = "not applied"
        elif healthy != Outcome.PASSED:
            verdict = "unhealthy baseline"
        elif not errors and result.outcome == Outcome.PASSED:
            verdict = "survived"
        elif not errors and result.outcome == Outcome.FAILED and result.failure_kind in CATCHES:
            verdict = result.failure_kind
        return {
            "outcome": result.outcome,
            "failure_kind": result.failure_kind,
            "verdict": verdict,
            "healthy": healthy,
            "proxy_errors": errors,
            "applied": hit is not None,
            "target": hit.exchange.request.target if hit else None,
            "edits": [asdict(edit) for edit in hit.edits] if hit else [],
        }


class FaultRun:
    """Checkpoint each result and publish the observed catches after each family."""

    def __init__(self, plan: FaultPlan, folder: Path) -> None:
        """Keep the schedule and checkpoint folder."""
        self.plan = plan
        self.folder = folder

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        """Replace one JSON file only after its new contents reach disk."""
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
        temporary.replace(path)

    def run(self, connect: Callable[[], AbstractContextManager[FaultSession]], gate: bool) -> None:
        """Resume only results collected with this plan.

        Raises:
            ValueError: If the output belongs to another plan.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            # Two runners must never reset the same trial or replace each other's checkpoints.
            lock = stack.enter_context(closing(sqlite3.connect(self.folder / "run.lock", timeout=0)))
            lock.execute("BEGIN IMMEDIATE")
            key = hashlib.sha256(json.dumps(asdict(self.plan), sort_keys=True).encode()).hexdigest()
            marker = self.folder / "plan.sha256"
            if marker.exists() and json.loads(marker.read_text()) != key:
                raise ValueError("This output belongs to another fault plan")
            self._write(marker, key)
            session = None
            controls_path = self.folder / "controls.json"
            controls = json.loads(controls_path.read_text()) if controls_path.exists() else {}
            kills_path = self.folder / "kills.json"
            saved = len(json.loads(kills_path.read_text())["families"]) if kills_path.exists() else 0
            summary: dict[str, Any] = {
                "families": [],
                "planned": len(self.plan.trials),
                "test_limit": self.plan.test_limit,
                "recorded_tests": sorted(self.plan.baseline),
            }
            if not kills_path.exists():
                self._write(kills_path, summary)
            rows = summary["families"]
            for index, trial in enumerate(self.plan.trials, 1):
                checkpoint = self.folder / f"family-{index}.json"
                pairs = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
                for test in trial.tests:
                    if test in pairs:
                        continue
                    if session is None:
                        session = stack.enter_context(connect())
                    if test in self.plan.controls and test not in controls:
                        result, errors = session.run(test, None)
                        controls[test] = Outcome.ERROR if errors else result.outcome
                        self._write(controls_path, controls)
                    healthy = controls.get(test, self.plan.baseline[test])
                    pairs[test] = session.pair(trial.family, test, healthy)
                    self._write(checkpoint, pairs)
                rows.append(self._family(trial, pairs, checkpoint))
                if index > saved:
                    self._write(kills_path, summary)
                if index == 20:
                    mix = Counter({v: 0 for v in (*CATCHES, "survived", "unresolved")})
                    mix.update(row["verdict"] for row in rows)
                    print(f"First 20 families: {dict(mix)}", flush=True)
                    if gate and saved <= 20 and mix["client error"] >= 18:
                        print("Gate stopped the run: at least 18 of 20 families produced only client error catches.")
                        return

    def _family(self, trial: FaultTrial, pairs: dict[str, Any], checkpoint: Path) -> dict[str, Any]:
        """Count the catches among tests with passing recorded baselines.

        Returns:
            The family verdict and the tests behind it.
        """
        eligible = [t for t in trial.tests if self.plan.baseline[t] == Outcome.PASSED]
        caught = [t for t in eligible if pairs[t]["verdict"] in CATCHES]
        verdicts = {pairs[t]["verdict"] for t in eligible}
        verdict = next((kind for kind in CATCHES if kind in verdicts), "unresolved")
        if verdicts == {"survived"}:
            verdict = "survived"
        return {
            "family": asdict(trial.family),
            "kind": trial.family.kind,
            "caught": caught,
            "verdict": verdict,
            "pairs": checkpoint.name,
        }

    @staticmethod
    def score(path: Path, kept: set[str]) -> dict[str, Any]:
        """Count retained catches among the measured families by change kind.

        Returns:
            Numerators and denominators with the limits of the measured sample.

        Raises:
            ValueError: If a kept test is absent from the recording.
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        if unknown := kept.difference(data["recorded_tests"]):
            raise ValueError(f"Unknown kept test ids: {sorted(unknown)}")
        counts: dict[str, dict[str, int]] = {}
        for row in data["families"]:
            kind = counts.setdefault(row["kind"], {"caught": 0, "retained": 0, "survived": 0, "unresolved": 0})
            if row["caught"]:
                kind["caught"] += 1
                kind["retained"] += int(not kept.isdisjoint(row["caught"]))
            else:
                kind[row["verdict"]] += 1
        return {
            "per_kind": {
                kind: {**row, "retained_fraction": row["retained"] / row["caught"] if row["caught"] else None}
                for kind, row in counts.items()
            },
            "measured": len(data["families"]),
            "planned": data["planned"],
            "test_limit": data["test_limit"],
        }
