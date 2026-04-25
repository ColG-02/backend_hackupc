"""
In-process rate limiter for crew GPS ingestion.

Uses monotonic time so it is unaffected by clock adjustments.
Works correctly for a single-process deployment.
"""

import time
from collections import OrderedDict

_MAX_ENTRIES = 10_000  # evict oldest entries beyond this to cap memory


class RateLimiter:
    def __init__(self) -> None:
        self._last: OrderedDict[str, float] = OrderedDict()

    def is_allowed(self, key: str, min_interval_sec: float) -> bool:
        """Return True and update the timestamp if the call is within rate limit."""
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and (now - last) < min_interval_sec:
            return False
        # Evict oldest entry when at capacity
        if key not in self._last and len(self._last) >= _MAX_ENTRIES:
            self._last.popitem(last=False)
        self._last[key] = now
        self._last.move_to_end(key)
        return True

    def remove(self, key: str) -> None:
        self._last.pop(key, None)


location_limiter = RateLimiter()
