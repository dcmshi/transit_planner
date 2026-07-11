"""
Rate limiting — per-IP sliding window on the public endpoints.

In-process state is sufficient: the app runs a single uvicorn worker
(APScheduler constraint, see README known limitations).  Behind a reverse
proxy every caller shares the proxy's IP — revisit with X-Forwarded-For
if the API is ever deployed behind one (tracked in TODO.md).
"""

import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from config import RATE_LIMIT_PER_MINUTE

_rate_buckets: dict[str, "deque[float]"] = {}
_rate_lock = threading.Lock()
_RATE_WINDOW_SECONDS = 60.0
_RATE_BUCKETS_MAX = 10_000


def _rate_limit(request: Request) -> None:
    """FastAPI dependency: reject with 429 when the caller's IP has made
    more than RATE_LIMIT_PER_MINUTE requests in the sliding window."""
    if RATE_LIMIT_PER_MINUTE <= 0:
        return
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_buckets.get(ip)
        if bucket is None:
            bucket = _rate_buckets[ip] = deque()
        while bucket and now - bucket[0] > _RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_PER_MINUTE:
            retry_after = max(1, int(_RATE_WINDOW_SECONDS - (now - bucket[0])) + 1)
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded — try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
        # Opportunistic cleanup: evict buckets whose newest entry has aged
        # out of the window (an idle IP's bucket is never popped by its own
        # requests, so "empty" is not a usable eviction signal).
        if len(_rate_buckets) > _RATE_BUCKETS_MAX:
            stale = [
                k for k, b in _rate_buckets.items()
                if not b or now - b[-1] > _RATE_WINDOW_SECONDS
            ]
            for key in stale:
                del _rate_buckets[key]
