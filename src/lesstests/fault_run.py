"""Run paired tests and save the evidence for each family."""

import hashlib
import json
import os
import random
import sqlite3
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Any, Self
from uuid import uuid4

from lesstests.api import ApiDescription
from lesstests.fault_inventory import FaultInventory
from lesstests.faults import FaultFamily, ProxyFault
from lesstests.java import JavaTest, JvmRunner, JvmSettings, Outcome, Overlay
from lesstests.recording import EveryClass, Recording
from lesstests.subjects import SUBJECTS, Connection, Runners

CATCHES = ("assertion failure", "client error")
UNCAUGHT = "not caught by the sampled tests"
SUPPORT = Path(__file__).parent / "support"


@dataclass(slots=True)
class FaultTrial:
    """A family and the tests chosen from its receivers."""

    family: FaultFamily
    tests: list[str]


@dataclass(slots=True)
class FaultPlan:
    """The sampled families, healthy outcomes and costs used for a run."""

    recording: str
    seed: int
    test_limit: int
    population: dict[str, int]
    trials: list[FaultTrial]
    inventory: dict[str, int]
    reset_seconds: float | None
    times: dict[str, float]

    @classmethod
    def build(cls, recording: Recording, size: int, seed: int, limit: int = 3) -> Self:
        """Return a seeded schedule.

        Returns: One trial per sampled family.
        """
        meta = recording.meta()
        api = ApiDescription.load(Path(meta["spec"]), meta.get("restcov_base_path"))
        inventory = FaultInventory.build(recording, api)
        records = recording.records()
        passing = {r.test.id for r in records if r.outcome == Outcome.PASSED and not r.errors}
        groups: dict[str, list[FaultFamily]] = defaultdict(list)
        for family, receivers in inventory.receivers.items():
            if receivers & passing:
                groups[family.kind].append(family)
        population = {kind: len(group) for kind, group in groups.items()}
        rng = random.Random(seed)
        receiver_rng = random.Random(seed)
        for group in groups.values():
            rng.shuffle(group)
        trials = []
        while groups and len(trials) < size:
            kinds = sorted(groups)
            rng.shuffle(kinds)
            for kind in kinds[: size - len(trials)]:
                family = groups[kind].pop()
                receivers = sorted(inventory.receivers[family] & passing)
                if len(receivers) > limit:
                    receiver_rng.shuffle(receivers)
                    receivers = sorted(receivers[:limit])
                trials.append(FaultTrial(family, receivers))
                if not groups[kind]:
                    del groups[kind]
        class_reset = meta.get("reset") == EveryClass.NAME
        ordered = sorted(
            (((recording.tests_folder / f"{r.test.id}.json").stat().st_mtime, r) for r in records),
            key=lambda item: item[0],
        )
        gaps = [
            end - start - current.seconds
            for (start, previous), (end, current) in pairwise(ordered)
            if (not class_reset or previous.test.class_name != current.test.class_name)
            and end - start >= current.seconds
        ]
        counts = dict(Counter(f.kind for f in inventory.receivers))
        reset = median(gaps) if gaps else None
        times = {r.test.id: r.seconds for r in records}
        return cls(str(recording.folder.resolve()), seed, limit, population, trials, counts, reset, times)

    def report(self, first: int | None = None) -> dict[str, Any]:
        """Return the cost of a plan prefix.

        Returns: The sampling frame and estimate.
        """
        trials = self.trials[:first]
        pairs = [t for trial in trials for t in trial.tests]
        runs = 2 * len(pairs)
        jvm = 2 * sum(self.times[t] for t in pairs)
        return {
            "seed": self.seed,
            "test_limit": self.test_limit,
            "interpretation": "Not caught by the sampled tests leaves other receivers unknown.",
            "inventory": self.inventory,
            "excluded_without_passing_error_free_receiver": {
                k: n - self.population.get(k, 0) for k, n in self.inventory.items()
            },
            "eligible": self.population,
            "sampled": dict(Counter(t.family.kind for t in trials)),
            "inventory_total": sum(self.inventory.values()),
            "eligible_total": sum(self.population.values()),
            "families": len(trials),
            "fault_pairs": len(pairs),
            "healthy_controls": len(pairs),
            "seconds_per_reset": self.reset_seconds,
            "cost_basis": "One control and one fault per pair before exclusions or repeat checks on catches.",
            "estimated_seconds": None if self.reset_seconds is None else jvm + runs * self.reset_seconds,
        }

    def save(self, path: Path) -> None:
        """Write the plan with its short report."""
        data = {**asdict(self), "report": self.report(), "first_20": self.report(20)}
        path.write_text(json.dumps(data, indent=1))

    @classmethod
    def load(cls, path: Path) -> Self:
        """Return the saved plan.

        Returns: The ordered schedule.
        """
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("report")
        raw.pop("first_20")
        raw["trials"] = [FaultTrial(FaultFamily.from_dict(row["family"]), row["tests"]) for row in raw["trials"]]
        return cls(**raw)


@dataclass(slots=True)
class FaultSession:
    """One service and the isolated JVMs that call it."""

    connection: Connection
    runner: JvmRunner
    api: ApiDescription
    scratch: Path
    testng: bool

    @classmethod
    @contextmanager
    def open(cls, plan: FaultPlan, output: Path, env: dict[str, str]) -> Generator[Self, None, None]:
        """Start the instrumented client and service.

        Yields:
            The session after compilation.

        Raises:
            ValueError: If the recorded client changed.
            RuntimeError: If another service owns the container names.
        """
        recording = Recording(Path(plan.recording))
        meta = recording.meta()
        subject = SUBJECTS[meta["subject"]]
        api = ApiDescription.load(Path(meta["spec"]), meta.get("restcov_base_path"))
        connection = subject.connect(env)
        with TemporaryDirectory(dir=output) as scratch:
            root = Path(scratch)
            testng = subject.name == "petstore"
            module = "src/test/java" if testng else "fineract-client-feign/src/main/java"
            helpers = ["FaultEvidence.java", "FaultListener.java" if testng else "FaultInterceptor.java"]
            overlays = [Overlay(SUPPORT / h, f"{module}/lesstests/{h}") for h in helpers]
            if testng:
                connection.settings = connection.settings.merged(JvmSettings(listeners=("lesstests.FaultListener",)))
            else:
                target = f"{module}/org/apache/fineract/client/feign/FineractFeignClientConfig.java"
                source = (recording.project_folder / target).read_text()
                builder = ".client(getOrCreateHttpClient())"
                if source.count(builder) != 1:
                    raise ValueError("Cannot identify the Fineract HTTP client builder")
                patched = root / Path(target).name
                wrapper = ".client(lesstests.FaultInterceptor.wrap(getOrCreateHttpClient()))"
                patched.write_text(source.replace(builder, wrapper))
                overlays.append(Overlay(patched, target))
            project = subject.toolchain.project(recording.project_folder, root / "project", tuple(overlays))
            project.prepare()
            runner = Runners.of(subject, project, root / "reports", connection.settings)
            with connection.proxy:
                name = f"lesstests-{subject.name}"
                existing = subprocess.check_output(["docker", "ps", "-a", "--format", "{{.Names}}"], text=True)
                if {name, name + "-db"}.intersection(existing.splitlines()):
                    raise RuntimeError(f"Existing {name} containers must be checked before starting")
                try:
                    connection.service.start()
                    yield cls(connection, runner, api, root, testng)
                finally:
                    connection.service.stop()

    def run(self, family: FaultFamily, test: str, control: bool) -> dict[str, Any]:
        """Return one solo JVM result.

        Returns: Its outcome and receipt after a reset.
        """
        token = uuid4().hex
        fault = ProxyFault(self.api, family, token, control)
        proxy = self.connection.proxy
        self.connection.service.reset()
        path = self.scratch / token
        self.connection.settings.properties.update(
            {"lesstests.execution": token, "lesstests.test": test, "lesstests.evidence": str(path)}
        )
        proxy.arm(fault)
        try:
            result = self.runner.run(JavaTest.from_id(test))
        finally:
            proxy.arm(None)
        traffic, errors = proxy.log.take()
        events = path.read_text().splitlines() if path.exists() else []
        failures = [line.split("\t", 2) for line in events if line not in {token, "finished"}]
        expected_exit = int(self.testng and result.outcome == Outcome.FAILED)
        normal = result.exit_code == expected_exit and events.count("finished") == 1
        received = bool(fault.hit and fault.sent and token in events and not errors)
        verdict, reason = "not applied", "No completed call phase delivery and client receipt"
        if received:
            verdict, reason = "unresolved", "Inconsistent outcome, failure phase or JVM exit"
            if normal and result.outcome == Outcome.PASSED and not failures:
                verdict, reason = "passed", ""
            elif normal and result.outcome == Outcome.FAILED and failures and events[0] == token:
                kinds = {failure[0] for failure in failures}
                if len(kinds) == 1 and failures[0][0] in CATCHES:
                    verdict, reason = failures[0][0], ""
        hit = fault.hit
        return {
            **asdict(result),
            "execution": token,
            "applied": received and not control,
            "receipt": received,
            "matched": bool(hit),
            "sent": fault.sent,
            "failures": failures,
            "reset": "before each solo JVM",
            "call_requests": sum(e.request.headers.get("X-Lesstests-Execution") == token for e in traffic),
            "errors": errors,
            "verdict": verdict,
            "reason": reason,
            "target": hit.exchange.request.target if hit else None,
            "edits": [asdict(e) for e in hit.edits] if hit and not control else [],
        }

    def pair(self, family: FaultFamily, test: str, confirm: bool = True) -> dict[str, Any]:
        """Return the paired measurement.

        Returns: Both arms and the confirmation evidence.
        """
        control = self.run(family, test, True)
        if control["verdict"] != "passed":
            reason = control["reason"] or control["verdict"]
            return dict(control=control, applied=False, verdict="not applied", excluded=reason)
        row = self.run(family, test, False)
        row["control"] = control
        if confirm and row["verdict"] in CATCHES:
            repeat = self.pair(family, test, confirm=False)
            row["confirmation"] = repeat
            if repeat["verdict"] != row["verdict"] or repeat.get("failures") != row["failures"]:
                row.update(verdict="unresolved", reason="Catch did not repeat under a passing control")
        return row


@dataclass(slots=True)
class FaultRun:
    """Checkpoint pairs and publish each completed family."""

    plan: FaultPlan
    folder: Path

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1))
        temporary.replace(path)

    def identity(self, env: dict[str, str]) -> str:
        """Identify the experiment inputs.

        Returns:
            A digest for resume.

        Raises:
            ValueError: If this subject lacks a reset or a client hook.
        """
        recording = Recording(Path(self.plan.recording))
        meta = recording.meta()
        if meta["subject"] not in {"petstore", "fineract"}:
            raise ValueError("This measurement needs a resettable subject and an instrumented client")
        tags = SUBJECTS[meta["subject"]].meta().values()
        images = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", *tags])
        java = subprocess.check_output(["java", "-version"], stderr=subprocess.STDOUT)
        names = "JAVA_HOME PATH JAVA_TOOL_OPTIONS JDK_JAVA_OPTIONS DOCKER_HOST DOCKER_CONTEXT"
        settings = {k: os.getenv(k) for k in names.split()}
        digest = hashlib.sha256(json.dumps([asdict(self.plan), env, settings], sort_keys=True).encode() + images + java)
        paths = [Path(meta["spec"]), recording.folder / "meta.json", *recording.tests_folder.glob("*.json")]
        paths.extend(recording.project_folder.rglob("*"))
        paths.extend([*SUPPORT.iterdir(), *SUPPORT.parent.glob("*.py")])
        for path in sorted(p for p in paths if p.is_file()):
            digest.update(os.fsencode(path) + b"\0" + hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    def run(self, connect: Callable[[], AbstractContextManager[FaultSession]], gate: bool, identity: str) -> None:
        """Resume a matching measurement.

        Raises:
            ValueError: If existing evidence has a different identity.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            lock = stack.enter_context(closing(sqlite3.connect(self.folder / "run.lock", timeout=0)))
            lock.execute("BEGIN IMMEDIATE")
            marker = self.folder / "identity.json"
            if marker.exists():
                if json.loads(marker.read_text()) != identity:
                    raise ValueError("The plan, recording, tool or runtime changed")
            elif any(p.name not in {"run.lock", "run.lock-journal"} for p in self.folder.iterdir()):
                raise ValueError("Existing results have no matching identity")
            self._write(marker, identity)
            stopped = False
            session = None
            rows = []
            summary = {"planned": len(self.plan.trials), "test_limit": self.plan.test_limit, "families": rows}
            path = self.folder / "kills.json"
            self._write(path, summary)
            for index, trial in enumerate(self.plan.trials, 1):
                checkpoint = self.folder / f"family-{index}.json"
                pairs = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
                for test in trial.tests:
                    if test not in pairs:
                        if stopped:
                            return
                        if session is None:
                            session = stack.enter_context(connect())
                        pairs[test] = session.pair(trial.family, test)
                        self._write(checkpoint, pairs)
                caught = [t for t in trial.tests if pairs[t]["verdict"] in CATCHES]
                verdicts = {p["verdict"] for p in pairs.values() if not p.get("excluded")}
                fallback = UNCAUGHT if verdicts == {"passed"} else "unresolved"
                verdict = next((k for k in CATCHES if k in verdicts), fallback)
                rows.append(dict(family=asdict(trial.family), caught=caught, verdict=verdict, pairs=checkpoint.name))
                self._write(path, summary)
                if index <= 20:
                    print(f"Family {index}: {verdict}", flush=True)
                if index == 20:
                    mix = Counter(r["verdict"] for r in rows)
                    print(f"First 20 families: {dict(mix)}", flush=True)
                    if gate and (mix["unresolved"] == 20 or mix["client error"] >= 18):
                        stopped = True
                        print("Gate stopped further executions on this evidence.", flush=True)
