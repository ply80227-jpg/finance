"""Xueqiu (snowballfinance.com) public-endpoint client and provider adapters.

The client uses ``urllib`` (no extra SDK), persists its bootstrap cookie to
disk so subsequent CLI invocations do not pay the bootstrap cost, and retries
once on transient ``URLError`` or 401/403 responses with a fresh cookie.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError

from ..cache import load_json, store_json
from ..models import FetchResult
from ..normalize import to_xq_symbol
from ..utils import to_float, utc_now_iso

_COOKIE_CACHE_FILE = "xueqiu_cookie.json"
_COOKIE_TTL_SECONDS = 30 * 60  # 30 minutes
_DEFAULT_TIMEOUT = 10.0


class XueqiuClient:
    """Public-endpoint client for Xueqiu quote / kline / news.

    The cookie is persisted to ``~/.cache/hermes_market/xueqiu_cookie.json``
    with a TTL so that one-shot CLI invocations do not need to bootstrap a
    fresh cookie every time.
    """

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.cookie: str | None = None
        self.timeout = timeout
        self._load_cached_cookie()

    def _load_cached_cookie(self) -> None:
        payload = load_json(_COOKIE_CACHE_FILE)
        if not payload:
            return
        cookie = payload.get("cookie")
        ts = payload.get("ts")
        if isinstance(cookie, str) and isinstance(ts, (int, float)) and time.time() - ts < _COOKIE_TTL_SECONDS:
            self.cookie = cookie

    def _store_cookie(self) -> None:
        if self.cookie:
            store_json(_COOKIE_CACHE_FILE, {"cookie": self.cookie, "ts": int(time.time())})

    def _refresh_cookie(self) -> None:
        bootstrap_urls = ["https://xueqiu.com/", "https://xueqiu.com/hq"]
        last_err: BaseException | None = None
        for u in bootstrap_urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - public URL
                    set_cookie = resp.headers.get_all("Set-Cookie") or []
                pairs: list[str] = []
                for c in set_cookie:
                    kv = c.split(";", 1)[0]
                    if "=" in kv:
                        pairs.append(kv)
                if pairs:
                    self.cookie = "; ".join(pairs)
                    self._store_cookie()
                    return
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(f"xueqiu cookie bootstrap failed: {last_err}")

    def _ensure_cookie(self) -> None:
        if not self.cookie:
            self._refresh_cookie()

    def _get_json(self, url: str) -> Any:
        self._ensure_cookie()
        assert self.cookie is not None
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://xueqiu.com/",
            "Cookie": self.cookie,
            "Accept": "application/json,text/plain,*/*",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (401, 403):
                self._refresh_cookie()
                headers["Cookie"] = self.cookie or ""
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    return json.loads(resp.read().decode("utf-8"))
            raise
        except URLError:
            # Transient network blip; one retry with a slightly longer timeout.
            with urllib.request.urlopen(req, timeout=self.timeout + 2) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))

    def quote(self, symbol: str) -> dict[str, Any]:
        url = f"https://stock.xueqiu.com/v5/stock/quote.json?symbol={urllib.parse.quote(symbol)}&extend=detail"
        j = self._get_json(url)
        item = (j.get("data") or {}).get("quote") or {}
        if not item:
            raise RuntimeError("empty quote from xueqiu")
        return item

    def news(self, symbol: str, count: int = 20) -> list[dict[str, str]]:
        url = f"https://stock.xueqiu.com/v5/stock/realtime/news.json?symbol={urllib.parse.quote(symbol)}&count={count}"
        j = self._get_json(url)
        lst = (j.get("data") or {}).get("items") or []
        out: list[dict[str, str]] = []
        for it in lst:
            out.append(
                {
                    "title": str(it.get("title") or ""),
                    "time": str(it.get("created_at") or ""),
                    "source": str(it.get("source") or "xueqiu"),
                    "url": str(it.get("target") or ""),
                }
            )
        if not out:
            raise RuntimeError("empty news from xueqiu")
        return out

    def history(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        sdt = datetime.strptime(start, "%Y-%m-%d").date()
        edt = datetime.strptime(end, "%Y-%m-%d").date()
        # Estimate trading days conservatively (1.6x calendar days, +20 floor).
        days = (edt - sdt).days
        needed = max(20, int(days * 1.6) + 5)
        # The endpoint counts backwards from ``begin``; cap per-call to 500.
        end_ms = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)
        pulled: list[Any] = []
        cols: list[str] = []
        remaining = needed
        cursor_ms = end_ms
        while remaining > 0:
            count = -min(remaining, 500)
            url = (
                "https://stock.xueqiu.com/v5/stock/chart/kline.json?"
                f"symbol={urllib.parse.quote(symbol)}&begin={cursor_ms}"
                f"&period=day&type=before&count={count}&indicator=kline"
            )
            j = self._get_json(url)
            data = j.get("data") or {}
            cols = data.get("column") or cols
            items = data.get("item") or []
            if not items:
                break
            pulled = items + pulled  # prepend older bars
            # Earliest timestamp in this batch becomes the new "before" cursor.
            earliest_ms = items[0][0]
            if earliest_ms >= cursor_ms:
                break  # no progress
            cursor_ms = earliest_ms
            if len(items) < min(remaining, 500):
                break
            remaining -= len(items)
        if not pulled:
            raise RuntimeError("empty history from xueqiu")
        idx = {c: i for i, c in enumerate(cols)}
        if "timestamp" not in idx:
            raise RuntimeError("xueqiu history missing timestamp column")
        out: list[dict[str, Any]] = []
        for row in pulled:
            ts = row[idx["timestamp"]]
            d = datetime.utcfromtimestamp(ts / 1000).date()
            if d < sdt or d > edt:
                continue
            try:
                bar = {
                    "date": d.strftime("%Y-%m-%d"),
                    "open": to_float(row[idx["open"]]),
                    "high": to_float(row[idx["high"]]),
                    "low": to_float(row[idx["low"]]),
                    "close": to_float(row[idx["close"]]),
                    "volume": to_float(row[idx["volume"]]),
                }
            except KeyError as e:
                raise RuntimeError(f"xueqiu history missing column {e}") from e
            out.append(bar)
        if not out:
            raise RuntimeError("empty history from xueqiu after filtering")
        return out


def quote(client: XueqiuClient, sym: str, market: str) -> FetchResult:
    xq_symbol = to_xq_symbol(sym, market)
    q = client.quote(xq_symbol)
    data = {
        "name": q.get("name") or xq_symbol,
        "last": to_float(q.get("current")),
        "change_pct": to_float(q.get("percent")),
        "turnover": to_float(q.get("amount")),
        "timestamp": utc_now_iso(),
    }
    return FetchResult(True, "xueqiu", sym, market, data)


def history(client: XueqiuClient, sym: str, market: str, start: str, end: str) -> FetchResult:
    xq_symbol = to_xq_symbol(sym, market)
    bars = client.history(xq_symbol, start, end)
    return FetchResult(True, "xueqiu", sym, market, {"bars": bars})


def news(client: XueqiuClient, limit: int, symbol: str | None, market: str) -> FetchResult:
    xq_symbol = to_xq_symbol(symbol, market) if symbol else "SH000001"
    items = client.news(xq_symbol, limit)
    return FetchResult(True, "xueqiu", symbol or "", market, {"news": items})
