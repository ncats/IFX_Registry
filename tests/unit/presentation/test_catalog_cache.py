"""Web catalog caching keeps repeated page reads fast and refreshes explicitly."""

import pytest

from ifx_registry.presentation.web.catalog_cache import CachedCatalog


def test_catalog_cache_reloads_after_five_minutes_or_explicit_refresh() -> None:
    now = [0.0]
    reads = [0]

    def load() -> tuple[int, ...]:
        reads[0] += 1
        return (reads[0],)

    cache = CachedCatalog(load, lambda _dataset, _version: 0, clock=lambda: now[0])
    assert cache.list_all() == (1,)
    now[0] = 299.0
    assert cache.list_all() == (1,)
    cache.refresh()
    assert cache.list_all() == (2,)
    now[0] = 599.0
    assert cache.list_all() == (3,)


def test_failed_refresh_does_not_serve_stale_catalog() -> None:
    fail = [False]

    def load() -> tuple[str, ...]:
        if fail[0]:
            raise RuntimeError("S3 unavailable")
        return ("snapshot",)

    cache = CachedCatalog(load, lambda _dataset, _version: "snapshot")
    assert cache.list_all() == ("snapshot",)
    fail[0] = True
    with pytest.raises(RuntimeError, match="S3 unavailable"):
        cache.refresh()
    with pytest.raises(RuntimeError, match="S3 unavailable"):
        cache.list_all()
