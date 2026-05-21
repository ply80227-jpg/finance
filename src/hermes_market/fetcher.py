"""Orchestrates the multi-provider fallback chain for quote / history / news.

The orchestration logic itself lives in :mod:`hermes_market.runner`; this
module is responsible for building a provider-specific list of
``(name, callable)`` attempts and passing it through the runner with the
caller's timeout / deadline / hedging settings.
"""

from __future__ import annotations

import concurrent.futures as cf
from collections.abc import Callable

from .enricher import enrich_fundamentals
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
from .symbol_index import SymbolRow, _search_xueqiu, get_index
from .symbol_index import search as _search_rows

# Defaults are deliberately conservative; the README recommended an 8-15s
# end-to-end budget, and we pick 6s per-provider / 20s overall so that the
# common case of "primary akshare succeeds in <2s" still wins comfortably
# while pathological hangs cannot blow past the budget.
DEFAULT_PROVIDER_TIMEOUT = 6.0
DEFAULT_GLOBAL_DEADLINE = 20.0
# Fundamentals are enabled by default — they are cheap (a single xueqiu RTT
# in the happy path) and answer the next obvious follow-up question ("PE?").
# Callers who care about absolute floor latency can pass ``with_fundamentals=False``.
DEFAULT_WITH_FUNDAMENTALS = True


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
    def quote(
        self,
        symbol: str,
        market: str | None = None,
        *,
        with_fundamentals: bool = DEFAULT_WITH_FUNDAMENTALS,
    ) -> FetchResult:
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
        result = self._run(attempts, sym, mkt)
        if with_fundamentals and result.ok:
            self._attach_fundamentals(result)
        return result

    # ---------------------------------------------------- fundamentals enrichment
    def _attach_fundamentals(self, result: FetchResult) -> None:
        """Best-effort: enrich ``result.data`` with PE/PB/market_cap in place.

        Failures are recorded under ``data['fundamentals_errors']`` so callers
        can audit, but they never demote ``result.ok``.
        """

        try:
            fund, errors = enrich_fundamentals(
                xq=self.xq,
                ak=self.ak,
                sym=result.symbol,
                market=result.market,
            )
        except Exception as exc:  # noqa: BLE001 - never fail the parent quote
            result.data["fundamentals_errors"] = [{"provider": "enricher", "message": f"{type(exc).__name__}: {exc}"}]
            return
        if fund is not None:
            result.data["fundamentals"] = fund
        if errors:
            result.data["fundamentals_errors"] = errors

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

    # -------------------------------------------------------------- batch quote
    def batch_quote(
        self,
        symbols: list[str],
        market: str | None = None,
        *,
        max_workers: int = 8,
        with_fundamentals: bool = DEFAULT_WITH_FUNDAMENTALS,
    ) -> list[FetchResult]:
        """Fetch quotes for many symbols concurrently.

        Each symbol runs its own independent fallback chain (so a flaky
        akshare on one symbol does not affect another). Returns results in
        the same order as ``symbols``. Individual failures surface as
        ``ok=False`` items rather than raising.
        """

        if not symbols:
            return []
        # Bound the worker pool to the actual batch size to avoid spawning
        # idle threads for tiny batches.
        workers = max(1, min(max_workers, len(symbols)))
        results: list[FetchResult | None] = [None] * len(symbols)

        def _one(idx_sym: tuple[int, str]) -> tuple[int, FetchResult]:
            idx, sym = idx_sym
            try:
                return idx, self.quote(sym, market, with_fundamentals=with_fundamentals)
            except Exception as exc:  # noqa: BLE001
                return idx, fail_result(
                    "none",
                    sym,
                    market or "",
                    [{"provider": "batch", "message": f"{type(exc).__name__}: {exc}"}],
                )

        with cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hermes-batch") as ex:
            for idx, res in ex.map(_one, enumerate(symbols)):
                results[idx] = res
        # The pool always populates every slot, but help the type checker.
        return [r if r is not None else fail_result("none", "", "", []) for r in results]

    # ------------------------------------------------------------------ search
    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        market: str | None = None,
    ) -> list[SymbolRow]:
        """Resolve a natural-language query to (code, name, market) tuples.

        Uses the local akshare-built index first (24-hour TTL); falls back to
        xueqiu's ``/v5/stock/search`` HTTP endpoint when the index is empty
        (e.g. on a fresh box without akshare installed).
        """

        if not query.strip():
            return []
        try:
            rows = get_index(self.ak)
        except Exception:  # noqa: BLE001
            # akshare's spot endpoints can be throttled / connection-reset
            # from various egress IPs; degrade to the xueqiu fallback below
            # instead of bubbling the network error up to the agent.
            rows = []
        if rows:
            return _search_rows(query, rows, limit=limit, market=market)
        # Online fallback: hit xueqiu's search endpoint.
        try:
            online = _search_xueqiu(query, self.xq._get_json)
        except Exception:  # noqa: BLE001
            online = []
        if market is not None:
            online = [r for r in online if r.market == market]
        return online[:limit]
