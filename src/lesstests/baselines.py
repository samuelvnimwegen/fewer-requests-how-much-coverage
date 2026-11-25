import random
from collections import Counter, defaultdict
from typing import Protocol

from lesstests.java import JavaTest
from lesstests.selection import CoverageMatrix, RandomSelector


class Baseline(Protocol):
    """Pick a comparison suite for one Selected suite and draw."""

    name: str

    def draw(self, matrix: CoverageMatrix, selected: list[JavaTest], seed: int) -> list[JavaTest]:
        """Pick from the same pool as Selected.

        Returns:
            The comparison suite.
        """
        ...


class UniformRandom:
    """Give every suite of the same size an equal chance."""

    name = "random"

    def draw(self, matrix: CoverageMatrix, selected: list[JavaTest], seed: int) -> list[JavaTest]:
        """Sample without replacement.

        Returns:
            A suite with as many tests as Selected.
        """
        return RandomSelector(seed).select(matrix, len(selected))


class LengthMatchedRandom:
    """Sample within groups of tests that send exactly the same number of requests."""

    name = "length-matched-random"

    def draw(self, matrix: CoverageMatrix, selected: list[JavaTest], seed: int) -> list[JavaTest]:
        """Match the number of tests Selected picked at each length.

        Returns:
            A suite with the same test lengths as Selected.
        """
        picked = set(selected)
        groups: dict[int, list[JavaTest]] = defaultdict(list)
        counts: Counter[int] = Counter()
        for test, length in zip(matrix.tests, matrix.requests, strict=True):
            groups[length].append(test)
            if test in picked:
                counts[length] += 1
        rng = random.Random(seed)
        return sorted(test for length, count in counts.items() for test in rng.sample(groups[length], count))


DEFAULT_BASELINES: tuple[Baseline, ...] = (UniformRandom(), LengthMatchedRandom())
