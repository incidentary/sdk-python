"""Fixed-allocation ring buffer for skeleton causal events."""

from __future__ import annotations

import threading
import time
from typing import List, Optional

from .types import SkeletonCe


class RingBuffer:
    """
    Fixed-size circular buffer. Pre-allocated. O(1) write, O(n) flush.
    When full, overwrites oldest entries.
    """

    def __init__(self, capacity: int = 4_000, window_ms: int = 60_000):
        self._capacity = capacity
        self._window_ms = window_ms
        self._slots: List[Optional[SkeletonCe]] = [None] * capacity
        self._head = 0
        self._count = 0
        self._lock = threading.Lock()

    def write(self, ce: SkeletonCe) -> None:
        with self._lock:
            self._slots[self._head] = ce
            self._head = (self._head + 1) % self._capacity
            if self._count < self._capacity:
                self._count += 1

    def flush(self, window_ms: Optional[int] = None) -> List[SkeletonCe]:
        with self._lock:
            w = window_ms if window_ms is not None else self._window_ms
            cutoff_ns = (int(time.time() * 1000) - w) * 1_000_000

            n = min(self._count, self._capacity)
            result: List[SkeletonCe] = []
            for i in range(n):
                idx = (self._head - n + i + self._capacity) % self._capacity
                slot = self._slots[idx]
                if slot is not None and slot.wall_ts_ns >= cutoff_ns:
                    result.append(slot)

            self._clear_unlocked()
        result.sort(key=lambda ce: (ce.wall_ts_ns, ce.ce_id))
        return result

    def clear(self) -> None:
        with self._lock:
            self._clear_unlocked()

    def _clear_unlocked(self) -> None:
        for i in range(self._capacity):
            self._slots[i] = None
        self._head = 0
        self._count = 0

    @property
    def size(self) -> int:
        with self._lock:
            return self._count
