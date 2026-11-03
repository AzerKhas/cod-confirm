"""In-memory sliding-window rate limiter.

Good for a single app instance (the recommended deployment). For several instances, put
the same limits in the reverse proxy or move the counters to Redis.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request, status


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_seconds: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def check(self, key: str, limit: int, window_seconds: float = 60.0) -> None:
        if not self.allow(key, limit, window_seconds):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too many requests",
                headers={"Retry-After": str(int(window_seconds))},
            )

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers sets request.client from X-Forwarded-For behind the proxy.
    return request.client.host if request.client else "unknown"
