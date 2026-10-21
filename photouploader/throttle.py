"""Shared upload bandwidth limiter (megabit/s) used by all parallel upload workers."""
import threading
import time

BLOCK = 16 * 1024


class Throttle:
    def __init__(self, mbps=0):
        self._lock = threading.Lock()
        self._next = 0.0
        self.mbps = mbps

    @property
    def mbps(self):
        return self._mbps

    @mbps.setter
    def mbps(self, value):
        self._mbps = max(float(value or 0), 0.0)  # 0 = unlimited

    def acquire(self, nbytes):
        rate = self._mbps * 1_000_000 / 8  # bytes per second
        if rate <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + nbytes / rate
        delay = start - now
        if delay > 0:
            time.sleep(delay)


class ThrottledBody:
    """File-like request body that paces reads through a Throttle; requests reads it in blocks."""

    def __init__(self, data, throttle):
        self._view, self._throttle, self._pos = memoryview(data), throttle, 0

    def __len__(self):
        return len(self._view)

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._view)
        size = min(size, BLOCK)
        chunk = self._view[self._pos:self._pos + size].tobytes()
        self._pos += len(chunk)
        if chunk:
            self._throttle.acquire(len(chunk))
        return chunk
