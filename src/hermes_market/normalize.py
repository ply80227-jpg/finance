"""Market detection and symbol-format normalization across providers.

A-share 6-digit code prefix → exchange routing:

* ``60x / 68x / 9xx`` → SH (Shanghai main board / STAR market)
* ``00x / 30x``       → SZ (Shenzhen main board / ChiNext)
* ``4xx / 8xx``       → BJ (Beijing Stock Exchange)

The original single-file script collapsed everything that did not start with
``6`` into Shenzhen, which mis-routed all Beijing-listed stocks.
"""

from __future__ import annotations

import re

CN_RE_FULL = re.compile(r"(sh|sz|bj)\d{6}")
CN_RE_BARE = re.compile(r"\d{6}")
HK_RE_FULL = re.compile(r"\d{5}")
HK_RE_SHORT = re.compile(r"\d{1,5}")


def detect_market(symbol: str, market: str | None) -> str:
    """Resolve the market for ``symbol``. Explicit ``market`` wins.

    The HK regex was widened to accept 1-5 digit codes (``700`` as well as
    ``00700``) when the input is unambiguously not an A-share 6-digit code.
    """

    if market in {"cn", "hk"}:
        return market
    s = symbol.strip().lower()
    if CN_RE_FULL.fullmatch(s) or CN_RE_BARE.fullmatch(s):
        return "cn"
    if HK_RE_FULL.fullmatch(s) or s.endswith(".hk"):
        return "hk"
    if HK_RE_SHORT.fullmatch(s):
        return "hk"
    raise ValueError(f"Cannot detect market, please pass --market cn|hk explicitly. symbol={symbol}")


def normalize_symbol(symbol: str, market: str) -> str:
    """Normalize ``symbol`` to the canonical bare form used internally."""

    s = symbol.strip().lower()
    if market == "cn":
        if CN_RE_BARE.fullmatch(s):
            return s
        if CN_RE_FULL.fullmatch(s):
            return s[2:]
    if market == "hk":
        if s.endswith(".hk"):
            return s[:-3].zfill(5)
        if re.fullmatch(r"\d{1,5}", s):
            return s.zfill(5)
    raise ValueError(f"Invalid symbol format: {symbol} (market={market})")


def _cn_exchange_prefix(sym: str) -> str:
    """Map a 6-digit A-share code to ``sh`` / ``sz`` / ``bj``.

    The unknown-prefix fallback returns ``sz`` to preserve the historical
    behaviour, but we explicitly route 4/8/9 to the correct venues.
    """

    if not sym or len(sym) < 1:
        return "sz"
    head = sym[0]
    if head in {"6", "9"}:
        return "sh"
    if head in {"0", "3"}:
        return "sz"
    if head in {"4", "8"}:
        return "bj"
    return "sz"


def to_xq_symbol(sym: str, market: str) -> str:
    if market == "hk":
        return f"HK{sym}"
    return f"{_cn_exchange_prefix(sym).upper()}{sym}"


def to_baostock_code(sym: str) -> str:
    return f"{_cn_exchange_prefix(sym)}.{sym}"


def to_yf_symbol(sym: str, market: str) -> str:
    if market == "hk":
        # Yahoo Finance HK tickers are 4 digits (e.g. ``0700.HK`` for Tencent,
        # ``9988.HK`` for Alibaba); akshare's 5-digit zero-padded form
        # (``00700``) returns "possibly delisted" from Yahoo. Drop one
        # leading zero, then re-pad to 4.
        digits = sym.lstrip("0") or "0"
        return f"{digits.zfill(4)}.HK"
    prefix = _cn_exchange_prefix(sym)
    return {"sh": f"{sym}.SS", "sz": f"{sym}.SZ", "bj": f"{sym}.BJ"}[prefix]


def to_stooq_symbol(sym: str, market: str) -> str:
    """Stooq's symbol convention differs from Yahoo's:

    * HK: bare digit code without leading zeros, ``.hk`` lowercase suffix
      (``700.hk`` for Tencent, ``9988.hk`` for Alibaba). Stooq returns
      ``N/D`` for the zero-padded ``0700.hk`` form.
    * CN A-shares: 6-digit code + ``.cn`` suffix regardless of which
      exchange (``600519.cn``, ``000001.cn``). Stooq does not distinguish
      Shanghai/Shenzhen/Beijing.
    """

    if market == "hk":
        digits = sym.lstrip("0") or "0"
        return f"{digits}.hk"
    return f"{sym}.cn"
