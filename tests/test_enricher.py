"""Tests for the fundamentals enricher (PE/PB/market_cap)."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from hermes_market import enricher as enricher_mod
from hermes_market import fetcher as fetcher_mod
from hermes_market.enricher import enrich_fundamentals
from hermes_market.fetcher import MarketDataFetcher
from hermes_market.models import FetchResult


class _FakeXq:
    """Minimal xueqiu stand-in returning whatever ``quote`` payload we wire in."""

    def __init__(self, payload: dict[str, Any], delay: float = 0.0) -> None:
        self.payload = payload
        self.delay = delay
        self.calls: list[str] = []

    def quote(self, symbol: str) -> dict[str, Any]:
        self.calls.append(symbol)
        if self.delay:
            time.sleep(self.delay)
        return self.payload


class _FakeAk:
    """Fake akshare module exposing a controllable ``stock_zh_valuation_baidu``."""

    def __init__(self, values: dict[str, float | None], delay: float = 0.0) -> None:
        self.values = values
        self.delay = delay
        self.calls: list[tuple[str, str]] = []

    def stock_zh_valuation_baidu(self, *, symbol: str, indicator: str, period: str) -> Any:
        self.calls.append((symbol, indicator))
        if self.delay:
            time.sleep(self.delay)
        v = self.values.get(indicator)

        class _DF:
            def __init__(self, val: float | None) -> None:
                self._val = val

            def __len__(self) -> int:
                return 1 if self._val is not None else 0

            @property
            def iloc(self) -> Any:
                return self

            def __getitem__(self, _idx: int) -> dict[str, Any]:
                return {"value": self._val}

        return _DF(v)


class TestEnricher:
    def test_xueqiu_returns_full_bundle(self) -> None:
        xq = _FakeXq({"pe_ttm": 20.0, "pb": 2.5, "market_capital": 1.0e12})
        fund, errs = enrich_fundamentals(xq=xq, ak=None, sym="600519", market="cn")
        assert fund is not None
        assert fund["pe_ttm"] == 20.0
        assert fund["pb"] == 2.5
        assert fund["market_cap"] == 1.0e12
        assert fund["currency"] == "CNY"
        assert fund["source"] == "xueqiu"
        assert errs == []

    def test_hk_market_only_uses_xueqiu(self) -> None:
        xq = _FakeXq({"pe_ttm": 12.3, "pb": 1.4, "market_capital": 4.0e12})
        ak = _FakeAk({})  # akshare must NOT be called for HK
        fund, _ = enrich_fundamentals(xq=xq, ak=ak, sym="00700", market="hk")
        assert fund is not None
        assert fund["currency"] == "HKD"
        assert fund["source"] == "xueqiu"
        assert ak.calls == [], "baidu valuation must not be hit for HK"

    def test_falls_back_to_baidu_when_xueqiu_fails(self) -> None:
        class _BadXq:
            def quote(self, symbol: str) -> dict[str, Any]:
                raise RuntimeError("xueqiu down")

        ak = _FakeAk({"市盈率(TTM)": 18.0, "市净率": 3.0, "总市值": 9.9e11})
        fund, errs = enrich_fundamentals(
            xq=_BadXq(),
            ak=ak,
            sym="600519",
            market="cn",
            hedge_delay=None,  # sequential so xueqiu fails first, then baidu wins
        )
        assert fund is not None
        assert fund["source"] == "akshare_baidu"
        assert fund["pe_ttm"] == 18.0
        assert fund["pb"] == 3.0
        # xueqiu's failure must surface in errors so callers can audit.
        assert any(e["provider"] == "xueqiu" for e in errs)

    def test_returns_none_when_both_sources_empty(self) -> None:
        class _EmptyXq:
            def quote(self, symbol: str) -> dict[str, Any]:
                return {}  # no valuation fields → raises ValueError internally

        ak = _FakeAk({})
        fund, errs = enrich_fundamentals(
            xq=_EmptyXq(),
            ak=ak,
            sym="600519",
            market="cn",
            hedge_delay=None,
        )
        assert fund is None
        # Both attempted providers should be represented in errors.
        names = {e["provider"] for e in errs}
        assert "xueqiu" in names
        assert "akshare_baidu" in names

    def test_no_providers_returns_none_quietly(self) -> None:
        fund, errs = enrich_fundamentals(xq=None, ak=None, sym="600519", market="cn")
        assert fund is None
        assert errs == []

    def test_hedged_picks_first_finisher(self) -> None:
        """When both providers are healthy, the faster one wins; the other is cancelled."""

        fast_xq = _FakeXq({"pe_ttm": 1.0, "pb": 1.0, "market_capital": 1.0}, delay=0.0)
        slow_ak = _FakeAk(
            {"市盈率(TTM)": 99.0, "市净率": 99.0, "总市值": 99.0},
            delay=0.5,  # would lose to xueqiu's instant return
        )
        t0 = time.monotonic()
        fund, _ = enrich_fundamentals(
            xq=fast_xq,
            ak=slow_ak,
            sym="600519",
            market="cn",
            hedge_delay=0.05,
        )
        elapsed = time.monotonic() - t0
        assert fund is not None
        assert fund["source"] == "xueqiu"
        # The race must not block on the slow loser.
        assert elapsed < 0.4, f"hedged race waited on loser: {elapsed:.2f}s"


class TestFetcherIntegration:
    """Verify ``MarketDataFetcher.quote`` wires the enricher in correctly."""

    @staticmethod
    def _bare_fetcher() -> MarketDataFetcher:
        # Bypass the real provider bootstrap; tests inject behaviour directly.
        f = MarketDataFetcher.__new__(MarketDataFetcher)
        f.ak = object()
        f.yf = None
        f.xq = None
        f.bs = None
        f.stooq = None
        f.provider_timeout = 1.0
        f.global_deadline = 5.0
        f.hedge_delay = None
        return f

    def test_quote_attaches_fundamentals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = self._bare_fetcher()

        def fake_ak_cn(_ak: Any, sym: str) -> FetchResult:
            return FetchResult(True, "akshare", sym, "cn", {"last": 100.0})

        called: dict[str, Any] = {}

        def fake_enrich(**kwargs: Any) -> tuple[dict[str, Any] | None, list[Any]]:
            called.update(kwargs)
            return ({"pe_ttm": 15.0, "pb": 2.0, "source": "xueqiu"}, [])

        monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", fake_ak_cn)
        monkeypatch.setattr(fetcher_mod, "enrich_fundamentals", fake_enrich)

        r = f.quote("600519", "cn")
        assert r.ok
        assert r.data["last"] == 100.0
        assert r.data["fundamentals"]["pe_ttm"] == 15.0
        assert r.data["fundamentals"]["source"] == "xueqiu"
        assert called["sym"] == "600519"
        assert called["market"] == "cn"

    def test_with_fundamentals_false_skips_enricher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = self._bare_fetcher()

        def fake_ak_cn(_ak: Any, sym: str) -> FetchResult:
            return FetchResult(True, "akshare", sym, "cn", {"last": 100.0})

        enrich_calls: list[int] = []

        def fake_enrich(**_kwargs: Any) -> tuple[dict[str, Any] | None, list[Any]]:
            enrich_calls.append(1)
            return None, []

        monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", fake_ak_cn)
        monkeypatch.setattr(fetcher_mod, "enrich_fundamentals", fake_enrich)

        r = f.quote("600519", "cn", with_fundamentals=False)
        assert r.ok
        assert "fundamentals" not in r.data
        assert enrich_calls == [], "enricher must not run when explicitly disabled"

    def test_enricher_failure_does_not_fail_quote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = self._bare_fetcher()

        def fake_ak_cn(_ak: Any, sym: str) -> FetchResult:
            return FetchResult(True, "akshare", sym, "cn", {"last": 100.0})

        def boom(**_kwargs: Any) -> tuple[dict[str, Any] | None, list[Any]]:
            raise RuntimeError("enricher internal bug")

        monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", fake_ak_cn)
        monkeypatch.setattr(fetcher_mod, "enrich_fundamentals", boom)

        r = f.quote("600519", "cn")
        assert r.ok, "fundamentals failures must never demote the price quote"
        assert r.data["last"] == 100.0
        errs = r.data.get("fundamentals_errors") or []
        assert any("enricher internal bug" in e["message"] for e in errs)

    def test_failed_quote_does_not_call_enricher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = self._bare_fetcher()

        def boom_ak(*_a: Any, **_k: Any) -> FetchResult:
            raise RuntimeError("ak down")

        enrich_calls: list[int] = []

        def fake_enrich(**_kwargs: Any) -> tuple[dict[str, Any] | None, list[Any]]:
            enrich_calls.append(1)
            return None, []

        monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", boom_ak)
        monkeypatch.setattr(fetcher_mod, "enrich_fundamentals", fake_enrich)

        r = f.quote("600519", "cn")
        assert not r.ok
        assert enrich_calls == [], "enricher must not run when the price quote failed"

    def test_concurrent_enricher_calls_are_thread_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sanity: batch_quote(with_fundamentals=True) does not deadlock or race."""

        f = self._bare_fetcher()
        lock = threading.Lock()
        seen: list[str] = []

        def fake_ak_cn(_ak: Any, sym: str) -> FetchResult:
            with lock:
                seen.append(sym)
            return FetchResult(True, "akshare", sym, "cn", {"last": 1.0})

        def fake_enrich(**kwargs: Any) -> tuple[dict[str, Any] | None, list[Any]]:
            return ({"pe_ttm": 10.0, "source": "xueqiu"}, [])

        monkeypatch.setattr(fetcher_mod.akshare_provider, "quote_cn", fake_ak_cn)
        monkeypatch.setattr(fetcher_mod, "enrich_fundamentals", fake_enrich)

        out = f.batch_quote(["600519", "000001", "688981"], "cn")
        assert {r.symbol for r in out} == {"600519", "000001", "688981"}
        assert all(r.data.get("fundamentals", {}).get("pe_ttm") == 10.0 for r in out)
        assert sorted(seen) == ["000001", "600519", "688981"]


class TestDefaults:
    """Pin defaults so accidental tightening of the budget shows up in CI."""

    def test_fundamentals_default_budgets_are_conservative(self) -> None:
        assert enricher_mod.DEFAULT_FUND_GLOBAL_DEADLINE <= 5.0
        assert enricher_mod.DEFAULT_FUND_HEDGE_DELAY is not None
        assert enricher_mod.DEFAULT_FUND_HEDGE_DELAY < 0.5
