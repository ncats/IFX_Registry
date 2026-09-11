"""Presentation-independent progress reporting for long source operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    stage: str
    message: str
    completed: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        if not self.stage.strip() or not self.message.strip():
            raise ValueError("progress stage and message must not be blank")
        if self.completed is not None and self.completed < 0:
            raise ValueError("completed progress must not be negative")
        if self.total is not None and self.total <= 0:
            raise ValueError("total progress must be positive")


class ProgressReporter(Protocol):
    def report(self, update: ProgressUpdate) -> None:
        """Report the latest state of a long-running operation."""


class NullProgressReporter:
    def report(self, update: ProgressUpdate) -> None:
        del update
