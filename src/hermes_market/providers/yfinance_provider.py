"""yfinance-backed quote / history provider."""

from __future__ import annotations

from typing import Any

from ..models import FetchResult
from ..normalize import to_yf_symbol
from ..utils import pct_change, to_float, utc_now_iso


def load_module():  # type: ignore[no-untyped-def]
    try:
        import yfinance as yf  # type: ignore[import-not-found]

        return yf
    except Exception:
        return None


def _fast_info_get(info: Any, key: str) -> Any:
    """Read a key from yfinance ``fast_info`` (dict-like across versions)."""

    if info is None:
        return None
    try:
        return info[key]
    except (KeyError, TypeError):
        pass
    getter = getattr(info, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:
            return None
    return getattr(info, key, None)


def quote(yf, sym: str, market: str) -> FetchResult:  # type: ignore[no-untyped-def]
    ticker = to_yf_symbol(sym, market)
    info = yf.Ticker(ticker).fast_info
    last = to_float(_fast_info_get(info, "lastPrice"))
    prev = to_float(_fast_info_get(info, "previousClose"))
    volume = to_float(_fast_info_get(info, "lastVolume"))
    turnover = last * volume if (last is not None and volume is not None) else None
    data: dict[str, Any] = {
        "name": ticker,
        "last": last,
        "change_pct": pct_change(last, prev),
        # ``turnover`` is reported in the listing currency (CNY/HKD). For the
        # yfinance path we approximate as ``last * volume`` since fast_info
        # does not expose a direct turnover field.
        "turnover": turnover,
        "volume": volume,
        "timestamp": utc_now_iso(),
    }
    return FetchResult(True, "yfinance", sym, market, data)


def history(yf, sym: str, market: str, start: str, end: str) -> FetchResult:  # type: ignore[no-untyped-def]
    ticker = to_yf_symbol(sym, market)
    hist = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
    if hist is None or hist.empty:
        raise ValueError("empty history from yfinance")
    rows: list[dict[str, Any]] = [
        {
            "date": idx.strftime("%Y-%m-%d"),
            "open": to_float(r.get("Open")),
            "high": to_float(r.get("High")),
            "low": to_float(r.get("Low")),
            "close": to_float(r.get("Close")),
            "volume": to_float(r.get("Volume")),
        }
        for idx, r in hist.iterrows()
    ]
    return FetchResult(True, "yfinance", sym, market, {"bars": rows})
