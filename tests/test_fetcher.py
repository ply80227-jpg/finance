"""Tests for the fetcher's fallback ordering using mocked providers.

We monkeypatch the provider modules referenced by :mod:`hermes_market.fetcher`
to avoid hitting any network during unit tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes_market import fetcher as fetcher_mod
from hermes_market.fetcher import MarketDataFetcher
from hermes_market.models import FetchResult


class _Sentinel:
    """Truthy placeholder used in place of the optional 3rd-party modules."""


@pytest.fixture(autouse=True)
def _isolate_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the fetcher to think every optional dep is installed; individual
    # tests then patch the provider call sites to control behaviour.
    monkeypatch.setattr(fetcher_mod.akshare_provider, "load_module", lambda: _Sentinel())
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "load_module", lambda: _Sentinel())
    monkeypatch.setattr(fetcher_mod.baostock_provider, "load_module", lambda: _Sentinel())
    monkeypatch.setattr(fetcher_mod.stooq_provider, "load_module", lambda: _Sentinel())
    # XueqiuClient must not bootstrap a real cookie during tests.
    monkeypatch.setattr(fetcher_mod, "XueqiuClient", lambda: _Sentinel())


def _ok(provider: str, symbol: str, market: str, data: dict[str, Any] | None = None) -> FetchResult:
    return FetchResult(True, provider, symbol, market, data or {"ok": True})


def test_quote_primary_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def _ak_cn(ak, sym):  # type: ignore[no-untyped-def]
        called.append("akshare")
        return _ok("akshare", sym, "cn", {"last": 1688.0})

    monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", _ak_cn)

    f = MarketDataFetcher()
    r = f.quote("600519", "cn")
    assert r.ok and r.provider == "akshare"
    assert called == ["akshare"]


def test_quote_falls_back_through_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def _ak_cn(ak, sym):  # type: ignore[no-untyped-def]
        called.append("akshare")
        raise RuntimeError("ak boom")

    def _yf_quote(yf, sym, mkt):  # type: ignore[no-untyped-def]
        called.append("yfinance")
        raise RuntimeError("yf boom")

    def _xq_quote(client, sym, mkt):
        called.append("xueqiu")
        return _ok("xueqiu", sym, mkt, {"last": 42.0})

    monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", _ak_cn)
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "quote", _yf_quote)
    monkeypatch.setattr(fetcher_mod.xueqiu_provider, "quote", _xq_quote)

    f = MarketDataFetcher()
    r = f.quote("600519", "cn")
    assert r.ok and r.provider == "xueqiu"
    assert called == ["akshare", "yfinance", "xueqiu"]


def test_quote_all_fail_returns_structured_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> FetchResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", _boom)
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "quote", _boom)
    monkeypatch.setattr(fetcher_mod.xueqiu_provider, "quote", _boom)
    monkeypatch.setattr(fetcher_mod.baostock_provider, "quote", _boom)
    monkeypatch.setattr(fetcher_mod.stooq_provider, "quote", _boom)

    f = MarketDataFetcher()
    r = f.quote("600519", "cn")
    assert not r.ok
    providers_attempted = {e["provider"] for e in r.errors}
    assert {"akshare", "yfinance", "xueqiu", "baostock", "stooq"}.issubset(providers_attempted)


def test_baostock_skipped_for_hk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: baostock must never be tried for HK."""

    def _boom(*args: Any, **kwargs: Any) -> FetchResult:
        raise RuntimeError("boom")

    bs_calls: list[int] = []

    def _bs_quote(bs, sym, mkt):  # type: ignore[no-untyped-def]
        bs_calls.append(1)
        raise RuntimeError("should not be called")

    monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_hk", _boom)
    monkeypatch.setattr(fetcher_mod.yfinance_provider, "quote", _boom)
    monkeypatch.setattr(fetcher_mod.xueqiu_provider, "quote", _boom)
    monkeypatch.setattr(fetcher_mod.baostock_provider, "quote", _bs_quote)
    monkeypatch.setattr(fetcher_mod.stooq_provider, "quote", _boom)

    f = MarketDataFetcher()
    r = f.quote("00700", "hk")
    assert not r.ok
    assert bs_calls == []  # baostock never invoked
    # And no baostock entry is recorded in errors for HK.
    providers_attempted = {e["provider"] for e in r.errors}
    assert "baostock" not in providers_attempted


def test_news_hk_with_symbol_prefers_xueqiu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for the original 1.5 bug: HK+symbol must not start at akshare."""

    order: list[str] = []

    def _ak_news(*args: Any, **kwargs: Any) -> FetchResult:
        order.append("akshare")
        raise RuntimeError("ak boom")

    def _xq_news(client, limit, sym, mkt):
        order.append("xueqiu")
        return _ok("xueqiu", sym or "", mkt, {"news": [{"title": "x"}]})

    monkeypatch.setattr(fetcher_mod.akshare_provider, "news", _ak_news)
    monkeypatch.setattr(fetcher_mod.xueqiu_provider, "news", _xq_news)

    f = MarketDataFetcher()
    r = f.news(limit=5, symbol="00700", market="hk")
    assert r.ok and r.provider == "xueqiu"
    assert order[0] == "xueqiu"  # xueqiu first, akshare not even consulted
    assert "akshare" not in order


def test_news_cn_falls_back_to_sina(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> FetchResult:
        raise RuntimeError("boom")

    def _sina(limit, symbol):  # type: ignore[no-untyped-def]
        return _ok("sina_rss", symbol or "", "global", {"news": [{"title": "fall"}]})

    monkeypatch.setattr(fetcher_mod.akshare_provider, "news", _boom)
    monkeypatch.setattr(fetcher_mod.xueqiu_provider, "news", _boom)
    monkeypatch.setattr(fetcher_mod.sina_rss, "news", _sina)

    f = MarketDataFetcher()
    r = f.news(limit=5, symbol="600519", market="cn")
    assert r.ok and r.provider == "sina_rss"
