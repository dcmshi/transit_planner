"""
Route cache — keyed by (origin, destination, YYYY-MM-DD, HH:MM).

Caches raw find_routes() output (legs only); risk scoring is always fresh.
Protected by a lock so concurrent requests don't duplicate find_routes()
work.  Empty results are cached too (shorter TTL) so repeated queries for
unroutable pairs don't re-run Yen's every time, and the cache is bounded:
expired entries are otherwise only evicted when their exact key is looked
up again, so unique keys would accumulate until the daily clear.

In-process state is correct for the single-worker deployment (see README
known limitations).
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_routes_cache: dict[tuple[str, str, str, str], tuple[list, datetime, timedelta]] = {}
_routes_cache_lock = threading.Lock()
_ROUTES_CACHE_TTL = timedelta(hours=1)
_ROUTES_CACHE_NEGATIVE_TTL = timedelta(minutes=5)
_ROUTES_CACHE_MAX_ENTRIES = 1000

# Per-key in-flight locks (single-flight): concurrent requests for the same
# cache key wait for the first one's find_routes() instead of recomputing.
_inflight_locks: dict[tuple[str, str, str, str], threading.Lock] = {}


def _inflight_lock_for(key: tuple[str, str, str, str]) -> threading.Lock:
    with _routes_cache_lock:
        lock = _inflight_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _inflight_locks[key] = lock
        return lock


def _release_inflight_lock(key: tuple[str, str, str, str]) -> None:
    """Drop the per-key lock once computation finishes so the dict stays
    bounded; waiters still hold their reference to the lock object."""
    with _routes_cache_lock:
        _inflight_locks.pop(key, None)


def _routes_cache_key(origin: str, destination: str, departure_dt: datetime) -> tuple[str, str, str, str]:
    """Stable cache key at minute resolution."""
    return (origin, destination, departure_dt.strftime("%Y-%m-%d"), departure_dt.strftime("%H:%M"))


def _get_cached_routes(key: tuple[str, str, str, str]) -> list | None:
    """Cached routes for key, or None on miss/expiry.  An empty list is a
    negative-cache hit ('known unroutable'), distinct from None."""
    with _routes_cache_lock:
        entry = _routes_cache.get(key)
        if entry is None:
            return None
        cached_routes, cached_at, ttl = entry
        if datetime.now(timezone.utc) - cached_at > ttl:
            del _routes_cache[key]
            return None
        return cached_routes


def _store_cached_routes(key: tuple[str, str, str, str], routes: list) -> None:
    ttl = _ROUTES_CACHE_TTL if routes else _ROUTES_CACHE_NEGATIVE_TTL
    with _routes_cache_lock:
        _routes_cache[key] = (routes, datetime.now(timezone.utc), ttl)
        if len(_routes_cache) > _ROUTES_CACHE_MAX_ENTRIES:
            # Evict the oldest ~10% by insertion time.
            oldest = sorted(_routes_cache.items(), key=lambda kv: kv[1][1])
            for evict_key, _ in oldest[: max(1, _ROUTES_CACHE_MAX_ENTRIES // 10)]:
                del _routes_cache[evict_key]


def _clear_routes_cache() -> None:
    with _routes_cache_lock:
        _routes_cache.clear()
    logger.info("Route cache cleared.")
