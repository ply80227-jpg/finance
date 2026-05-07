#!/usr/bin/env python3
"""免费 A 股/港股行情数据拉取脚本（支持 fallback），适合 Hermes agent 接入。

特点：
1) 自动识别市场：A 股（sh/sz 前缀或 6 位代码）、港股（5 位代码或 .HK）。
2) 主数据源优先使用 akshare；失败时自动回退到 yfinance。
3) 标准 JSON 输出，便于 agent 直接解析。

示例：
  python hermes_market_data.py quote --symbol 600519 --market cn
  python hermes_market_data.py quote --symbol 00700 --market hk
  python hermes_market_data.py history --symbol 600519 --start 2025-01-01 --end 2025-01-31
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


def _load_akshare():
    try:
        import akshare as ak  # type: ignore

        return ak
    except Exception:
        return None


def _load_yfinance():
    try:
        import yfinance as yf  # type: ignore

        return yf
    except Exception:
        return None




def _load_xueqiu_client():
    # 基于公开接口，使用 urllib + cookie 轻量接入
    return XueqiuClient()

def _load_baostock():
    try:
        import baostock as bs  # type: ignore

        return bs
    except Exception:
        return None


def _load_stooq_reader():
    try:
        from pandas_datareader import data as pdr  # type: ignore

        return pdr
    except Exception:
        return None




def _fail_result(provider: str, sym: str, mkt: str, errors: List[str]) -> FetchResult:
    return FetchResult(False, provider, sym, mkt, {}, "; ".join(errors))

@dataclass
class FetchResult:
    ok: bool
    provider: str
    symbol: str
    market: str
    data: Dict[str, Any]
    error: Optional[str] = None


class MarketDataFetcher:
    def __init__(self):
        self.ak = _load_akshare()
        self.yf = _load_yfinance()
        self.xq = _load_xueqiu_client()
        self.bs = _load_baostock()
        self.stooq = _load_stooq_reader()

    @staticmethod
    def detect_market(symbol: str, market: Optional[str]) -> str:
        if market in {"cn", "hk"}:
            return market
        s = symbol.strip().lower()
        if re.fullmatch(r"(sh|sz)\d{6}", s) or re.fullmatch(r"\d{6}", s):
            return "cn"
        if re.fullmatch(r"\d{5}", s) or s.endswith(".hk"):
            return "hk"
        raise ValueError(f"无法识别市场，请显式传入 --market cn|hk，symbol={symbol}")

    @staticmethod
    def normalize_symbol(symbol: str, market: str) -> str:
        s = symbol.strip().lower()
        if market == "cn":
            if re.fullmatch(r"\d{6}", s):
                return s
            if re.fullmatch(r"(sh|sz)\d{6}", s):
                return s[2:]
        if market == "hk":
            if s.endswith(".hk"):
                return s[:-3].zfill(5)
            if re.fullmatch(r"\d{1,5}", s):
                return s.zfill(5)
        raise ValueError(f"symbol 格式不正确: {symbol} (market={market})")

    def quote(self, symbol: str, market: Optional[str] = None) -> FetchResult:
        mkt = self.detect_market(symbol, market)
        sym = self.normalize_symbol(symbol, mkt)

        if self.ak:
            try:
                if mkt == "cn":
                    return self._quote_cn_ak(sym)
                return self._quote_hk_ak(sym)
            except Exception as e:
                ak_error = f"akshare failed: {e}"
        else:
            ak_error = "akshare not installed"

        yf_error = "yfinance not installed"
        if self.yf:
            try:
                return self._quote_yf(sym, mkt)
            except Exception as e:
                yf_error = f"yfinance failed: {e}"

        xq_error = "xueqiu unavailable"
        try:
            return self._quote_xq(sym, mkt)
        except Exception as e:
            xq_error = f"xueqiu failed: {e}"

        bs_error = "baostock not installed"
        if self.bs:
            try:
                return self._quote_baostock(sym, mkt)
            except Exception as e:
                bs_error = f"baostock failed: {e}"

        if self.stooq:
            try:
                return self._quote_stooq(sym, mkt)
            except Exception as e:
                return _fail_result("stooq", sym, mkt, [f"fallback failed", ak_error, yf_error, xq_error, bs_error, f"stooq failed: {e}"])

        return _fail_result("none", sym, mkt, ["no provider available", ak_error, yf_error, xq_error, bs_error, "stooq reader not installed"])

    def history(self, symbol: str, start: str, end: str, market: Optional[str] = None) -> FetchResult:
        mkt = self.detect_market(symbol, market)
        sym = self.normalize_symbol(symbol, mkt)

        if self.ak:
            try:
                return self._history_ak(sym, mkt, start, end)
            except Exception as e:
                ak_error = f"akshare failed: {e}"
        else:
            ak_error = "akshare not installed"

        yf_error = "yfinance not installed"
        if self.yf:
            try:
                return self._history_yf(sym, mkt, start, end)
            except Exception as e:
                yf_error = f"yfinance failed: {e}"

        xq_error = "xueqiu unavailable"
        try:
            return self._history_xq(sym, mkt, start, end)
        except Exception as e:
            xq_error = f"xueqiu failed: {e}"

        bs_error = "baostock not installed"
        if self.bs:
            try:
                return self._history_baostock(sym, mkt, start, end)
            except Exception as e:
                bs_error = f"baostock failed: {e}"

        if self.stooq:
            try:
                return self._history_stooq(sym, mkt, start, end)
            except Exception as e:
                return _fail_result("stooq", sym, mkt, [f"fallback failed", ak_error, yf_error, xq_error, bs_error, f"stooq failed: {e}"])

        return _fail_result("none", sym, mkt, ["no provider available", ak_error, yf_error, xq_error, bs_error, "stooq reader not installed"])


    def news(self, limit: int = 20, symbol: Optional[str] = None, market: Optional[str] = None) -> FetchResult:
        symbol_norm = None
        mkt = market or "global"
        if symbol:
            mkt = self.detect_market(symbol, market)
            symbol_norm = self.normalize_symbol(symbol, mkt)

        if self.ak:
            try:
                return self._news_ak(limit, symbol_norm, mkt)
            except Exception as e:
                ak_error = f"akshare news failed: {e}"
        else:
            ak_error = "akshare not installed"

        xq_error = "xueqiu unavailable"
        try:
            return self._news_xq(limit, symbol_norm, mkt)
        except Exception as e:
            xq_error = f"xueqiu news failed: {e}"

        try:
            return self._news_sina(limit, symbol_norm)
        except Exception as e:
            return _fail_result("sina", symbol_norm or "", mkt, ["news fallback failed", ak_error, xq_error, f"sina rss failed: {e}"])

    def _news_ak(self, limit: int, symbol: Optional[str], market: str) -> FetchResult:
        if symbol and market == "cn":
            df = self.ak.stock_news_em(symbol=symbol)
        else:
            df = self.ak.stock_info_global_em()
        if df is None or df.empty:
            raise ValueError("empty news from akshare")
        items = []
        for _, r in df.head(limit).iterrows():
            title = str(r.get("标题") or r.get("内容") or r.get("资讯标题") or "")
            if symbol and symbol not in title and market == "hk":
                continue
            items.append({
                "title": title,
                "time": str(r.get("发布时间") or r.get("时间") or ""),
                "source": str(r.get("文章来源") or r.get("来源") or ""),
                "url": str(r.get("新闻链接") or r.get("链接") or ""),
            })
        if not items:
            raise ValueError("empty filtered news from akshare")
        return FetchResult(True, "akshare", symbol or "", market, {"news": items[:limit]})

    def _news_xq(self, limit: int, symbol: Optional[str], market: str) -> FetchResult:
        xq_symbol = _to_xq_symbol(symbol, market) if symbol else "SH000001"
        items = self.xq.news(xq_symbol, limit)
        return FetchResult(True, "xueqiu", symbol or "", market, {"news": items})

    def _news_sina(self, limit: int, symbol: Optional[str]) -> FetchResult:
        url = "https://rss.sina.com.cn/finance/allnews.xml"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
        items = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if symbol and symbol not in title:
                continue
            items.append({
                "title": title,
                "time": (item.findtext("pubDate") or "").strip(),
                "source": "sina_rss",
                "url": (item.findtext("link") or "").strip(),
            })
            if len(items) >= limit:
                break
        if not items:
            raise RuntimeError("empty news from sina rss")
        return FetchResult(True, "sina_rss", symbol or "", "global", {"news": items})

    def _quote_cn_ak(self, sym: str) -> FetchResult:
        spot = self.ak.stock_zh_a_spot_em()
        row = spot.loc[spot["代码"] == sym]
        if row.empty:
            raise ValueError(f"CN symbol not found in akshare spot: {sym}")
        r = row.iloc[0]
        data = {
            "name": r.get("名称"),
            "last": _to_float(r.get("最新价")),
            "change_pct": _to_float(r.get("涨跌幅")),
            "turnover": _to_float(r.get("成交额")),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        return FetchResult(True, "akshare", sym, "cn", data)

    def _quote_hk_ak(self, sym: str) -> FetchResult:
        spot = self.ak.stock_hk_spot_em()
        row = spot.loc[spot["代码"].astype(str).str.zfill(5) == sym]
        if row.empty:
            raise ValueError(f"HK symbol not found in akshare spot: {sym}")
        r = row.iloc[0]
        data = {
            "name": r.get("名称"),
            "last": _to_float(r.get("最新价")),
            "change_pct": _to_float(r.get("涨跌幅")),
            "turnover": _to_float(r.get("成交额")),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        return FetchResult(True, "akshare", sym, "hk", data)

    def _quote_yf(self, sym: str, mkt: str) -> FetchResult:
        ticker = _to_yf_symbol(sym, mkt)
        t = self.yf.Ticker(ticker)
        info = t.fast_info
        data = {
            "name": ticker,
            "last": _to_float(info.get("lastPrice")),
            "change_pct": _pct_change_from_fast_info(info),
            "turnover": _to_float(info.get("lastVolume")),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        return FetchResult(True, "yfinance", sym, mkt, data)

    def _history_ak(self, sym: str, mkt: str, start: str, end: str) -> FetchResult:
        if mkt == "cn":
            df = self.ak.stock_zh_a_hist(
                symbol=sym,
                period="daily",
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
                adjust="",
            )
        else:
            df = self.ak.stock_hk_hist(
                symbol=sym,
                period="daily",
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
                adjust="",
            )
        if df is None or df.empty:
            raise ValueError("empty history from akshare")
        rows = []
        for _, r in df.iterrows():
            rows.append(
                {
                    "date": str(r.get("日期")),
                    "open": _to_float(r.get("开盘")),
                    "high": _to_float(r.get("最高")),
                    "low": _to_float(r.get("最低")),
                    "close": _to_float(r.get("收盘")),
                    "volume": _to_float(r.get("成交量")),
                }
            )
        return FetchResult(True, "akshare", sym, mkt, {"bars": rows})

    def _history_yf(self, sym: str, mkt: str, start: str, end: str) -> FetchResult:
        ticker = _to_yf_symbol(sym, mkt)
        hist = self.yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
        if hist is None or hist.empty:
            raise ValueError("empty history from yfinance")
        rows: List[Dict[str, Any]] = []
        for idx, r in hist.iterrows():
            rows.append(
                {
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": _to_float(r.get("Open")),
                    "high": _to_float(r.get("High")),
                    "low": _to_float(r.get("Low")),
                    "close": _to_float(r.get("Close")),
                    "volume": _to_float(r.get("Volume")),
                }
            )
        return FetchResult(True, "yfinance", sym, mkt, {"bars": rows})


    def _quote_xq(self, sym: str, mkt: str) -> FetchResult:
        xq_symbol = _to_xq_symbol(sym, mkt)
        q = self.xq.quote(xq_symbol)
        data = {
            "name": q.get("name") or xq_symbol,
            "last": _to_float(q.get("current")),
            "change_pct": _to_float(q.get("percent")),
            "turnover": _to_float(q.get("amount")),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        return FetchResult(True, "xueqiu", sym, mkt, data)

    def _history_xq(self, sym: str, mkt: str, start: str, end: str) -> FetchResult:
        xq_symbol = _to_xq_symbol(sym, mkt)
        bars = self.xq.history(xq_symbol, start, end)
        return FetchResult(True, "xueqiu", sym, mkt, {"bars": bars})

    def _quote_baostock(self, sym: str, mkt: str) -> FetchResult:
        if mkt != "cn":
            raise ValueError("baostock supports CN only")
        code = _to_baostock_code(sym)
        lg = self.bs.login()
        if lg.error_code != "0":
            raise RuntimeError(lg.error_msg)
        try:
            rs = self.bs.query_history_k_data_plus(
                code,
                "date,code,close,preclose,volume",
                start_date=datetime.utcnow().strftime("%Y-%m-%d"),
                end_date=datetime.utcnow().strftime("%Y-%m-%d"),
                frequency="d",
                adjustflag="3",
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                raise ValueError("baostock T+1: no data for today")
            date, _, close, preclose, volume = rows[-1]
            close_f = _to_float(close)
            prev_f = _to_float(preclose)
            pct = None if close_f is None or prev_f in (None, 0) else (close_f - prev_f) / prev_f * 100
            return FetchResult(True, "baostock", sym, mkt, {
                "name": code,
                "last": close_f,
                "change_pct": pct,
                "turnover": _to_float(volume),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "note": "T+1 delayed via baostock",
            })
        finally:
            self.bs.logout()

    def _history_baostock(self, sym: str, mkt: str, start: str, end: str) -> FetchResult:
        if mkt != "cn":
            raise ValueError("baostock supports CN only")
        code = _to_baostock_code(sym)
        lg = self.bs.login()
        if lg.error_code != "0":
            raise RuntimeError(lg.error_msg)
        try:
            rs = self.bs.query_history_k_data_plus(
                code,
                "date,open,high,low,close,volume",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="3",
            )
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            rows: List[Dict[str, Any]] = []
            while rs.next():
                date, op, hi, lo, cl, vol = rs.get_row_data()
                rows.append({"date": date, "open": _to_float(op), "high": _to_float(hi), "low": _to_float(lo), "close": _to_float(cl), "volume": _to_float(vol)})
            if not rows:
                raise ValueError("empty history from baostock")
            return FetchResult(True, "baostock", sym, mkt, {"bars": rows, "note": "T+1 delayed via baostock"})
        finally:
            self.bs.logout()

    def _quote_stooq(self, sym: str, mkt: str) -> FetchResult:
        ticker = _to_yf_symbol(sym, mkt)
        end = datetime.utcnow()
        start = end.replace(day=1)
        df = self.stooq.DataReader(ticker, "stooq", start, end)
        if df is None or df.empty:
            raise ValueError("empty quote from stooq")
        r = df.sort_index().iloc[-1]
        data = {
            "name": ticker,
            "last": _to_float(r.get("Close")),
            "change_pct": None,
            "turnover": _to_float(r.get("Volume")),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        return FetchResult(True, "stooq", sym, mkt, data)

    def _history_stooq(self, sym: str, mkt: str, start: str, end: str) -> FetchResult:
        ticker = _to_yf_symbol(sym, mkt)
        df = self.stooq.DataReader(ticker, "stooq", start, end)
        if df is None or df.empty:
            raise ValueError("empty history from stooq")
        rows: List[Dict[str, Any]] = []
        for idx, r in df.sort_index().iterrows():
            rows.append(
                {
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": _to_float(r.get("Open")),
                    "high": _to_float(r.get("High")),
                    "low": _to_float(r.get("Low")),
                    "close": _to_float(r.get("Close")),
                    "volume": _to_float(r.get("Volume")),
                }
            )
        return FetchResult(True, "stooq", sym, mkt, {"bars": rows})



class XueqiuClient:
    def __init__(self):
        self.cookie = None

    def _refresh_cookie(self):
        # 多入口刷新 cookie，提升可用性
        bootstrap_urls = ["https://xueqiu.com/", "https://xueqiu.com/hq"]
        last_err = None
        for u in bootstrap_urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    set_cookie = resp.headers.get_all("Set-Cookie") or []
                pairs = []
                for c in set_cookie:
                    kv = c.split(";", 1)[0]
                    if "=" in kv:
                        pairs.append(kv)
                if pairs:
                    self.cookie = "; ".join(pairs)
                    return
            except Exception as e:
                last_err = e
        raise RuntimeError(f"xueqiu cookie bootstrap failed: {last_err}")

    def _ensure_cookie(self):
        if not self.cookie:
            self._refresh_cookie()

    def _get_json(self, url: str):
        self._ensure_cookie()
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://xueqiu.com/",
            "Cookie": self.cookie,
            "Accept": "application/json,text/plain,*/*",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            # 401/403 常见于 cookie 过期，刷新后重试一次
            if e.code in (401, 403):
                self._refresh_cookie()
                headers["Cookie"] = self.cookie
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            raise
        except URLError:
            # 网络临时波动，重试一次
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode("utf-8"))

    def quote(self, symbol: str):
        url = f"https://stock.xueqiu.com/v5/stock/quote.json?symbol={urllib.parse.quote(symbol)}&extend=detail"
        j = self._get_json(url)
        item = ((j.get("data") or {}).get("quote") or {})
        if not item:
            raise RuntimeError("empty quote from xueqiu")
        return item

    def news(self, symbol: str, count: int = 20):
        url = f"https://stock.xueqiu.com/v5/stock/realtime/news.json?symbol={urllib.parse.quote(symbol)}&count={count}"
        j = self._get_json(url)
        lst = ((j.get("data") or {}).get("items") or [])
        out = []
        for it in lst:
            out.append({"title": str(it.get("title") or ""), "time": str(it.get("created_at") or ""), "source": str(it.get("source") or "xueqiu"), "url": str(it.get("target") or "")})
        if not out:
            raise RuntimeError("empty news from xueqiu")
        return out

    def history(self, symbol: str, start: str, end: str):
        end_ms = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)
        url = (
            "https://stock.xueqiu.com/v5/stock/chart/kline.json?"
            f"symbol={urllib.parse.quote(symbol)}&begin={end_ms}&period=day&type=before&count=-500&indicator=kline"
        )
        j = self._get_json(url)
        data = j.get("data") or {}
        cols = data.get("column") or []
        items = data.get("item") or []
        idx = {c: i for i, c in enumerate(cols)}
        if "timestamp" not in idx:
            raise RuntimeError("xueqiu history missing timestamp column")
        out = []
        sdt = datetime.strptime(start, "%Y-%m-%d").date()
        edt = datetime.strptime(end, "%Y-%m-%d").date()
        for row in items:
            ts = row[idx["timestamp"]]
            d = datetime.utcfromtimestamp(ts / 1000).date()
            if d < sdt or d > edt:
                continue
            out.append({"date": d.strftime("%Y-%m-%d"), "open": _to_float(row[idx.get("open", 1)]), "high": _to_float(row[idx.get("high", 2)]), "low": _to_float(row[idx.get("low", 3)]), "close": _to_float(row[idx.get("close", 5)]), "volume": _to_float(row[idx.get("volume", 6)])})
        if not out:
            raise RuntimeError("empty history from xueqiu")
        return out



def _to_xq_symbol(sym: str, market: str) -> str:
    if market == "hk":
        return f"HK{sym}"
    return f"SH{sym}" if sym.startswith("6") else f"SZ{sym}"


def _to_baostock_code(sym: str) -> str:
    return f"sh.{sym}" if sym.startswith("6") else f"sz.{sym}"


def _to_yf_symbol(sym: str, market: str) -> str:
    if market == "hk":
        return f"{sym}.HK"
    # A 股在 Yahoo 的后缀与交易所有关
    if sym.startswith("6"):
        return f"{sym}.SS"
    return f"{sym}.SZ"


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _pct_change_from_fast_info(info: Dict[str, Any]) -> Optional[float]:
    last_p = _to_float(info.get("lastPrice"))
    prev = _to_float(info.get("previousClose"))
    if last_p is None or prev in (None, 0):
        return None
    return (last_p - prev) / prev * 100


def main():
    parser = argparse.ArgumentParser(description="A/HK free market data fetcher with fallback")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_quote = sub.add_parser("quote", help="获取实时快照")
    p_quote.add_argument("--symbol", required=True)
    p_quote.add_argument("--market", choices=["cn", "hk"], default=None)

    p_hist = sub.add_parser("history", help="获取历史 K 线")
    p_hist.add_argument("--symbol", required=True)
    p_hist.add_argument("--market", choices=["cn", "hk"], default=None)
    p_hist.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_hist.add_argument("--end", required=True, help="YYYY-MM-DD")

    p_news = sub.add_parser("news", help="获取财经新闻")
    p_news.add_argument("--limit", type=int, default=20)
    p_news.add_argument("--symbol", default=None)
    p_news.add_argument("--market", choices=["cn", "hk"], default=None)

    args = parser.parse_args()
    fetcher = MarketDataFetcher()

    if args.cmd == "quote":
        result = fetcher.quote(args.symbol, args.market)
    elif args.cmd == "history":
        result = fetcher.history(args.symbol, args.start, args.end, args.market)
    else:
        result = fetcher.news(args.limit, args.symbol, args.market)

    print(json.dumps(asdict(result), ensure_ascii=False))
    sys.exit(0 if result.ok else 2)


if __name__ == "__main__":
    main()
