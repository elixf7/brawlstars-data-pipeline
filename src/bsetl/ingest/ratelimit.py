"""Global request pacing for the Brawl Stars API.

Separated from the crawler so the pacing policy can be tested and tuned
without touching crawl logic.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque


class AsyncRateLimiter:
    """
    Simple global requests-per-second limiter with shared 429 backoff.
    Ensures at most `requests_per_second` acquisitions within any 1s window.
    A 429 triggers a global cooldown so all tasks pause before retrying.
    """
    def __init__(self, requests_per_second: float):
        self.requests_per_second = max(float(requests_per_second or 1.0), 0.1)
        self._events = deque()
        self._lock = asyncio.Lock()
        self._backoff_until = 0.0

    async def acquire(self):
        while True:
            # Respect global backoff if active
            now = time.monotonic()
            if now < self._backoff_until:
                await asyncio.sleep(self._backoff_until - now)

            async with self._lock:
                now = time.monotonic()
                # Drop timestamps older than 1s window
                while self._events and (now - self._events[0]) >= 1.0:
                    self._events.popleft()
                if len(self._events) < self.requests_per_second:
                    self._events.append(now)
                    return
                # Need to wait until the oldest acquisition falls out of the 1s window
                earliest = self._events[0]
                sleep_for = max(0.0, (earliest + 1.0) - now)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    def trigger_backoff(self, seconds: float):
        target = time.monotonic() + max(0.0, float(seconds))
        # Extend backoff if longer than current
        if target > self._backoff_until:
            self._backoff_until = target
