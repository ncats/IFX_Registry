"""Input contracts for source-oriented application use cases."""

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from ifx_registry.application.progress import NullProgressReporter, ProgressReporter
from ifx_registry.domain.models import SourceVersion

DEFAULT_SOURCE_TIMEOUT = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class VersionProbeRequest:
    """Settings for checking the latest available source version."""

    timeout: timedelta = DEFAULT_SOURCE_TIMEOUT

    def __post_init__(self) -> None:
        if self.timeout.total_seconds() <= 0:
            raise ValueError("timeout must be positive")


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """Settings for fetching one source snapshot into a working directory."""

    destination: Path
    expected_version: SourceVersion | None = None
    timeout: timedelta = DEFAULT_SOURCE_TIMEOUT
    progress: ProgressReporter = field(default_factory=NullProgressReporter)

    def __post_init__(self) -> None:
        if self.timeout.total_seconds() <= 0:
            raise ValueError("timeout must be positive")
        object.__setattr__(self, "destination", Path(self.destination))
