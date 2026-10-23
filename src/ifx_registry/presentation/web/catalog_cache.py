"""Short-lived, server-side cache for parsed Registry catalog manifests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import Any, Generic, TypeVar

from ifx_registry.domain.models import DatasetId

Snapshot = TypeVar("Snapshot")


class CachedCatalog(Generic[Snapshot]):
    """Cache list reads while keeping exact snapshot reads authoritative."""

    def __init__(
        self,
        list_all: Callable[[], tuple[Snapshot, ...]],
        get: Callable[[DatasetId, str], Snapshot],
        *,
        ttl_seconds: int = 300,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._list_all = list_all
        self._get = get
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._value: tuple[Snapshot, ...] | None = None
        self._loaded_at = 0.0
        self._loaded_at_utc: datetime | None = None

    @property
    def loaded_at(self) -> datetime | None:
        return self._loaded_at_utc

    def list_all(self) -> tuple[Snapshot, ...]:
        with self._lock:
            if self._value is None or self._clock() - self._loaded_at >= self._ttl_seconds:
                self._load()
            assert self._value is not None
            return self._value

    def refresh(self) -> None:
        with self._lock:
            self._load()

    def get(self, dataset: DatasetId, version: str) -> Snapshot:
        return self._get(dataset, version)

    def _load(self) -> None:
        # A failed reload must not leave an expired value serving as fresh data.
        self._value = None
        self._loaded_at_utc = None
        value = self._list_all()
        self._value = value
        self._loaded_at = self._clock()
        self._loaded_at_utc = datetime.now(UTC)


class WebCatalogCache:
    def __init__(self, *catalogs: CachedCatalog[Any]) -> None:
        self._catalogs = catalogs

    @property
    def loaded_at(self) -> datetime | None:
        loaded = tuple(
            loaded_at
            for catalog in self._catalogs
            if (loaded_at := catalog.loaded_at) is not None
        )
        return min(loaded) if loaded and len(loaded) == len(self._catalogs) else None

    def refresh(self) -> None:
        for catalog in self._catalogs:
            catalog.refresh()
