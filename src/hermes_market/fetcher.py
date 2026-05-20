"""Orchestrates the multi-provider fallback chain for quote / history / news.

The orchestration logic itself lives in :mod:`hermes_market.runner`; this
module is responsible for building a provider-specific list of
``(name, callable)`` attempts and passing it through the runner with the
caller's timeout / deadline / hedging settings.
"""

from __future__ import annotations

from collections.abc import Callable

from .models import FetchResult, fail_result
from .normalize import detect_market, normalize_symbol
from .providers import (
    akshare_provider,
    baostock_provider,
    sina_rss,
    stooq_provider,
    xueqiu_provider,
    yfinance_provider,
)
from .providers.xueqiu_provider import XueqiuClient
from .runner import Attempt, run_with_fallback

# Defaults are deliberately conservative; the README recommended an 8-15s
# end-to-end budget, and we pick 6s per-provider / 20s overall so that the
# common case of "primary akshare succeeds in <2s" still wins comfortably
# while pathological hangs cannot blow past the budget.
DEFAULT_PROVIDER_TIMEOUT = 6.0
DEFAULT_GLOBAL_DEADLINE = 20.0


class MarketDataFetcher:
    """High-level orchestrator.

    Provider chain (each step is tried only if available; failures are recorded
    in ``FetchResult.errors``):

    * quote / history: ``akshare`` → ``yfinance`` → ``xueqiu`` → ``baostock`` (CN only) → ``stooq``
    * news:            HK+symbol routes to ``xueqiu`` first; otherwise
      ``akshare`` → ``xueqiu`` → ``sina_rss``

    Pass ``hedge_delay`` to enable speculative concurrent execution (the next
    provider is spawned after the previous one has waited ``hedge_delay``
    seconds). Default is ``None`` — strict sequential.
    """

    def __init__(
        self,
        *,
        provider_timeout: float = DEFAULT_PROVIDER_TIMEOUT,
        global_deadline: float = DEFAULT_GLOBAL_DEADLINE,
        hedge_delay: float | None = None,
    ) -> None:
        self.ak = akshare_provider.load_module()
        self.yf = yfinance_provider.load_module()
        self.xq = XueqiuClient()
        self.bs = baostock_provider.load_module()
        self.stooq = stooq_provider.load_module()
        self.provider_timeout = provider_timeout
        self.global_deadline = global_deadline
        self.hedge_delay = hedge_delay

    # ------------------------------------------------------------------- helpers
    def _run(self, attempts: list[Attempt], symbol: str, market: str) -> FetchResult:
        result, errors = run_with_fallback(
            attempts,
            per_provider_timeout=self.provider_timeout,
            global_deadline=self.global_deadline,
            hedge_delay=self.hedge_delay,
        )
        if result is not None:
            return result
        return fail_result("none", symbol, market, errors)  # type: ignore[arg-type]

    @staticmethod
    def _missing(name: str) -> Callable[[], FetchResult]:
        def _f() -> FetchResult:
            raise RuntimeError("not installed")

        _f.__name__ = f"_missing_{name}"
        return _f

    # ------------------------------------------------------------------ quote
    def quote(self, symbol: str, market: str | None = None) -> FetchResult:
        mkt = detect_market(symbol, market)
        sym = normalize_symbol(symbol, mkt)

        attempts: list[Attempt] = []
        if self.ak is not None:
            attempts.append(
                (
                    "akshare",
                    (lambda: akshare_provider.quote_cn(self.ak, sym))
                    if mkt == "cn"
                    else (lambda: akshare_provider.quote_hk(self.ak, sym)),
                )
            )
        else:
            attempts.append(("akshare", self._missing("akshare")))
        if self.yf is not None:
            attempts.append(("yfinance", lambda: yfinance_provider.quote(self.yf, sym, mkt)))
        else:
            attempts.append(("yfinance", self._missing("yfinance")))
        attempts.append(("xueqiu", lambda: xueqiu_provider.quote(self.xq, sym, mkt)))
        if mkt == "cn":
            if self.bs is not None:
                attempts.append(("baostock", lambda: baostock_provider.quote(self.bs, sym, mkt)))
            else:
                attempts.append(("baostock", self._missing("baostock")))
        if self.stooq is not None:
            attempts.append(("stooq", lambda: stooq_provider.quote(self.stooq, sym, mkt)))
        else:
            attempts.append(("stooq", self._missing("stooq")))
        return self._run(attempts, sym, mkt)

    # ---------------------------------------------------------------- history
    def history(self, symbol: str, start: str, end: str, market: str | None = None) -> FetchResult:
        mkt = detect_market(symbol, market)
        sym = normalize_symbol(symbol, mkt)

        attempts: list[Attempt] = []
        if self.ak is not None:
            attempts.append(("akshare", lambda: akshare_provider.history(self.ak, sym, mkt, start, end)))
        else:
            attempts.append(("akshare", self._missing("akshare")))
        if self.yf is not None:
            attempts.append(("yfinance", lambda: yfinance_provider.history(self.yf, sym, mkt, start, end)))
        else:
            attempts.append(("yfinance", self._missing("yfinance")))
        attempts.append(("xueqiu", lambda: xueqiu_provider.history(self.xq, sym, mkt, start, end)))
        if mkt == "cn":
            if self.bs is not None:
                attempts.append(("baostock", lambda: baostock_provider.history(self.bs, sym, mkt, start, end)))
            else:
                attempts.append(("baostock", self._missing("baostock")))
        if self.stooq is not None:
            attempts.append(("stooq", lambda: stooq_provider.history(self.stooq, sym, mkt, start, end)))
        else:
            attempts.append(("stooq", self._missing("stooq")))
        return self._run(attempts, sym, mkt)

    # ------------------------------------------------------------------- news
    def news(self, limit: int = 20, symbol: str | None = None, market: str | None = None) -> FetchResult:
        symbol_norm: str | None = None
        mkt = market or "global"
        if symbol:
            mkt = detect_market(symbol, market)
            symbol_norm = normalize_symbol(symbol, mkt)

        # akshare news has no per-symbol filter for HK; route HK + symbol to
        # xueqiu first so we do not silently fall back through an empty path.
        prefer_xq = symbol_norm is not None and mkt == "hk"

        attempts: list[Attempt] = []
        if not prefer_xq:
            if self.ak is not None:
                attempts.append(("akshare", lambda: akshare_provider.news(self.ak, limit, symbol_norm, mkt)))
            else:
                attempts.append(("akshare", self._missing("akshare")))
        attempts.append(("xueqiu", lambda: xueqiu_provider.news(self.xq, limit, symbol_norm, mkt)))
        if prefer_xq and self.ak is not None:
            attempts.append(("akshare", lambda: akshare_provider.news(self.ak, limit, symbol_norm, mkt)))
        attempts.append(("sina_rss", lambda: sina_rss.news(limit, symbol_norm)))
        return self._run(attempts, symbol_norm or "", mkt)
