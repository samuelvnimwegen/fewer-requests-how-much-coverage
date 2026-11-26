"""Sample response faults and price each isolated test run."""

import json
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any, Self

from lesstests.api import ApiDescription
from lesstests.fault_inventory import FaultInventory
from lesstests.faults import FaultFamily
from lesstests.java import Outcome
from lesstests.recording import EveryClass, RecordedTest, Recording


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
    requested: int
    test_limit: int | None
    population: dict[str, int]
    trials: list[FaultTrial]
    baseline: dict[str, str]
    controls: list[str]
    reset_seconds: float | None
    jvm_seconds: float

    @classmethod
    def build(cls, recording: Recording, size: int, seed: int, limit: int | None) -> Self:
        """Draw without replacement in rounds across the available kinds.

        Returns:
            The schedule with one reset per execution.
        """
        meta = recording.meta()
        api = ApiDescription.load(Path(meta["spec"]), meta.get("restcov_base_path"))
        inventory = FaultInventory.build(recording, api)
        records = recording.records()
        times = {r.test.id: r.seconds for r in records}
        baseline = {r.test.id: r.outcome.value for r in records}
        passing = {t for t in baseline if baseline[t] == Outcome.PASSED}
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
                receivers = sorted(inventory.receivers[family])
                if limit is not None and len(receivers) > limit:
                    receiver_rng.shuffle(receivers)
                    receivers.sort(key=lambda t: t not in passing)
                    receivers = sorted(receivers[:limit])
                trials.append(FaultTrial(family, receivers))
                if not groups[kind]:
                    del groups[kind]
        pairs = [t for trial in trials for t in trial.tests]
        class_reset = meta.get("reset") == EveryClass.NAME
        controls = sorted(set(pairs) & passing) if class_reset else []
        return cls(
            str(recording.folder.resolve()),
            seed,
            size,
            limit,
            population,
            trials,
            baseline,
            controls,
            cls._reset_cost(recording, records, class_reset),
            sum(times[t] for t in pairs + controls),
        )

    @staticmethod
    def _reset_cost(recording: Recording, records: list[RecordedTest], class_reset: bool) -> float | None:
        """Estimate overhead between completed tests across recorded resets.

        Returns:
            Median seconds after subtracting the next JVM time, if observed.
        """
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
        return median(gaps) if gaps else None

    def report(self) -> dict[str, Any]:
        """Count the draws and price their resets and JVMs.

        Returns:
            The counts and wall time estimate in seconds.
        """
        pairs = [t for trial in self.trials for t in trial.tests]
        runs = len(pairs) + len(self.controls)
        return {
            "eligible": self.population,
            "sampled": dict(Counter(t.family.kind for t in self.trials)),
            "families": len(self.trials),
            "distinct_tests": len(set(pairs)),
            "fault_pairs": len(pairs),
            "healthy_controls": len(self.controls),
            "test_runs": runs,
            "seconds_per_reset": self.reset_seconds,
            "jvm_seconds": self.jvm_seconds,
            "estimated_seconds": None if self.reset_seconds is None else self.jvm_seconds + runs * self.reset_seconds,
        }

    def save(self, path: Path) -> None:
        """Write the plan with its short report."""
        path.write_text(json.dumps({**asdict(self), "report": self.report()}, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read a saved plan.

        Returns:
            The original ordered trials and baseline.
        """
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("report")
        raw["trials"] = [FaultTrial(FaultFamily.from_dict(row["family"]), row["tests"]) for row in raw["trials"]]
        return cls(**raw)
