"""Small thread-safe capacity and idle-time bounded object cache."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import MutableMapping


class BoundedTTLCache(MutableMapping):
    """LRU cache whose entries expire after an idle interval.

    Callers receive a normal strong reference from ``get``/``__getitem__``.
    Eviction only removes the cache's reference, so an in-flight request remains
    safe while idle user runtimes can be reclaimed.
    """

    def __init__(self, capacity: int, ttl_seconds: float, on_evict=None,
                 clock=time.monotonic):
        if int(capacity) <= 0 or float(ttl_seconds) <= 0:
            raise ValueError("缓存容量和闲置时间必须大于 0")
        self.capacity = int(capacity)
        self.ttl_seconds = float(ttl_seconds)
        self._on_evict = on_evict
        self._clock = clock
        self._items = OrderedDict()
        self._lock = threading.RLock()

    def _discard(self, key):
        entry = self._items.pop(key, None)
        if entry is not None and self._on_evict:
            try:
                self._on_evict(key, entry[1])
            except Exception:
                pass

    def sweep(self):
        with self._lock:
            cutoff = self._clock() - self.ttl_seconds
            expired = [key for key, (used, _value) in self._items.items()
                       if used <= cutoff]
            for key in expired:
                self._discard(key)
            return len(expired)

    def __getitem__(self, key):
        with self._lock:
            self.sweep()
            used, value = self._items[key]
            self._items[key] = (self._clock(), value)
            self._items.move_to_end(key)
            return value

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key, value):
        with self._lock:
            self.sweep()
            if key in self._items:
                self._discard(key)
            self._items[key] = (self._clock(), value)
            while len(self._items) > self.capacity:
                oldest = next(iter(self._items))
                self._discard(oldest)

    def __delitem__(self, key):
        with self._lock:
            if key not in self._items:
                raise KeyError(key)
            self._discard(key)

    def pop(self, key, default=None):
        with self._lock:
            if key not in self._items:
                return default
            _used, value = self._items[key]
            self._discard(key)
            return value

    def __iter__(self):
        with self._lock:
            self.sweep()
            return iter(list(self._items.keys()))

    def __len__(self):
        with self._lock:
            self.sweep()
            return len(self._items)

    def clear(self):
        with self._lock:
            for key in list(self._items):
                self._discard(key)

    def keys_snapshot(self):
        with self._lock:
            self.sweep()
            return list(self._items.keys())
