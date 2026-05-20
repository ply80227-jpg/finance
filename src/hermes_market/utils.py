"""Small generic helpers (float coercion, timestamps, retry)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

T = TypeVar("T")


def to_float(value: Any) -> float | None:
    """Best-effort coercion to ``float``. Returns ``None`` on failure / ``None`` input."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def utc_now_iso() -> str:
    """Return a UTC ISO-8601 timestamp ending in ``Z`` (timezone-aware)."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pct_change(last: float | None, prev: float | None) -> float | None:
    """Percentage change between ``last`` and ``prev``. ``None`` if either is missing or prev==0."""

    if last is None or prev in (None, 0):
        return None
    return (last - prev) / prev * 100  # type: ignore[operator]


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 2,
    base_delay: float = 0.4,
    factor: float = 2.0,
    catch: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Call ``fn`` with exponential backoff. Raises the last exception on failure."""

    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last_exc: BaseException | None = None
    delay = base_delay
    for i in range(attempts):
        try:
            return fn()
        except catch as exc:  # noqa: PERF203 - intentional retry loop
            last_exc = exc
            if i == attempts - 1:
                break
            time.sleep(delay)
            delay *= factor
    assert last_exc is not None
    raise last_exc
