"""Fundamental-data enricher (PE / PB / market cap).

The :func:`enrich_fundamentals` helper races two free public sources in
parallel and returns whichever one comes back first with a non-empty
valuation bundle. The same hedged primitive used by :mod:`hermes_market.runner`
is reused so the latency/timeout behaviour matches the rest of the package.

Source matrix:

* **xueqiu** (``stock.xueqiu.com /v5/stock/quote.json?extend=detail``)
  returns ``pe_ttm`` / ``pe_lyr`` / ``pb`` / ``market_capital`` /
  ``float_market_capital`` in a single HTTP round-trip. Works for both
  A-share and HK.
* **akshare → baidu** (``stock_zh_valuation_baidu``) returns one indicator
  per call; we fan out ``市盈率(TTM) / 市盈率(静) / 市净率 / 总市值`` in
  parallel and snap the most recent value off each daily series. Only
  supports A-share.

The enricher is intentionally **best-effort**: a failure here never fails
the parent ``quote`` call. The caller receives ``(fundamentals_dict_or_None,
errors_list)`` and decides whether to surface either.
"""

from __future__ import annotations

import concurrent.futures as cf
from collections.abc import Callable
from typing import Any

from .models import FetchResult
from .normalize import to_xq_symbol
from .runner import Attempt, run_with_fallback
from .utils import to_float, utc_now_iso

# Fundamentals are a side-channel to the price quote; default budgets are
# tighter than the main fallback chain so that adding ``--with-fundamentals``
# never doubles the user-visible latency.
DEFAULT_FUND_PROVIDER_TIMEOUT = 3.0
DEFAULT_FUND_GLOBAL_DEADLINE = 4.0
DEFAULT_FUND_HEDGE_DELAY: float | None = 0.15

# Currency code per market — turnover/market_cap are reported in this unit.
_CCY = {"cn": "CNY", "hk": "HKD"}


def _has_valuation(fund: dict[str, Any]) -> bool:
    """A fundamentals bundle is "usable" if any one of pe_ttm / pb / market_cap is present."""

    return any(fund.get(k) is not None for k in ("pe_ttm", "pe_lyr", "pb", "market_cap"))


# --------------------------------------------------------------------- xueqiu


def _fundamentals_xueqiu(client: Any, sym: str, market: str) -> FetchResult:
    """One-shot xueqiu fundamentals via the existing quote endpoint."""

    item = client.quote(to_xq_symbol(sym, market))
    fund: dict[str, Any] = {
        "pe_ttm": to_float(item.get("pe_ttm")),
        "pe_lyr": to_float(item.get("pe_lyr")),
        "pb": to_float(item.get("pb")),
        "ps_ttm": to_float(item.get("psr")),
        "market_cap": to_float(item.get("market_capital")),
        "float_market_cap": to_float(item.get("float_market_capital")),
        "dividend_yield": to_float(item.get("dividend_yield")),
        "currency": _CCY.get(market),
        "as_of": utc_now_iso(),
    }
    if not _has_valuation(fund):
        raise ValueError("xueqiu returned no valuation fields")
    return FetchResult(True, "xueqiu", sym, market, fund)


# ------------------------------------------------------------- akshare/baidu


def _baidu_last_value(ak: Any, sym: str, indicator: str) -> float | None:
    """Snap the most recent (date, value) row from baidu's daily valuation series."""

    fn = getattr(ak, "stock_zh_valuation_baidu", None)
    if fn is None:
        return None
    df = fn(symbol=sym, indicator=indicator, period="近一年")
    if df is None or len(df) == 0:
        return None
    val = df.iloc[-1].get("value")
    return to_float(val)


_BAIDU_INDICATORS = [
    ("pe_ttm", "市盈率(TTM)"),
    ("pe_lyr", "市盈率(静)"),
    ("pb", "市净率"),
    ("market_cap", "总市值"),
]


def _fundamentals_akshare_baidu(ak: Any, sym: str, market: str) -> FetchResult:
    """Parallel fan-out of baidu's per-indicator endpoints (A-share only)."""

    if market != "cn":
        raise ValueError("akshare_baidu valuation is CN-only")

    out: dict[str, Any] = {
        "pe_ttm": None,
        "pe_lyr": None,
        "pb": None,
        "ps_ttm": None,
        "market_cap": None,
        "float_market_cap": None,
        "dividend_yield": None,
        "currency": _CCY.get(market),
        "as_of": utc_now_iso(),
    }
    # 4 short HTTP calls; doing them in parallel keeps the akshare path
    # competitive with xueqiu's single-shot endpoint.
    with cf.ThreadPoolExecutor(
        max_workers=len(_BAIDU_INDICATORS),
        thread_name_prefix="hermes-fund-baidu",
    ) as ex:
        futs = {ex.submit(_baidu_last_value, ak, sym, label): key for key, label in _BAIDU_INDICATORS}
        for fut in cf.as_completed(futs):
            key = futs[fut]
            try:
                out[key] = fut.result()
            except Exception:  # noqa: BLE001 - one indicator failing is fine, others may still fill in
                out[key] = None
    if not _has_valuation(out):
        raise ValueError("akshare/baidu returned no valuation fields")
    return FetchResult(True, "akshare_baidu", sym, market, out)


# -------------------------------------------------------------- orchestrator


def _missing_attempt(name: str) -> Callable[[], FetchResult]:
    def _f() -> FetchResult:
        raise RuntimeError("not installed")

    _f.__name__ = f"_missing_{name}"
    return _f


def enrich_fundamentals(
    *,
    xq: Any,
    ak: Any,
    sym: str,
    market: str,
    provider_timeout: float = DEFAULT_FUND_PROVIDER_TIMEOUT,
    global_deadline: float = DEFAULT_FUND_GLOBAL_DEADLINE,
    hedge_delay: float | None = DEFAULT_FUND_HEDGE_DELAY,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Race xueqiu and akshare/baidu; return ``(fundamentals_or_None, errors)``.

    For HK only xueqiu has a working free endpoint, so the race degenerates to
    a single attempt. The returned dict always includes the winning provider
    under ``source``; missing fields are ``None`` (never raised).
    """

    attempts: list[Attempt] = []
    if xq is not None:
        attempts.append(("xueqiu", lambda: _fundamentals_xueqiu(xq, sym, market)))
    if market == "cn" and ak is not None:
        attempts.append(("akshare_baidu", lambda: _fundamentals_akshare_baidu(ak, sym, market)))
    if not attempts:
        return None, []

    result, errors = run_with_fallback(
        attempts,
        per_provider_timeout=provider_timeout,
        global_deadline=global_deadline,
        hedge_delay=hedge_delay,
    )
    if result is None or not result.ok:
        return None, errors
    fund = dict(result.data)
    fund["source"] = result.provider
    return fund, errors
