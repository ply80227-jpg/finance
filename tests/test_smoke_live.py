"""Live smoke tests against real third-party providers.

These tests are **skipped by default** and are not part of CI's required
checks. They are wired up purely so a human can run them locally to verify
the fallback chain still works end-to-end against the real Internet.

Enable them by setting the environment variable ``HERMES_RUN_LIVE=1``::

    HERMES_RUN_LIVE=1 pytest tests/test_smoke_live.py -v

The tests *deliberately* tolerate provider-level failures: the contract is
"at least one provider succeeds", not that any particular provider does.
Treat individual provider failures as "expected" and only treat a 5-of-5
chain failure as a real bug worth a PR.
"""

from __future__ import annotations

import os

import pytest

from hermes_market.fetcher import MarketDataFetcher
from hermes_market.providers import stooq_provider

LIVE_FLAG = pytest.mark.skipif(
    not os.environ.get("HERMES_RUN_LIVE"),
    reason="Live network test; set HERMES_RUN_LIVE=1 to enable.",
)


@LIVE_FLAG
def test_live_quote_cn_serial() -> None:
    f = MarketDataFetcher(provider_timeout=4.0, global_deadline=15.0, hedge_delay=None)
    result = f.quote("600519")
    assert result.ok, f"all providers failed: {result.errors}"
    assert isinstance(result.data.get("last"), (int, float))


@LIVE_FLAG
def test_live_quote_cn_hedged() -> None:
    f = MarketDataFetcher(provider_timeout=4.0, global_deadline=15.0, hedge_delay=0.5)
    result = f.quote("600519")
    assert result.ok, f"all providers failed: {result.errors}"
    assert isinstance(result.data.get("last"), (int, float))


@LIVE_FLAG
def test_live_quote_hk() -> None:
    f = MarketDataFetcher(provider_timeout=4.0, global_deadline=15.0, hedge_delay=0.5)
    result = f.quote("00700")
    assert result.ok, f"all providers failed: {result.errors}"
    assert isinstance(result.data.get("last"), (int, float))


@LIVE_FLAG
def test_live_history_cn() -> None:
    f = MarketDataFetcher(provider_timeout=4.0, global_deadline=20.0, hedge_delay=0.5)
    result = f.history("600519", "2026-05-01", "2026-05-19")
    assert result.ok, f"all providers failed: {result.errors}"
    bars = result.data.get("bars") or []
    assert len(bars) > 0


@LIVE_FLAG
def test_live_news_cn() -> None:
    f = MarketDataFetcher(provider_timeout=4.0, global_deadline=15.0, hedge_delay=0.5)
    result = f.news()
    # Even if no headline endpoint works, the contract is "return a list,
    # possibly empty"; ok=False is acceptable if all providers refused.
    assert isinstance(result.data.get("items", []), list)


@LIVE_FLAG
def test_live_quote_with_fundamentals_cn() -> None:
    """Verify the fundamentals side-channel populates at least PE or PB for an A-share."""

    f = MarketDataFetcher(provider_timeout=4.0, global_deadline=15.0, hedge_delay=0.5)
    result = f.quote("600519", with_fundamentals=True)
    assert result.ok, f"price quote failed: {result.errors}"
    fund = result.data.get("fundamentals") or {}
    # Tolerate per-provider flakiness: the contract is "at least one of pe_ttm/pb/market_cap arrived".
    assert any(fund.get(k) is not None for k in ("pe_ttm", "pe_lyr", "pb", "market_cap")), (
        f"no valuation fields returned: fund={fund}, errors={result.data.get('fundamentals_errors')}"
    )
    assert fund.get("source") in {"xueqiu", "akshare_baidu"}


@LIVE_FLAG
def test_live_quote_with_fundamentals_hk() -> None:
    """HK fundamentals only have one usable free source (xueqiu); allow degraded result."""

    f = MarketDataFetcher(provider_timeout=4.0, global_deadline=15.0, hedge_delay=0.5)
    result = f.quote("00700", with_fundamentals=True)
    assert result.ok, f"price quote failed: {result.errors}"
    fund = result.data.get("fundamentals")
    if fund is None:
        # Acceptable on networks where xueqiu HK is blocked; we still must
        # not have broken the parent price quote.
        return
    assert fund["currency"] == "HKD"
    assert fund.get("source") == "xueqiu"


@LIVE_FLAG
def test_live_batch_quote() -> None:
    """Mixed CN+HK batch should return all 4 items in order, at least 1 ok."""

    f = MarketDataFetcher(provider_timeout=4.0, global_deadline=20.0, hedge_delay=0.5)
    syms = ["600519", "000001", "00700", "09988"]
    results = f.batch_quote(syms)
    assert [r.symbol for r in results] == syms
    assert any(r.ok for r in results), f"all 4 batch items failed: {[r.errors for r in results]}"


@LIVE_FLAG
def test_live_search_returns_matches() -> None:
    """Search for '茅台' should return at least one match (Maotai 600519)."""

    f = MarketDataFetcher(provider_timeout=4.0, global_deadline=15.0)
    rows = f.search("茅台", limit=5)
    # If the akshare index built successfully OR the xueqiu fallback works,
    # we expect at least 1 row. On a fully offline box this can be 0.
    if rows:
        assert any("茅台" in r.name for r in rows), [r.to_dict() for r in rows]


@LIVE_FLAG
def test_live_stooq_quote_direct_cn() -> None:
    """Hit the real Stooq CSV endpoint without going through the fallback chain.

    Verifies the HTTP-based rewrite actually works against production
    Stooq. Tolerates "N/D" results (out-of-hours or symbol not on Stooq)
    by allowing the test to xfail rather than fail outright.
    """

    try:
        result = stooq_provider.quote(stooq_provider.load_module(), "600519", "cn")
    except ValueError as exc:
        pytest.xfail(f"Stooq returned no data for 600519.cn: {exc}")
        return
    assert result.ok is True
    assert result.provider == "stooq"
    assert isinstance(result.data.get("last"), (int, float))


@LIVE_FLAG
def test_live_stooq_quote_direct_hk() -> None:
    """Verify the HK short-form ticker (``700.hk`` not ``0700.hk``) actually
    resolves on Stooq.

    Regression: the previous pandas-datareader path would silently fall
    back to the old Yahoo-style 4-digit format and Stooq returned N/D
    for every HK symbol.
    """

    try:
        result = stooq_provider.quote(stooq_provider.load_module(), "00700", "hk")
    except ValueError as exc:
        pytest.xfail(f"Stooq returned no data for 700.hk: {exc}")
        return
    assert result.ok is True
    assert result.provider == "stooq"
    assert isinstance(result.data.get("last"), (int, float))
