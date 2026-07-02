"""In-memory rate limiting for the public demo.

Single-process by design (Render free tier runs one instance) — no Redis needed.
Two shapes: a per-IP sliding window, and a global daily counter used to cap total
LLM spend regardless of how many IPs are hitting the service.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date
from typing import Deque, Dict

from fastapi import Request


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = max(1, int(limit))
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 5000:  # opportunistic GC of idle IPs
                for k in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
                    self._hits.pop(k, None)
            return True


class DailyCounter:
    """Global daily cap (resets at local midnight)."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._day = ""
        self._count = 0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        today = date.today().isoformat()
        with self._lock:
            if today != self._day:
                self._day, self._count = today, 0
            if self._count >= self.limit:
                return False
            self._count += 1
            return True


def client_ip(request: Request) -> str:
    """Client IP, honoring the reverse proxy (Render sets X-Forwarded-For)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
