"""baostock-backed CN-only quote / history provider.

baostock is T+1; for the quote path we look back ~7 calendar days and return
the most recent trading day's close. The result is tagged with a ``note``
field so the caller can warn that the value is not realtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import FetchResult
from ..normalize import to_baostock_code
from ..utils import to_float, utc_now_iso


def load_module():  # type: ignore[no-untyped-def]
    try:
        import baostock as bs  # type: ignore[import-not-found]

        return bs
    except Exception:
        return None


def quote(bs, sym: str, market: str) -> FetchResult:  # type: ignore[no-untyped-def]
    if market != "cn":
        raise ValueError("baostock supports CN only")
    code = to_baostock_code(sym)
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(lg.error_msg)
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        rs = bs.query_history_k_data_plus(
            code,
            "date,code,close,preclose,volume",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="3",
        )
        if rs.error_code != "0":
            raise RuntimeError(rs.error_msg)
        rows: list[list[Any]] = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            raise ValueError("baostock T+1: no recent trading day data")
        date_str, _, close, preclose, volume = rows[-1]
        close_f = to_float(close)
        prev_f = to_float(preclose)
        pct = None if close_f is None or prev_f in (None, 0) else (close_f - prev_f) / prev_f * 100  # type: ignore[operator]
        return FetchResult(
            True,
            "baostock",
            sym,
            market,
            {
                "name": code,
                "last": close_f,
                "change_pct": pct,
                "turnover": to_float(volume),
                "as_of": date_str,
                "timestamp": utc_now_iso(),
                "note": "T+1 delayed via baostock",
            },
        )
    finally:
        bs.logout()


def history(bs, sym: str, market: str, start: str, end: str) -> FetchResult:  # type: ignore[no-untyped-def]
    if market != "cn":
        raise ValueError("baostock supports CN only")
    code = to_baostock_code(sym)
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(lg.error_msg)
    try:
        rs = bs.query_history_k_data_plus(
            code,
            "date,open,high,low,close,volume",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="3",
        )
        if rs.error_code != "0":
            raise RuntimeError(rs.error_msg)
        rows: list[dict[str, Any]] = []
        while rs.next():
            date_str, op, hi, lo, cl, vol = rs.get_row_data()
            rows.append(
                {
                    "date": date_str,
                    "open": to_float(op),
                    "high": to_float(hi),
                    "low": to_float(lo),
                    "close": to_float(cl),
                    "volume": to_float(vol),
                }
            )
        if not rows:
            raise ValueError("empty history from baostock")
        return FetchResult(True, "baostock", sym, market, {"bars": rows, "note": "T+1 delayed via baostock"})
    finally:
        bs.logout()
