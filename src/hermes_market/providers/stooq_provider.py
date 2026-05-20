"""Stooq fallback via pandas_datareader (last-resort provider)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..models import FetchResult
from ..normalize import to_yf_symbol
from ..utils import to_float, utc_now_iso


def load_module():  # type: ignore[no-untyped-def]
    try:
        from pandas_datareader import data as pdr  # type: ignore[import-not-found]

        return pdr
    except Exception:
        return None


def quote(pdr, sym: str, market: str) -> FetchResult:  # type: ignore[no-untyped-def]
    ticker = to_yf_symbol(sym, market)
    end = datetime.now(timezone.utc)
    start = end.replace(day=1)
    df = pdr.DataReader(ticker, "stooq", start, end)
    if df is None or df.empty:
        raise ValueError("empty quote from stooq")
    r = df.sort_index().iloc[-1]
    data = {
        "name": ticker,
        "last": to_float(r.get("Close")),
        "change_pct": None,  # Stooq does not directly expose prev-close in this slice.
        "turnover": None,
        "volume": to_float(r.get("Volume")),
        "timestamp": utc_now_iso(),
    }
    return FetchResult(True, "stooq", sym, market, data)


def history(pdr, sym: str, market: str, start: str, end: str) -> FetchResult:  # type: ignore[no-untyped-def]
    ticker = to_yf_symbol(sym, market)
    df = pdr.DataReader(ticker, "stooq", start, end)
    if df is None or df.empty:
        raise ValueError("empty history from stooq")
    rows: list[dict[str, Any]] = [
        {
            "date": idx.strftime("%Y-%m-%d"),
            "open": to_float(r.get("Open")),
            "high": to_float(r.get("High")),
            "low": to_float(r.get("Low")),
            "close": to_float(r.get("Close")),
            "volume": to_float(r.get("Volume")),
        }
        for idx, r in df.sort_index().iterrows()
    ]
    return FetchResult(True, "stooq", sym, market, {"bars": rows})
