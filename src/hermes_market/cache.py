"""Lightweight caches: in-process TTL cache + persistent JSON cache.

Used to:

* Cache akshare's market-wide spot DataFrames briefly so repeated quote
  invocations within a few seconds reuse the same payload.
* Persist the Xueqiu cookie to disk so a fresh CLI invocation does not need to
  re-bootstrap from ``https://xueqiu.com/`` every time.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any


def cache_dir() -> Path:
    """Return the per-user cache directory used by the package."""

    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    p = Path(base) / "hermes_market"
    p.mkdir(parents=True, exist_ok=True)
    return p


class TTLCache:
    """Trivial in-memory TTL cache.

    Not thread-safe by design — this script is invoked as a one-shot CLI per
    request, so concurrency is bounded.
    """

    def __init__(self, ttl_seconds: float = 8.0) -> None:
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic() + self.ttl, value)


def load_json(name: str) -> dict[str, Any] | None:
    """Read a JSON blob from the package cache directory. ``None`` if missing/corrupt."""

    p = cache_dir() / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def store_json(name: str, payload: dict[str, Any]) -> None:
    """Best-effort write of ``payload`` into the package cache directory."""

    with contextlib.suppress(OSError):
        (cache_dir() / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
