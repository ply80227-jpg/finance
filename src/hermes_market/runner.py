"""Provider fallback runner with per-provider timeout and optional hedging.

The fetcher module composes a list of ``(provider_name, callable)`` attempts and
hands them to :func:`run_with_fallback`. Two modes are supported:

* **Sequential** (``hedge_delay`` is ``None`` — the default): each provider is
  given up to ``per_provider_timeout`` seconds; on timeout, exception, or
  exhausting the ``global_deadline`` budget we record an entry in ``errors``
  and try the next one. This matches the original synchronous behaviour but
  adds a hard upper bound on tail latency.

* **Hedged** (``hedge_delay`` is a positive float): the next provider in the
  list is spawned in parallel after the previous one has been waiting for
  ``hedge_delay`` seconds. Whichever future succeeds first wins; the others
  are best-effort cancelled. Useful for ``quote`` where the cost of an extra
  in-flight request is acceptable to shave off worst-case latency.

The runner returns either the winning :class:`~hermes_market.models.FetchResult`
or ``None`` plus a structured list of ``{provider, message}`` errors recording
every attempted hop.
"""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import time
from collections.abc import Callable, Iterable

from .models import FetchResult

Attempt = tuple[str, Callable[[], FetchResult]]


def _record(errors: list[dict[str, str]], name: str, message: str) -> None:
    errors.append({"provider": name, "message": message})


def _exc_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def run_with_fallback(
    attempts: Iterable[Attempt],
    *,
    per_provider_timeout: float = 6.0,
    global_deadline: float = 20.0,
    hedge_delay: float | None = None,
) -> tuple[FetchResult | None, list[dict[str, str]]]:
    """Run the provider attempts and return ``(result, errors)``.

    ``per_provider_timeout`` and ``global_deadline`` are both in seconds.

    ``hedge_delay`` controls hedging behaviour:

    * ``None`` — strict sequential, equivalent to the original behaviour but
      with per-provider timeout and a global deadline guard.
    * positive float — once the in-flight provider has been pending for
      ``hedge_delay`` seconds (without exceeding ``per_provider_timeout``), the
      next provider in the list is also spawned. The first successful future
      wins and the rest are cancelled.
    """

    attempts_list = list(attempts)
    if not attempts_list:
        return None, []

    if hedge_delay is None:
        return _run_sequential(
            attempts_list,
            per_provider_timeout=per_provider_timeout,
            global_deadline=global_deadline,
        )
    if hedge_delay <= 0:
        raise ValueError("hedge_delay must be positive when set; pass None to disable")
    return _run_hedged(
        attempts_list,
        per_provider_timeout=per_provider_timeout,
        global_deadline=global_deadline,
        hedge_delay=hedge_delay,
    )


def _run_sequential(
    attempts: list[Attempt],
    *,
    per_provider_timeout: float,
    global_deadline: float,
) -> tuple[FetchResult | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    start = time.monotonic()
    for name, fn in attempts:
        elapsed = time.monotonic() - start
        remaining_budget = global_deadline - elapsed
        if remaining_budget <= 0:
            _record(errors, name, "global deadline exceeded before attempt")
            continue
        timeout = min(per_provider_timeout, remaining_budget)
        # A fresh single-worker executor per attempt — when a provider hangs we
        # abandon its thread (Python can't safely kill it) and move on without
        # blocking subsequent attempts behind the dead thread.
        ex = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"hermes-seq-{name}")
        try:
            fut = ex.submit(fn)
            try:
                result = fut.result(timeout=timeout)
            except cf.TimeoutError:
                _record(errors, name, f"timeout after {timeout:.2f}s")
                with contextlib.suppress(Exception):
                    fut.cancel()
                continue
            except BaseException as exc:  # noqa: BLE001
                _record(errors, name, _exc_message(exc))
                continue
            if isinstance(result, FetchResult) and result.ok:
                return result, errors
            # A FetchResult marked ``ok=False`` is treated as a soft fail.
            msg = result.error if isinstance(result, FetchResult) and result.error else "provider returned ok=False"
            _record(errors, name, msg)
        finally:
            # ``wait=False`` lets us walk away from a still-running hung worker.
            ex.shutdown(wait=False)
    return None, errors


def _run_hedged(
    attempts: list[Attempt],
    *,
    per_provider_timeout: float,
    global_deadline: float,
    hedge_delay: float,
) -> tuple[FetchResult | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    start = time.monotonic()
    deadline_at = start + global_deadline

    with cf.ThreadPoolExecutor(max_workers=max(2, len(attempts)), thread_name_prefix="hermes-hedge") as ex:
        idx = 0
        in_flight: dict[cf.Future, tuple[str, float]] = {}

        def _spawn_next() -> None:
            nonlocal idx
            if idx >= len(attempts):
                return
            name, fn = attempts[idx]
            idx += 1
            fut = ex.submit(fn)
            in_flight[fut] = (name, time.monotonic())

        _spawn_next()

        while in_flight:
            now = time.monotonic()
            if now >= deadline_at:
                # Burn the remaining futures; nothing succeeded in time.
                for f, (name, _) in in_flight.items():
                    _record(errors, name, "global deadline exceeded")
                    with contextlib.suppress(Exception):
                        f.cancel()
                in_flight.clear()
                break

            # Cap each future's wall-clock at per_provider_timeout.
            earliest_kill = min(spawn_t + per_provider_timeout for _, spawn_t in in_flight.values())
            next_hedge_at = (
                min(spawn_t + hedge_delay for _, spawn_t in in_flight.values()) if idx < len(attempts) else float("inf")
            )
            wake_at = min(deadline_at, earliest_kill, next_hedge_at)
            wait_for = max(wake_at - now, 0.0)

            done, _pending = cf.wait(list(in_flight), timeout=wait_for, return_when=cf.FIRST_COMPLETED)
            now = time.monotonic()

            for f in done:
                name, _ = in_flight.pop(f)
                try:
                    res = f.result()
                except BaseException as exc:  # noqa: BLE001
                    _record(errors, name, _exc_message(exc))
                    continue
                if isinstance(res, FetchResult) and res.ok:
                    for other in in_flight:
                        with contextlib.suppress(Exception):
                            other.cancel()
                    in_flight.clear()
                    return res, errors
                msg = res.error if isinstance(res, FetchResult) and res.error else "provider returned ok=False"
                _record(errors, name, msg)

            # Time-out the futures whose individual budget has elapsed.
            for f in list(in_flight):
                name, spawn_t = in_flight[f]
                if now - spawn_t >= per_provider_timeout:
                    _record(errors, name, f"timeout after {per_provider_timeout:.2f}s")
                    with contextlib.suppress(Exception):
                        f.cancel()
                    in_flight.pop(f, None)

            # If nothing is in flight and the hedge window passed (or all
            # attempts already spawned), launch the next attempt.
            if (not in_flight or now >= next_hedge_at) and idx < len(attempts):
                _spawn_next()

    return None, errors
