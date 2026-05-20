"""Stooq fallback via direct HTTP CSV (last-resort provider).

Stooq exposes two CSV endpoints we care about:

* ``https://stooq.com/q/l/`` — latest quote (free, no auth). Returns one row
  per symbol with ``Symbol,Date,Time,Open,High,Low,Close,Volume``. ``N/D``
  in any field signals "no data" (typically wrong symbol format or symbol
  not on Stooq).
* ``https://stooq.com/q/d/l/`` — historical daily CSV. As of 2025 this
  endpoint requires an apikey (captcha-gated, see
  https://stooq.com/q/d/?s=...&get_apikey). Without the key it returns a
  plain-text "Get your apikey" message. We surface that as a clean error
  so the fallback chain moves on. If ``STOOQ_APIKEY`` is set, we append
  it and history works.

This replaces the previous ``pandas_datareader`` implementation that was
broken on Python 3.12 (`distutils.version` removed,
`pandas.util.deprecate_kwarg` signature changed).
"""

from __future__ import annotations

import csv
import io
import os
import urllib.parse
import urllib.request
from typing import Any

from ..models import FetchResult
from ..normalize import to_stooq_symbol
from ..utils import to_float, utc_now_iso

_QUOTE_URL = "https://stooq.com/q/l/"
_HISTORY_URL = "https://stooq.com/q/d/l/"
_DEFAULT_TIMEOUT = 6.0
_USER_AGENT = "hermes-market/1 (+https://github.com/ply80227-jpg/finance)"


def load_module() -> object:  # type: ignore[no-untyped-def]
    """Sentinel kept for fetcher-chain symmetry with other providers.

    Returns an opaque non-None object so the runner does not skip stooq.
    The real network call happens inside :func:`quote` / :func:`history`.
    """

    return object()


def _http_get(url: str, params: dict[str, str], *, timeout: float = _DEFAULT_TIMEOUT) -> str:
    """GET ``url`` with ``params`` and return the decoded body."""

    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed scheme
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _is_apikey_gate(body: str) -> bool:
    """Stooq's free-tier denial page starts with a fixed banner."""

    head = body.lstrip().lower()
    return head.startswith("get your apikey") or "get_apikey" in head[:200]


def _parse_csv(body: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(body))
    return [row for row in reader]


def _f(value: str | None) -> float | None:
    """Float coercer that treats Stooq's ``N/D`` sentinel as missing."""

    if value is None:
        return None
    s = value.strip()
    if not s or s.upper() == "N/D":
        return None
    return to_float(s)


def quote(_mod: object, sym: str, market: str) -> FetchResult:
    """Fetch latest snapshot via Stooq's free CSV endpoint."""

    ticker = to_stooq_symbol(sym, market)
    body = _http_get(_QUOTE_URL, {"s": ticker, "f": "sd2t2ohlcv", "h": "", "e": "csv"})
    rows = _parse_csv(body)
    if not rows:
        raise ValueError("empty quote from stooq")
    r = rows[0]
    close = _f(r.get("Close"))
    open_ = _f(r.get("Open"))
    if close is None:
        raise ValueError(f"stooq has no data for {ticker} (got N/D)")
    change_pct: float | None = None
    if open_ is not None and open_ != 0:
        # Stooq's CSV omits previous close; intraday % vs. open is the best
        # proxy and matches the field semantics the other providers use.
        change_pct = (close - open_) / open_ * 100.0
    data = {
        "name": ticker,
        "last": close,
        "change_pct": change_pct,
        "turnover": None,
        "volume": _f(r.get("Volume")),
        "timestamp": utc_now_iso(),
        "as_of": r.get("Date") or None,
    }
    return FetchResult(True, "stooq", sym, market, data)


def _yyyymmdd(date_str: str) -> str:
    """Accept either ``YYYYMMDD`` or ``YYYY-MM-DD`` and return ``YYYYMMDD``."""

    s = date_str.strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s.replace("-", "")
    return s


def history(_mod: object, sym: str, market: str, start: str, end: str) -> FetchResult:
    """Fetch daily OHLCV via Stooq's historical CSV endpoint.

    The endpoint requires an apikey (free, captcha-gated) since 2025. Set
    ``STOOQ_APIKEY`` in the environment to enable. Without the key we
    surface a structured error so the fetcher's fallback chain continues.
    """

    ticker = to_stooq_symbol(sym, market)
    params = {"s": ticker, "d1": _yyyymmdd(start), "d2": _yyyymmdd(end), "i": "d"}
    apikey = os.environ.get("STOOQ_APIKEY")
    if apikey:
        params["apikey"] = apikey
    body = _http_get(_HISTORY_URL, params)
    if _is_apikey_gate(body):
        raise RuntimeError(
            "stooq history requires apikey since 2025; "
            "set STOOQ_APIKEY env var or rely on earlier providers in the chain"
        )
    rows = _parse_csv(body)
    if not rows:
        raise ValueError("empty history from stooq")
    bars: list[dict[str, Any]] = []
    for r in rows:
        date = r.get("Date")
        if not date:
            continue
        bars.append(
            {
                "date": date,
                "open": _f(r.get("Open")),
                "high": _f(r.get("High")),
                "low": _f(r.get("Low")),
                "close": _f(r.get("Close")),
                "volume": _f(r.get("Volume")),
            }
        )
    if not bars:
        raise ValueError("empty history from stooq (no parseable rows)")
    return FetchResult(True, "stooq", sym, market, {"bars": bars})
