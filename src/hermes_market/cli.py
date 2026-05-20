"""CLI entry point. Preserves the historical ``python hermes_market_data.py`` UX.

Concurrency / timeout flags
---------------------------

* ``--provider-timeout SECONDS`` — max seconds to wait per provider before
  giving up and moving to the next (default: ``HERMES_PROVIDER_TIMEOUT`` env
  var or :data:`~hermes_market.fetcher.DEFAULT_PROVIDER_TIMEOUT`).
* ``--deadline SECONDS`` — hard global deadline for the whole fallback chain
  (default: ``HERMES_GLOBAL_DEADLINE`` env var or
  :data:`~hermes_market.fetcher.DEFAULT_GLOBAL_DEADLINE`).
* ``--hedge-delay SECONDS`` — when set to a positive value, enables hedged
  concurrent fallback: the next provider is spawned in parallel after the
  previous one has been pending for this many seconds, and whichever
  succeeds first wins. Default: ``HERMES_HEDGE_DELAY`` env var or strict
  sequential.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from .fetcher import DEFAULT_GLOBAL_DEADLINE, DEFAULT_PROVIDER_TIMEOUT, MarketDataFetcher
from .models import FetchResult


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A/HK free market data fetcher with multi-provider fallback")
    parser.add_argument(
        "--provider-timeout",
        type=float,
        default=_env_float("HERMES_PROVIDER_TIMEOUT", DEFAULT_PROVIDER_TIMEOUT),
        help="Max seconds per provider attempt (env: HERMES_PROVIDER_TIMEOUT)",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=_env_float("HERMES_GLOBAL_DEADLINE", DEFAULT_GLOBAL_DEADLINE),
        help="Hard global deadline for the whole fallback chain (env: HERMES_GLOBAL_DEADLINE)",
    )
    parser.add_argument(
        "--hedge-delay",
        type=float,
        default=_env_float("HERMES_HEDGE_DELAY", None),
        help="Enable hedged concurrent fallback: spawn the next provider after this many seconds. "
        "Default = sequential. (env: HERMES_HEDGE_DELAY)",
    )

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
        fetcher = MarketDataFetcher(
            provider_timeout=args.provider_timeout,
            global_deadline=args.deadline,
            hedge_delay=args.hedge_delay,
        )
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
