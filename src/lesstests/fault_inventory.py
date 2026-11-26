"""Count the tests that receive each response fault family."""

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Self

from lesstests.api import ApiDescription
from lesstests.faults import FaultFamily, ResponseFaults
from lesstests.recording import Recording


@dataclass(slots=True)
class FaultInventory:
    """The distinct recorded tests that receive each family."""

    receivers: dict[FaultFamily, set[str]]
    tests: set[str]

    @classmethod
    def build(cls, recording: Recording, api: ApiDescription) -> Self:
        """Read the exchanges in the recording.

        Returns:
            The families of documented operations and their receivers.
        """
        match = lru_cache(maxsize=8192)(api.match)
        receivers = defaultdict[FaultFamily, set[str]](set)
        tests: set[str] = set()
        for record in recording.records():
            tests.add(record.test.id)
            for exchange in record.exchanges:
                operation = match(exchange.request.method, exchange.request.target)
                if operation is not None:
                    for family in ResponseFaults(operation.name, exchange).families():
                        receivers[family].add(record.test.id)
        return cls(dict(receivers), tests)

    def save(self, path: Path) -> None:
        """Write the families in recording order with sorted test ids."""
        rows = [{**asdict(family), "tests": sorted(tests)} for family, tests in self.receivers.items()]
        path.write_text(json.dumps(rows, indent=1), encoding="utf-8")

    def report(self, kept: set[str] | None = None) -> dict[str, object]:
        """Summarise exposure across tests.

        Returns:
            Counts by change, with single receivers and losses for the given suite.

        Raises:
            ValueError: If a kept id is missing from the recording.
        """
        if kept and (unknown := kept - self.tests):
            raise ValueError(f"Unknown kept test ids: {', '.join(sorted(unknown))}")
        kinds = Counter(family.kind for family in self.receivers)
        report: dict[str, object] = {
            "families": len(self.receivers),
            "per_change": dict(sorted(kinds.items())),
            "single_receiver": sum(len(tests) == 1 for tests in self.receivers.values()),
        }
        if kept is not None:
            report["no_kept_receiver"] = sum(tests.isdisjoint(kept) for tests in self.receivers.values())
        return report
