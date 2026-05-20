"""Tests for the :mod:`hermes_market.runner` fallback runner.

These tests exercise the runner directly with simple callables — no provider
modules are involved — so behaviour is deterministic and fast.
"""

from __future__ import annotations

import time

import pytest

from hermes_market.models import FetchResult
from hermes_market.runner import run_with_fallback


def _ok(provider: str) -> FetchResult:
    return FetchResult(True, provider, "X", "cn", {"provider": provider})


# -------------------------------------------------------------------- sequential


def test_sequential_returns_first_success() -> None:
    calls: list[str] = []

    def _a() -> FetchResult:
        calls.append("a")
        return _ok("a")

    def _b() -> FetchResult:
        calls.append("b")
        return _ok("b")

    result, errors = run_with_fallback(
        [("a", _a), ("b", _b)],
        per_provider_timeout=2.0,
        global_deadline=5.0,
    )
    assert result is not None and result.provider == "a"
    assert calls == ["a"]
    assert errors == []


def test_sequential_falls_through_on_exception() -> None:
    def _bad() -> FetchResult:
        raise RuntimeError("nope")

    def _good() -> FetchResult:
        return _ok("good")

    result, errors = run_with_fallback(
        [("bad", _bad), ("good", _good)],
        per_provider_timeout=2.0,
        global_deadline=5.0,
    )
    assert result is not None and result.provider == "good"
    assert [e["provider"] for e in errors] == ["bad"]
    assert "nope" in errors[0]["message"]


def test_sequential_per_provider_timeout_skips_slow_provider() -> None:
    def _slow() -> FetchResult:
        time.sleep(0.5)
        return _ok("slow")

    def _fast() -> FetchResult:
        return _ok("fast")

    t0 = time.monotonic()
    result, errors = run_with_fallback(
        [("slow", _slow), ("fast", _fast)],
        per_provider_timeout=0.1,
        global_deadline=5.0,
    )
    elapsed = time.monotonic() - t0
    assert result is not None and result.provider == "fast"
    assert [e["provider"] for e in errors] == ["slow"]
    assert "timeout" in errors[0]["message"]
    # Should be well under the slow provider's real runtime.
    assert elapsed < 0.4


def test_sequential_global_deadline_cuts_off() -> None:
    def _slow() -> FetchResult:
        time.sleep(0.5)
        return _ok("slow")

    t0 = time.monotonic()
    result, errors = run_with_fallback(
        [("slow1", _slow), ("slow2", _slow), ("slow3", _slow)],
        per_provider_timeout=0.2,
        global_deadline=0.3,
    )
    elapsed = time.monotonic() - t0
    assert result is None
    # We tried the first one, time-out'd it, then the deadline kicked in for the rest.
    providers_errored = [e["provider"] for e in errors]
    assert providers_errored[0] == "slow1"
    assert any("deadline" in e["message"] for e in errors[1:])
    assert elapsed < 1.0  # nowhere near 3 * 0.5


def test_sequential_treats_ok_false_result_as_soft_fail() -> None:
    def _soft_fail() -> FetchResult:
        return FetchResult(False, "soft", "X", "cn", {}, error="bad data")

    def _good() -> FetchResult:
        return _ok("good")

    result, errors = run_with_fallback(
        [("soft", _soft_fail), ("good", _good)],
        per_provider_timeout=1.0,
        global_deadline=3.0,
    )
    assert result is not None and result.provider == "good"
    assert errors[0]["provider"] == "soft"
    assert "bad data" in errors[0]["message"]


def test_empty_attempts_returns_none() -> None:
    result, errors = run_with_fallback([])
    assert result is None
    assert errors == []


# ------------------------------------------------------------------------ hedged


def test_hedged_fast_secondary_wins_when_primary_slow() -> None:
    """Primary takes 0.4s; secondary spawned after 0.1s and finishes in 0.05s."""

    def _slow() -> FetchResult:
        time.sleep(0.4)
        return _ok("slow")

    def _fast() -> FetchResult:
        time.sleep(0.05)
        return _ok("fast")

    t0 = time.monotonic()
    result, _errors = run_with_fallback(
        [("slow", _slow), ("fast", _fast)],
        per_provider_timeout=2.0,
        global_deadline=5.0,
        hedge_delay=0.1,
    )
    elapsed = time.monotonic() - t0
    assert result is not None and result.provider == "fast"
    # Fast wins shortly after the hedge fires (0.1s + 0.05s + scheduler slack)
    assert elapsed < 0.5


def test_hedged_primary_returns_before_hedge_fires() -> None:
    """Primary returns in 0.05s, well before the 0.5s hedge delay — secondary never starts."""

    calls: list[str] = []

    def _primary() -> FetchResult:
        time.sleep(0.05)
        calls.append("primary")
        return _ok("primary")

    def _secondary() -> FetchResult:
        calls.append("secondary")
        return _ok("secondary")

    result, _errors = run_with_fallback(
        [("primary", _primary), ("secondary", _secondary)],
        per_provider_timeout=2.0,
        global_deadline=5.0,
        hedge_delay=0.5,
    )
    assert result is not None and result.provider == "primary"
    assert "secondary" not in calls


def test_hedged_rejects_non_positive_delay() -> None:
    with pytest.raises(ValueError):
        run_with_fallback([("a", lambda: _ok("a"))], hedge_delay=0)
    with pytest.raises(ValueError):
        run_with_fallback([("a", lambda: _ok("a"))], hedge_delay=-1.0)


def test_hedged_returns_before_slow_loser_finishes() -> None:
    """The caller must see the fastest provider's latency, NOT the slowest.

    Regression for a bug where ``with ThreadPoolExecutor:`` would call
    ``shutdown(wait=True)`` on return, blocking the caller on the still-running
    slow provider and erasing the latency gain from hedging.
    """

    def _slow_loser() -> FetchResult:
        # Significantly slower than the per_provider_timeout in the test, but
        # the test must still return before this sleep finishes.
        time.sleep(2.0)
        return _ok("slow")

    def _fast_winner() -> FetchResult:
        time.sleep(0.05)
        return _ok("fast")

    t0 = time.monotonic()
    result, _errors = run_with_fallback(
        [("slow", _slow_loser), ("fast", _fast_winner)],
        per_provider_timeout=5.0,
        global_deadline=10.0,
        hedge_delay=0.1,
    )
    elapsed = time.monotonic() - t0
    assert result is not None and result.provider == "fast"
    # Was ~2s under the old (broken) shutdown(wait=True) behaviour.
    assert elapsed < 0.6, f"hedged caller blocked on slow loser: {elapsed:.2f}s"
