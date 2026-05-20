"""CLI entry point. Preserves the historical ``python hermes_market_data.py`` UX."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .fetcher import MarketDataFetcher
from .models import FetchResult


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A/HK free market data fetcher with multi-provider fallback")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_quote = sub.add_parser("quote", help="Realtime snapshot quote")
    p_quote.add_argument("--symbol", required=True)
    p_quote.add_argument("--market", choices=["cn", "hk"], default=None)

    p_hist = sub.add_parser("history", help="Daily K-line history")
    p_hist.add_argument("--symbol", required=True)
    p_hist.add_argument("--market", choices=["cn", "hk"], default=None)
    p_hist.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_hist.add_argument("--end", required=True, help="YYYY-MM-DD")

    p_news = sub.add_parser("news", help="Finance news headlines")
    p_news.add_argument("--limit", type=int, default=20)
    p_news.add_argument("--symbol", default=None)
    p_news.add_argument("--market", choices=["cn", "hk"], default=None)

    return parser


def _emit(result: FetchResult) -> int:
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if result.ok else 2


def _emit_error(provider: str, exc: BaseException) -> int:
    payload = {
        "ok": False,
        "provider": provider,
        "symbol": "",
        "market": "",
        "data": {},
        "error": f"{type(exc).__name__}: {exc}",
        "errors": [{"provider": provider, "message": f"{type(exc).__name__}: {exc}"}],
        "schema_version": 1,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse already wrote a human-readable error to stderr; preserve its code.
        raise e

    try:
        fetcher = MarketDataFetcher()
        if args.cmd == "quote":
            result = fetcher.quote(args.symbol, args.market)
        elif args.cmd == "history":
            result = fetcher.history(args.symbol, args.start, args.end, args.market)
        else:
            result = fetcher.news(args.limit, args.symbol, args.market)
    except Exception as e:  # noqa: BLE001
        return _emit_error("cli", e)

    return _emit(result)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
