"""Tests for fetcher integration with the runner (timeout / hedged mode)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from hermes_market import fetcher as fetcher_mod
from hermes_market.fetcher import MarketDataFetcher
from hermes_market.models import FetchResult


class _Sentinel:
    pass


@pytest.fixture(autouse=True)
def _isolate_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetcher_mod.akshare_provider, "load_module", lambda: _Sentinel())
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "load_module", lambda: _Sentinel())
    monkeypatch.setattr(fetcher_mod.baostock_provider, "load_module", lambda: _Sentinel())
    monkeypatch.setattr(fetcher_mod.stooq_provider, "load_module", lambda: _Sentinel())
    monkeypatch.setattr(fetcher_mod, "XueqiuClient", lambda: _Sentinel())


def _ok(provider: str, sym: str, mkt: str) -> FetchResult:
    return FetchResult(True, provider, sym, mkt, {"last": 1.0})


def test_quote_per_provider_timeout_skips_hanging_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hanging akshare should not block the chain past per_provider_timeout."""

    def _hang(ak, sym):  # type: ignore[no-untyped-def]
        time.sleep(0.5)
        return _ok("akshare", sym, "cn")

    def _fast(yf, sym, mkt):  # type: ignore[no-untyped-def]
        return _ok("yfinance", sym, mkt)

    monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", _hang)
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "quote", _fast)

    f = MarketDataFetcher(provider_timeout=0.1, global_deadline=5.0)
    t0 = time.monotonic()
    r = f.quote("600519", "cn")
    elapsed = time.monotonic() - t0

    assert r.ok and r.provider == "yfinance"
    assert elapsed < 0.4  # nowhere near akshare's 0.5s sleep


def test_quote_hedged_mode_lets_secondary_overtake(monkeypatch: pytest.MonkeyPatch) -> None:
    """With hedge_delay enabled, a slow primary lets a faster secondary win."""

    def _slow(ak, sym):  # type: ignore[no-untyped-def]
        time.sleep(0.4)
        return _ok("akshare", sym, "cn")

    def _fast(yf, sym, mkt):  # type: ignore[no-untyped-def]
        time.sleep(0.05)
        return _ok("yfinance", sym, mkt)

    monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", _slow)
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "quote", _fast)

    f = MarketDataFetcher(provider_timeout=2.0, global_deadline=5.0, hedge_delay=0.1)
    t0 = time.monotonic()
    r = f.quote("600519", "cn")
    elapsed = time.monotonic() - t0

    assert r.ok and r.provider == "yfinance"
    assert elapsed < 0.5  # well under the primary's 0.4s sleep


def test_quote_total_failure_aggregates_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> FetchResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", _boom)
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "quote", _boom)
    monkeypatch.setattr(fetcher_mod.xueqiu_provider, "quote", _boom)
    monkeypatch.setattr(fetcher_mod.baostock_provider, "quote", _boom)
    monkeypatch.setattr(fetcher_mod.stooq_provider, "quote", _boom)

    f = MarketDataFetcher(provider_timeout=1.0, global_deadline=3.0)
    r = f.quote("600519", "cn")
    assert not r.ok
    providers_attempted = {e["provider"] for e in r.errors}
    assert {"akshare", "yfinance", "xueqiu", "baostock", "stooq"}.issubset(providers_attempted)
