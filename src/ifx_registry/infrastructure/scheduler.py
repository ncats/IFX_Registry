"""Single-worker background scheduler for local Registry operation."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from ifx_registry.application.ports.derived_builds import DerivedBuildScheduler
from ifx_registry.application.ports.jobs import AcquisitionScheduler


class ThreadJobScheduler(AcquisitionScheduler, DerivedBuildScheduler):
    """Serialize long-running jobs without tying them to an HTTP request."""

    def __init__(self, execute: Callable[[str], None]):
        self._execute = execute
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="ifx-registry-acquisition",
            daemon=True,
        )
        self._thread.start()

    def submit(self, job_id: str) -> None:
        self._queue.put(job_id)

    def shutdown(self) -> None:
        self._queue.put(None)

    def _run(self) -> None:
        while (job_id := self._queue.get()) is not None:
            try:
                self._execute(job_id)
            except Exception:
                # The application executor persists an actionable failure before returning.
                pass
            finally:
                self._queue.task_done()


class ThreadAcquisitionScheduler(ThreadJobScheduler):
    """Background scheduler for source acquisition jobs."""


class ThreadDerivedBuildScheduler(ThreadJobScheduler):
    """Background scheduler for Registry-managed derived builds."""
