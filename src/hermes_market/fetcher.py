"""Orchestrates the multi-provider fallback chain for quote / history / news."""

from __future__ import annotations

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


class MarketDataFetcher:
    """High-level orchestrator.

    Provider chain (each step is tried only if available; failures are recorded
    in ``FetchResult.errors``):

    * quote / history: ``akshare`` → ``yfinance`` → ``xueqiu`` → ``baostock`` (CN only) → ``stooq``
    * news:            HK+symbol routes to ``xueqiu`` first; otherwise
      ``akshare`` → ``xueqiu`` → ``sina_rss``
    """

    def __init__(self) -> None:
        self.ak = akshare_provider.load_module()
        self.yf = yfinance_provider.load_module()
        self.xq = XueqiuClient()
        self.bs = baostock_provider.load_module()
        self.stooq = stooq_provider.load_module()

    # ------------------------------------------------------------------ quote
    def quote(self, symbol: str, market: str | None = None) -> FetchResult:
        mkt = detect_market(symbol, market)
        sym = normalize_symbol(symbol, mkt)
        errors: list[dict[str, str]] = []

        if self.ak is not None:
            try:
                return (
                    akshare_provider.quote_cn(self.ak, sym) if mkt == "cn" else akshare_provider.quote_hk(self.ak, sym)
                )
            except Exception as e:  # noqa: BLE001
                errors.append({"provider": "akshare", "message": str(e)})
        else:
            errors.append({"provider": "akshare", "message": "not installed"})

        if self.yf is not None:
            try:
                return yfinance_provider.quote(self.yf, sym, mkt)
            except Exception as e:  # noqa: BLE001
                errors.append({"provider": "yfinance", "message": str(e)})
        else:
            errors.append({"provider": "yfinance", "message": "not installed"})

        try:
            return xueqiu_provider.quote(self.xq, sym, mkt)
        except Exception as e:  # noqa: BLE001
            errors.append({"provider": "xueqiu", "message": str(e)})

        if mkt == "cn":
            if self.bs is not None:
                try:
                    return baostock_provider.quote(self.bs, sym, mkt)
                except Exception as e:  # noqa: BLE001
                    errors.append({"provider": "baostock", "message": str(e)})
            else:
                errors.append({"provider": "baostock", "message": "not installed"})

        if self.stooq is not None:
            try:
                return stooq_provider.quote(self.stooq, sym, mkt)
            except Exception as e:  # noqa: BLE001
                errors.append({"provider": "stooq", "message": str(e)})
        else:
            errors.append({"provider": "stooq", "message": "not installed"})

        return fail_result("none", sym, mkt, errors)  # type: ignore[arg-type]

    # ---------------------------------------------------------------- history
    def history(self, symbol: str, start: str, end: str, market: str | None = None) -> FetchResult:
        mkt = detect_market(symbol, market)
        sym = normalize_symbol(symbol, mkt)
        errors: list[dict[str, str]] = []

        if self.ak is not None:
            try:
                return akshare_provider.history(self.ak, sym, mkt, start, end)
            except Exception as e:  # noqa: BLE001
                errors.append({"provider": "akshare", "message": str(e)})
        else:
            errors.append({"provider": "akshare", "message": "not installed"})

        if self.yf is not None:
            try:
                return yfinance_provider.history(self.yf, sym, mkt, start, end)
            except Exception as e:  # noqa: BLE001
                errors.append({"provider": "yfinance", "message": str(e)})
        else:
            errors.append({"provider": "yfinance", "message": "not installed"})

        try:
            return xueqiu_provider.history(self.xq, sym, mkt, start, end)
        except Exception as e:  # noqa: BLE001
            errors.append({"provider": "xueqiu", "message": str(e)})

        if mkt == "cn":
            if self.bs is not None:
                try:
                    return baostock_provider.history(self.bs, sym, mkt, start, end)
                except Exception as e:  # noqa: BLE001
                    errors.append({"provider": "baostock", "message": str(e)})
            else:
                errors.append({"provider": "baostock", "message": "not installed"})

        if self.stooq is not None:
            try:
                return stooq_provider.history(self.stooq, sym, mkt, start, end)
            except Exception as e:  # noqa: BLE001
                errors.append({"provider": "stooq", "message": str(e)})
        else:
            errors.append({"provider": "stooq", "message": "not installed"})

        return fail_result("none", sym, mkt, errors)  # type: ignore[arg-type]

    # ------------------------------------------------------------------- news
    def news(self, limit: int = 20, symbol: str | None = None, market: str | None = None) -> FetchResult:
        symbol_norm: str | None = None
        mkt = market or "global"
        if symbol:
            mkt = detect_market(symbol, market)
            symbol_norm = normalize_symbol(symbol, mkt)
        errors: list[dict[str, str]] = []

        # akshare news has no per-symbol filter for HK; route HK + symbol to
        # xueqiu first so we do not silently fall back through an empty path.
        prefer_xq = symbol_norm is not None and mkt == "hk"

        if not prefer_xq:
            if self.ak is not None:
                try:
                    return akshare_provider.news(self.ak, limit, symbol_norm, mkt)
                except Exception as e:  # noqa: BLE001
                    errors.append({"provider": "akshare", "message": str(e)})
            else:
                errors.append({"provider": "akshare", "message": "not installed"})

        try:
            return xueqiu_provider.news(self.xq, limit, symbol_norm, mkt)
        except Exception as e:  # noqa: BLE001
            errors.append({"provider": "xueqiu", "message": str(e)})

        # If we preferred xueqiu first and it failed, give akshare a chance
        # before giving up to sina.
        if prefer_xq and self.ak is not None:
            try:
                return akshare_provider.news(self.ak, limit, symbol_norm, mkt)
            except Exception as e:  # noqa: BLE001
                errors.append({"provider": "akshare", "message": str(e)})

        try:
            return sina_rss.news(limit, symbol_norm)
        except Exception as e:  # noqa: BLE001
            errors.append({"provider": "sina_rss", "message": str(e)})

        return fail_result("none", symbol_norm or "", mkt, errors)  # type: ignore[arg-type]
