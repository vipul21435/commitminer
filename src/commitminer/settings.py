"""Filter limits, feature caps, weights and difficulty bands, with their defaults.

Every number here can be set in ``commitminer.toml`` (see :mod:`commitminer.config`);
``mine`` also takes the filter limits on the command line.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields

from commitminer.classify import RULES, Rule


def _check_weights(weights: object) -> None:
    for item in fields(weights):  # type: ignore[arg-type]
        value = getattr(weights, item.name)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"weight {item.name} must be a finite number >= 0, got {value!r}")


@dataclass(frozen=True, slots=True)
class Weights:
    """Score weights; the defaults add up to 10, the best possible score."""

    small_diff: float = 3.0
    test_lines_added: float = 2.0
    added_assertions: float = 1.0
    linked_reference: float = 2.0
    fix_keyword: float = 1.0
    focused_source: float = 1.0

    def __post_init__(self) -> None:
        _check_weights(self)


@dataclass(frozen=True, slots=True)
class DifficultyWeights:
    """Difficulty weights; the defaults add up to 10, the hardest possible commit."""

    files: float = 1.0
    hunks: float = 3.0
    lines: float = 3.0
    cross_file: float = 2.0
    public_api: float = 1.0

    def __post_init__(self) -> None:
        _check_weights(self)


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the filter, the scorer and the difficulty estimate can be tuned with."""

    max_lines: int = 400
    """Hard filter: largest accepted diff, added plus deleted lines over source and test files."""
    max_source_files: int = 10
    """Hard filter: most source files one commit may change."""
    test_lines_cap: int = 40
    """Added test lines at which ``test_lines_added`` reaches its full value."""
    assertions_cap: int = 5
    """Added assertion lines at which ``added_assertions`` reaches its full value."""
    files_cap: int = 10
    """Source and test files at which the ``files`` difficulty reaches its full value."""
    hunks_cap: int = 10
    """Source code hunks at which ``hunks`` reaches its full value."""
    lines_cap: int = 100
    """Source code lines at which ``lines`` reaches its full value."""
    cross_file_cap: int = 4
    """Source files beyond the first at which ``cross_file`` reaches its full value."""
    medium_at: float = 2.0
    """Difficulty at which a candidate stops being easy."""
    hard_at: float = 4.5
    """Difficulty at which a candidate becomes hard."""
    weights: Weights = field(default_factory=Weights)
    difficulty_weights: DifficultyWeights = field(default_factory=DifficultyWeights)
    rules: tuple[Rule, ...] = RULES
    """Classifier table: the built-in rules, or a per-repository override."""

    def __post_init__(self) -> None:
        for name in (
            "max_lines",
            "max_source_files",
            "test_lines_cap",
            "assertions_cap",
            "files_cap",
            "hunks_cap",
            "lines_cap",
            "cross_file_cap",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if not 0 <= self.medium_at <= self.hard_at or not math.isfinite(self.hard_at):
            raise ValueError("difficulty bands need 0 <= medium_at <= hard_at")

    def band(self, difficulty: float) -> str:
        """``easy``, ``medium`` or ``hard`` for a difficulty value."""
        if difficulty >= self.hard_at:
            return "hard"
        return "medium" if difficulty >= self.medium_at else "easy"

    @property
    def bands_label(self) -> str:
        """The band thresholds as text, for reports."""
        return f"easy < {self.medium_at:g} <= medium < {self.hard_at:g} <= hard"
