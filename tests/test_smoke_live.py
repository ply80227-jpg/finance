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
