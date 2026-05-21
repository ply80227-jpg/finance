"""Tests for ``MarketDataFetcher.batch_quote`` — uses monkeypatched ``quote``."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from hermes_market.fetcher import MarketDataFetcher
from hermes_market.models import FetchResult


def _ok(sym: str, market: str = "cn") -> FetchResult:
    return FetchResult(ok=True, provider="fake", symbol=sym, market=market, data={"last": 1.0})


def _new_fetcher(monkeypatch: pytest.MonkeyPatch) -> MarketDataFetcher:
    # Skip the real provider bootstrapping (akshare/baostock imports, xueqiu
    # cookie load) — these tests only exercise the orchestration in
    # ``batch_quote``.
    f = MarketDataFetcher.__new__(MarketDataFetcher)
    f.ak = None
    f.yf = None
    f.xq = None
    f.bs = None
    f.stooq = None
    f.provider_timeout = 1.0
    f.global_deadline = 5.0
    f.hedge_delay = None
    return f


class TestBatchQuote:
    def test_empty_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = _new_fetcher(monkeypatch)
        assert f.batch_quote([]) == []

    def test_preserves_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = _new_fetcher(monkeypatch)

        def fake_quote(
            self: Any,
            sym: str,
            market: str | None = None,
            *,
            with_fundamentals: bool = True,
        ) -> FetchResult:
            # Reverse the sleep order so iteration order is decoupled from
            # arrival order; output must still match input order.
            sleep_for = {"a": 0.05, "b": 0.0, "c": 0.02}.get(sym, 0)
            if sleep_for:
                time.sleep(sleep_for)
            return _ok(sym)

        monkeypatch.setattr(MarketDataFetcher, "quote", fake_quote)
        out = f.batch_quote(["a", "b", "c"])
        assert [r.symbol for r in out] == ["a", "b", "c"]

    def test_runs_concurrently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = _new_fetcher(monkeypatch)
        live = 0
        peak = 0
        lock = threading.Lock()

        def fake_quote(
            self: Any,
            sym: str,
            market: str | None = None,
            *,
            with_fundamentals: bool = True,
        ) -> FetchResult:
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.1)
            with lock:
                live -= 1
            return _ok(sym)

        monkeypatch.setattr(MarketDataFetcher, "quote", fake_quote)
        t0 = time.monotonic()
        out = f.batch_quote(["a", "b", "c", "d"])
        elapsed = time.monotonic() - t0
        assert len(out) == 4
        # 4 parallel sleeps of 0.1s ≈ 0.1s wall; serial would be ≈ 0.4s.
        assert elapsed < 0.35, f"batch ran serially: {elapsed:.2f}s"
        assert peak >= 2, f"only {peak} concurrent worker(s)"

    def test_partial_failure_does_not_abort_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = _new_fetcher(monkeypatch)

        def fake_quote(
            self: Any,
            sym: str,
            market: str | None = None,
            *,
            with_fundamentals: bool = True,
        ) -> FetchResult:
            if sym == "boom":
                raise RuntimeError("simulated provider blowup")
            return _ok(sym)

        monkeypatch.setattr(MarketDataFetcher, "quote", fake_quote)
        out = f.batch_quote(["ok1", "boom", "ok2"])
        assert [r.symbol for r in out] == ["ok1", "boom", "ok2"]
        assert out[0].ok is True
        assert out[1].ok is False
        assert "simulated provider blowup" in (out[1].error or "")
        assert out[2].ok is True
