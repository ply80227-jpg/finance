"""akshare-backed quote / history / news provider."""

from __future__ import annotations

from typing import Any

from ..cache import TTLCache
from ..models import FetchResult
from ..utils import to_float, utc_now_iso

# Shared per-process TTL cache for the (heavy) full-market spot tables.
_SPOT_CACHE = TTLCache(ttl_seconds=8.0)


def load_module():  # type: ignore[no-untyped-def]
    try:
        import akshare as ak  # type: ignore[import-not-found]

        return ak
    except Exception:
        return None


def _cached_spot(ak, table: str):  # type: ignore[no-untyped-def]
    hit = _SPOT_CACHE.get(table)
    if hit is not None:
        return hit
    fn = getattr(ak, table)
    df = fn()
    _SPOT_CACHE.set(table, df)
    return df


def _bid_ask_dict(df: Any) -> dict[str, str]:
    """Convert an akshare ``stock_bid_ask_em`` DataFrame (``item``/``value`` columns)
    to a ``{item: value}`` dict.
    """

    try:
        return {str(r["item"]): str(r["value"]) for _, r in df.iterrows()}
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"akshare stock_bid_ask_em returned unexpected shape: {exc}") from exc


def quote_cn(ak, sym: str) -> FetchResult:  # type: ignore[no-untyped-def]
    """Single-stock CN quote via ``stock_bid_ask_em`` (P3 fast path).

    Falls back to the spot-table-scan path if the per-stock endpoint is
    unavailable on the installed akshare version or rejects the symbol.
    """

    fn = getattr(ak, "stock_bid_ask_em", None)
    if fn is not None:
        try:
            df = fn(symbol=sym)
            if df is not None and not df.empty:
                d = _bid_ask_dict(df)
                last = to_float(d.get("最新"))
                if last is not None:
                    data = {
                        "name": None,  # bid/ask endpoint omits the company name; left to caller
                        "last": last,
                        "change_pct": to_float(d.get("涨幅") or d.get("涨跌幅")),
                        "turnover": to_float(d.get("成交额")),
                        "volume": to_float(d.get("成交量")),
                        "open": to_float(d.get("今开")),
                        "high": to_float(d.get("最高")),
                        "low": to_float(d.get("最低")),
                        "prev_close": to_float(d.get("昨收")),
                        "timestamp": utc_now_iso(),
                        "source": "stock_bid_ask_em",
                    }
                    return FetchResult(True, "akshare", sym, "cn", data)
        except Exception:
            # fall through to spot-table scan below
            pass
    return _quote_cn_via_spot(ak, sym)


def _quote_cn_via_spot(ak, sym: str) -> FetchResult:  # type: ignore[no-untyped-def]
    spot = _cached_spot(ak, "stock_zh_a_spot_em")
    row = spot.loc[spot["代码"] == sym]
    if row.empty:
        raise ValueError(f"CN symbol not found in akshare spot: {sym}")
    r = row.iloc[0]
    data = {
        "name": r.get("名称"),
        "last": to_float(r.get("最新价")),
        "change_pct": to_float(r.get("涨跌幅")),
        "turnover": to_float(r.get("成交额")),
        "timestamp": utc_now_iso(),
        "source": "stock_zh_a_spot_em",
    }
    return FetchResult(True, "akshare", sym, "cn", data)


def quote_hk(ak, sym: str) -> FetchResult:  # type: ignore[no-untyped-def]
    spot = _cached_spot(ak, "stock_hk_spot_em")
    codes = spot["代码"].astype(str).str.zfill(5)
    row = spot.loc[codes == sym]
    if row.empty:
        raise ValueError(f"HK symbol not found in akshare spot: {sym}")
    r = row.iloc[0]
    data = {
        "name": r.get("名称"),
        "last": to_float(r.get("最新价")),
        "change_pct": to_float(r.get("涨跌幅")),
        "turnover": to_float(r.get("成交额")),
        "timestamp": utc_now_iso(),
    }
    return FetchResult(True, "akshare", sym, "hk", data)


def history(ak, sym: str, market: str, start: str, end: str) -> FetchResult:  # type: ignore[no-untyped-def]
    if market == "cn":
        df = ak.stock_zh_a_hist(
            symbol=sym,
            period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust="",
        )
    else:
        df = ak.stock_hk_hist(
            symbol=sym,
            period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust="",
        )
    if df is None or df.empty:
        raise ValueError("empty history from akshare")
    rows: list[dict[str, Any]] = [
        {
            "date": str(r.get("日期")),
            "open": to_float(r.get("开盘")),
            "high": to_float(r.get("最高")),
            "low": to_float(r.get("最低")),
            "close": to_float(r.get("收盘")),
            "volume": to_float(r.get("成交量")),
        }
        for _, r in df.iterrows()
    ]
    return FetchResult(True, "akshare", sym, market, {"bars": rows})


def news(ak, limit: int, symbol: str | None, market: str) -> FetchResult:  # type: ignore[no-untyped-def]
    df = ak.stock_news_em(symbol=symbol) if symbol and market == "cn" else ak.stock_info_global_em()
    if df is None or df.empty:
        raise ValueError("empty news from akshare")
    items: list[dict[str, str]] = []
    for _, r in df.head(limit).iterrows():
        title = str(r.get("标题") or r.get("内容") or r.get("资讯标题") or "")
        items.append(
            {
                "title": title,
                "time": str(r.get("发布时间") or r.get("时间") or ""),
                "source": str(r.get("文章来源") or r.get("来源") or ""),
                "url": str(r.get("新闻链接") or r.get("链接") or ""),
            }
        )
    if not items:
        raise ValueError("empty filtered news from akshare")
    # NOTE: HK akshare news has no symbol filter; we intentionally do not drop
    # rows that omit the symbol in the title (see provider docstring) — the
    # caller routes HK + symbol queries to xueqiu first via the fetcher.
    return FetchResult(True, "akshare", symbol or "", market, {"news": items[:limit]})
